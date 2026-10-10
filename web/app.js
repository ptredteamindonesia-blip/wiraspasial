/* WIRA-SPASIAL (Wawasan Intelijen & Ruang Analisis Spasial) — dashboard Leaflet + REST API */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtHa = (v) => `${(v ?? 0).toLocaleString("id-ID", { maximumFractionDigits: 1 })} ha`;
const fmtPct = (v) => `${Math.round((v ?? 0) * 100)}%`;
const STATUS_LABEL = { queued: "antri", running: "berjalan", done: "selesai", error: "gagal", cancelled: "dibatalkan" };
const statusBadge = (st) => `<span class="badge ${st}">${STATUS_LABEL[st] ?? st}</span>`;
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" }) : "–");

let META = null;
const STATE_KEY = "geoint.ui.v1";
function loadUiState() { try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; } catch { return {}; } }
function saveUiState(patch) { try { localStorage.setItem(STATE_KEY, JSON.stringify({ ...loadUiState(), ...patch })); } catch { /* storage diblokir */ } }
function saveForm() {
  saveUiState({ form: {
    preset: $("#preset").value, source: $("#source").value, name: $("#an-name").value,
    b: [$("#b-start").value, $("#b-end").value], a: [$("#a-start").value, $("#a-end").value], aoi: aoiGeom,
  } });
}
let aoiGeom = null;            // GeoJSON geometry AOI terpilih
let current = null;            // analisa yang sedang ditampilkan
let detFC = null;              // FeatureCollection deteksi
const layerById = new Map();

function accessToken() { try { return localStorage.getItem("geoint.token") || ""; } catch { return ""; } }
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const tok = accessToken();
  if (tok) headers["X-Wira-Token"] = tok;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body));
  return body;
}

function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

// ---------------------------------------------------------------- peta
const map = L.map("map", { zoomControl: true }).setView([-2.5, 118], 5);
const basemaps = {
  "Citra satelit (Esri)": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 19, attribution: "Esri, Maxar, Earthstar Geographics",
  }),
  "Topografi (OpenTopoMap)": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 17, attribution: "© OpenStreetMap, SRTM | © OpenTopoMap",
  }),
  OpenStreetMap: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }),
};
basemaps["Citra satelit (Esri)"].addTo(map);
L.control.layers(basemaps, null, { position: "bottomright" }).addTo(map);
L.control.scale({ imperial: false }).addTo(map);

const aoiLayer = L.featureGroup().addTo(map);
const detLayer = L.featureGroup().addTo(map);
let imgBefore = null, imgAfter = null, imgChanges = null;

map.addControl(new L.Control.Draw({
  edit: { featureGroup: aoiLayer, edit: false, remove: false },
  draw: { polygon: { allowIntersection: false, showArea: true }, rectangle: {}, polyline: false, circle: false, marker: false, circlemarker: false },
}));
map.on(L.Draw.Event.CREATED, (e) => {
  $("#preset").value = "";
  setAoi(e.layer.toGeoJSON().geometry);
});

function geomAreaKm2(g) {
  // Aproksimasi luas dari bbox poligon (cukup untuk validasi kasar).
  const pts = g.coordinates.flat(g.type === "MultiPolygon" ? 2 : 1);
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1]);
  const lat = ((Math.min(...lats) + Math.max(...lats)) / 2) * Math.PI / 180;
  return (Math.max(...lons) - Math.min(...lons)) * 111.32 * Math.cos(lat) * (Math.max(...lats) - Math.min(...lats)) * 110.574;
}

function setAoi(geometry, fit = true) {
  aoiGeom = geometry;
  aoiLayer.clearLayers();
  const l = L.geoJSON(geometry, { style: { color: "#ffd400", weight: 2, fill: false, dashArray: "6 4" } });
  aoiLayer.addLayer(l);
  if (fit) map.fitBounds(l.getBounds(), { padding: [20, 20] });
  saveForm();
  const km2 = geomAreaKm2(geometry);
  const lim = META?.limits?.max_aoi_km2 ?? 1500;
  $("#aoi-info").innerHTML = `Area ±${km2.toLocaleString("id-ID", { maximumFractionDigits: 1 })} km²` +
    (km2 > lim ? ` — <b style="color:var(--human)">melebihi batas ${lim} km²</b>` : "");
}

function bboxToGeom(b) {
  return { type: "Polygon", coordinates: [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]]] };
}

// ---------------------------------------------------------------- tabs
// Sub-tab modul SAT (Analisa / Pantauan / Riwayat). Alert & Aktivitas kini modul tersendiri di rail.
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll("#mod-sat .tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
    if (b.dataset.tab === "pantauan") { loadAois(); loadMonitor(); }
    if (b.dataset.tab === "riwayat") loadHistory();
    saveUiState({ tab: b.dataset.tab });
  })
);
function openSatTab(tab) {
  if (typeof showModule === "function") showModule("sat");
  document.querySelector(`#tabs button[data-tab="${tab}"]`).click();
}

// ---------------------------------------------------------------- init
async function init() {
  META = await api("/api/meta");
  for (const p of META.presets) $("#preset").add(new Option(`${p.name} — ${p.theme}`, p.id));
  for (const [k, v] of Object.entries(META.sources)) $("#source").add(new Option(v, k));
  for (const [k, v] of Object.entries(META.categories)) $("#flt-cat").add(new Option(v, k));
  for (const [k, v] of Object.entries(META.classes)) $("#flt-class").add(new Option(v.label, k));
  [$("#b-start").value, $("#b-end").value] = META.defaults.before;
  [$("#a-start").value, $("#a-end").value] = META.defaults.after;
  $("#legend").innerHTML = Object.values(META.classes)
    .map((c) => `<div><span class="swatch" style="background:${c.color}"></span>${esc(c.label)}</div>`).join("");
  loadAlertCount();
  setInterval(loadAlertCount, 60000);
  await restoreState();
}

// Pulihkan tampilan terakhir setelah halaman dimuat ulang / server direstart.
async function restoreState() {
  const st = loadUiState();
  const f = st.form;
  if (f) {
    if (f.preset) $("#preset").value = f.preset;
    if (f.source && META.sources[f.source]) $("#source").value = f.source;
    $("#an-name").value = f.name || "";
    if (f.b?.[0]) [$("#b-start").value, $("#b-end").value] = f.b;
    if (f.a?.[0]) [$("#a-start").value, $("#a-end").value] = f.a;
    if (f.aoi) setAoi(f.aoi);
  }
  const running = await api("/api/analyses?limit=20").then((r) => r.filter((a) => ["queued", "running"].includes(a.status)));
  if (running.length) {
    toast(`${running.length} analisa sedang berjalan/dilanjutkan di server.`);
    watchJob(running[0].id);
  } else if (st.lastAnalysis) {
    await showAnalysis(st.lastAnalysis, { switchModule: !st.module || st.module === "sat" }).catch(() => saveUiState({ lastAnalysis: null }));
  }
  if (st.tab && ["pantauan", "riwayat"].includes(st.tab)) document.querySelector(`#tabs button[data-tab="${st.tab}"]`)?.click();
}
["#preset", "#source", "#an-name", "#b-start", "#b-end", "#a-start", "#a-end"].forEach((s) => $(s).addEventListener("change", saveForm));

$("#preset").addEventListener("change", (e) => {
  const p = META.presets.find((x) => x.id === e.target.value);
  if (p) { setAoi(bboxToGeom(p.bbox)); $("#an-name").value = p.name; }
});

// ---------------------------------------------------------------- analisa
$("#btn-rerun").addEventListener("click", async () => {
  if (!current) return;
  try {
    const a = await api(`/api/analyses/${current.id}/rerun`, { method: "POST" });
    await watchJob(a.id);
  } catch (err) { toast(err.message, 7000); }
});

$("#btn-analyze").addEventListener("click", async () => {
  if (!aoiGeom) return toast("Pilih wilayah contoh atau gambar area di peta terlebih dahulu.");
  const body = {
    name: $("#an-name").value || null,
    geometry: aoiGeom,
    source: $("#source").value,
    before: [$("#b-start").value, $("#b-end").value],
    after: [$("#a-start").value, $("#a-end").value],
  };
  $("#btn-analyze").disabled = true;
  try {
    const a = await api("/api/analyses", { method: "POST", body: JSON.stringify(body) });
    await watchJob(a.id);
  } catch (err) {
    toast(err.message, 7000);
  } finally {
    $("#btn-analyze").disabled = false;
  }
});

let watchingJob = null;
async function watchJob(id) {
  watchingJob = id;
  $("#job").hidden = false;
  $("#btn-job-cancel").hidden = false;
  for (;;) {
    if (watchingJob !== id) return;  // pengguna beralih memantau analisa lain
    const a = await api(`/api/analyses/${id}`);
    $("#job-bar").style.width = `${Math.round(a.progress * 100)}%`;
    $("#job-msg").textContent = `#${id} ${a.name}: ${a.message || a.status}`;
    if (a.status === "done") { await showAnalysis(id); setTimeout(() => ($("#job").hidden = true), 1500); return; }
    if (a.status === "error") { toast(`Analisa gagal: ${a.message}`, 9000); $("#btn-job-cancel").hidden = true; return; }
    if (a.status === "cancelled") { toast(`Analisa #${id} dibatalkan.`); $("#job").hidden = true; return; }
    await new Promise((r) => setTimeout(r, 1500));
  }
}
$("#btn-job-cancel").addEventListener("click", async () => {
  if (!watchingJob || !confirm(`Batalkan analisa #${watchingJob}?`)) return;
  try {
    const r = await api(`/api/analyses/${watchingJob}/cancel`, { method: "POST" });
    toast(r.state === "cancelling" ? "Membatalkan… analisa berhenti di tahap berikutnya." : "Analisa dibatalkan.");
  } catch (err) { toast(err.message, 7000); }
});

async function showAnalysis(id, opts = {}) {
  current = await api(`/api/analyses/${id}`);
  if (current.status !== "done") throw new Error(`Analisa #${id} belum selesai (${current.status})`);
  detFC = await api(`/api/analyses/${id}/detections`);
  saveUiState({ lastAnalysis: id });
  if (opts.switchModule === false) document.querySelector('#tabs button[data-tab="analisa"]').click();
  else openSatTab("analisa");
  if (current.geometry) setAoi(current.geometry, false);

  const s = current.summary;
  $("#result").hidden = false;
  $("#res-title").textContent = current.name;
  const sc = s.scenes || {};
  $("#res-meta").innerHTML =
    `${esc(META.sources[current.source] ?? current.source)} · sebelum ${current.before_start}…${current.before_end} (${sc.before?.n ?? "?"} akuisisi) · ` +
    `sesudah ${current.after_start}…${current.after_end} (${sc.after?.n ?? "?"} akuisisi) · cakupan valid ${fmtPct(s.valid_fraction)}` +
    (s.hotspots ? ` · ${s.hotspots} titik panas FIRMS` : "") +
    (s.synthetic ? `<br><b>Mode demo:</b> data sintetis 8×8 km di pusat AOI.` : "");
  const srcIcon = { ok: "✓", error: "✗", off: "–" };
  $("#res-sources").innerHTML = s.data_sources
    ? `<div class="muted" style="margin-top:6px">Sumber data:</div><div class="src-chips">` +
      Object.values(s.data_sources).map((d) => `<span class="src-chip ${d.status}" title="${esc(d.detail ?? "")}">${srcIcon[d.status] ?? "?"} ${esc(d.label)}</span>`).join("") +
      `</div>`
    : `<p class="muted">Analisa lama tanpa data pendukung — klik "Analisa ulang" untuk menambahkan radar, WorldCover, JRC & OSM.</p>`;
  $("#st-human").textContent = fmtHa(s.by_category.manusia.area_ha);
  $("#st-natural").textContent = fmtHa(s.by_category.alami.area_ha);
  $("#st-verify").textContent = fmtHa(s.by_category.perlu_verifikasi.area_ha);
  $("#st-pct").textContent = `${s.change_pct_of_forest.toLocaleString("id-ID", { maximumFractionDigits: 2 })}%`;
  $("#st-pct").title = `${fmtHa(s.change_ha)} dari ${fmtHa(s.forest_before_ha)} hutan`;

  const maxA = Math.max(1e-9, ...Object.values(s.by_class).map((c) => c.area_ha));
  $("#class-bars").innerHTML = Object.entries(s.by_class)
    .filter(([, c]) => c.count)
    .sort((a, b) => b[1].area_ha - a[1].area_ha)
    .map(([k, c]) => `<div class="bar-row"><span><span class="swatch" style="background:${META.classes[k].color}"></span>${esc(c.label)}</span>
      <div class="track"><div class="fill" style="width:${(100 * c.area_ha) / maxA}%;background:${META.classes[k].color}"></div></div>
      <span class="val">${fmtHa(c.area_ha)} · ${c.count}</span></div>`).join("") || `<p class="muted">Tidak ada perubahan signifikan.</p>`;

  $("#dl-geojson").href = `/api/analyses/${id}/export.geojson`;
  $("#dl-csv").href = `/api/analyses/${id}/export.csv`;
  $("#dl-inputs").hidden = !current.inputs;
  if (current.inputs) {
    $("#dl-inputs").innerHTML = `Data citra tersimpan (GeoTIFF): <a href="${current.inputs.before}">sebelum</a> · ` +
      `<a href="${current.inputs.after}">sesudah</a> · <a href="${current.inputs.dem}">DEM</a>`;
  }

  drawProducts();
  drawDetections();
  renderDetList();
}

function drawProducts() {
  [imgBefore, imgAfter, imgChanges].forEach((l) => l && map.removeLayer(l));
  imgBefore = imgAfter = imgChanges = null;
  const p = current.products;
  if (!p) return;
  const op = +$("#lyr-opacity").value;
  imgBefore = L.imageOverlay(p.before, p.bounds, { opacity: op });
  imgAfter = L.imageOverlay(p.after, p.bounds, { opacity: op });
  imgChanges = L.imageOverlay(p.changes, p.bounds, { opacity: 0.85 });
  $("#an-layers").hidden = false;
  applyLayerToggles();
  map.fitBounds(p.bounds);
}

function applyLayerToggles() {
  const mode = document.querySelector('input[name="img"]:checked').value;
  for (const [l, on] of [[imgBefore, mode === "before"], [imgAfter, mode === "after"], [imgChanges, $("#lyr-changes").checked]]) {
    if (!l) continue;
    on ? l.addTo(map) : map.removeLayer(l);
  }
  $("#lyr-polys").checked ? detLayer.addTo(map) : map.removeLayer(detLayer);
  detLayer.bringToFront();
}
document.querySelectorAll('input[name="img"]').forEach((r) => r.addEventListener("change", applyLayerToggles));
$("#lyr-changes").addEventListener("change", applyLayerToggles);
$("#lyr-polys").addEventListener("change", applyLayerToggles);
$("#lyr-opacity").addEventListener("input", (e) => [imgBefore, imgAfter].forEach((l) => l && l.setOpacity(+e.target.value)));

function filteredFeatures() {
  const cat = $("#flt-cat").value, cls = $("#flt-class").value;
  return detFC.features.filter((f) => (!cat || f.properties.category === cat) && (!cls || f.properties.class === cls));
}

function drawDetections() {
  detLayer.clearLayers();
  layerById.clear();
  for (const f of filteredFeatures()) {
    const color = META.classes[f.properties.class].color;
    const dash = f.properties.category === "perlu_verifikasi" ? "5 4" : null;
    const l = L.geoJSON(f, { style: { color, weight: 2, fillColor: color, fillOpacity: 0.25, dashArray: dash } });
    l.bindPopup(() => popupHtml(f.properties), { maxWidth: 380 });
    l.addTo(detLayer);
    layerById.set(f.id, l);
  }
  if (typeof onDetectionsChanged === "function") onDetectionsChanged();
}

function renderDetList() {
  const feats = filteredFeatures();
  $("#det-list").innerHTML = feats.map((f) => {
    const p = f.properties;
    return `<li data-id="${f.id}"><div class="top"><span><span class="swatch" style="background:${META.classes[p.class].color}"></span><b>${esc(p.class_label)}</b></span>
      <span class="badge ${p.category}">${esc(p.category_label)}</span></div>
      <div class="muted">${fmtHa(p.area_ha)} · keyakinan ${fmtPct(p.confidence)} · skor manusia ${fmtPct(p.human_score)}</div></li>`;
  }).join("") || `<li class="muted">Tidak ada data.</li>`;
}
$("#det-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  // Klik item daftar → buka inset fokus & perbesaran objek (lihat focus.js).
  if (typeof openFocus === "function") return openFocus(+li.dataset.id);
  const l = layerById.get(+li.dataset.id);
  if (l) { map.fitBounds(l.getBounds(), { maxZoom: 16, padding: [40, 40] }); l.getLayers()[0].openPopup(); }
});
["#flt-cat", "#flt-class"].forEach((s) => $(s).addEventListener("change", () => { drawDetections(); renderDetList(); }));

const FEATURE_LABELS = [
  ["area_ha", "Luas (ha)", 2], ["ndvi_before", "NDVI sebelum", 2], ["ndvi_after", "NDVI sesudah", 2], ["dnbr", "dNBR", 2],
  ["bsi_after", "Indeks tanah terbuka (BSI)", 2], ["water_frac", "Fraksi air", 2], ["ndti_water", "Kekeruhan air (NDTI)", 2],
  ["ferric_after", "Rasio merah/biru", 2], ["albedo_cv", "Heterogenitas permukaan", 2],
  ["ring_nonforest", "Non-hutan di sekeliling (sebelum)", 2], ["slope_mean", "Lereng rata-rata (°)", 1], ["elev_mean", "Elevasi (m)", 0],
  ["rectangularity", "Rektangularitas", 2], ["elongation", "Elongasi", 1], ["width_m", "Lebar efektif (m)", 0],
  ["downslope_alignment", "Keselarasan arah lereng", 2], ["dist_water_m", "Jarak ke sungai (m)", 0],
  ["n_neighbors_1500m", "Patch tetangga (1,5 km)", 0], ["hotspots", "Titik panas FIRMS", 0],
  ["dvh_db", "Perubahan radar Sentinel-1 VH (dB)", 1], ["wc_tree_frac", "Hutan menurut WorldCover 2021", 2],
  ["wc_built_ring", "Lahan terbangun di sekitar (WorldCover)", 2], ["new_water_frac", "Genangan baru (JRC)", 2],
  ["gsw_water_occ", "Riwayat genangan JRC (% waktu)", 0], ["osm_mining_frac", "Tumpang-tindih tambang OSM", 2],
  ["osm_built_ring", "Permukiman/industri OSM di sekitar", 2], ["road_dist_m", "Jarak ke jalan OSM (m)", 0],
];

function popupHtml(p) {
  const f = p.features;
  const probs = Object.entries(p.probs).sort((a, b) => b[1] - a[1]).slice(0, 4)
    .map(([k, v]) => `<tr><td><span class="swatch" style="background:${META.classes[k].color}"></span>${esc(META.classes[k].label)}</td><td>${fmtPct(v)}</td></tr>`).join("");
  const rows = FEATURE_LABELS.filter(([k]) => f[k] !== null && f[k] !== undefined)
    .map(([k, label, nd]) => `<tr><td>${label}</td><td>${(+f[k]).toLocaleString("id-ID", { maximumFractionDigits: nd, minimumFractionDigits: nd })}</td></tr>`).join("");
  return `<div class="pop">
    <h3><span class="swatch" style="background:${META.classes[p.class].color}"></span>${esc(p.class_label)}</h3>
    <button class="primary small" data-focus="${p.id}" style="float:right;margin-top:-26px">🔍 Fokus & perbesar</button>
    <span class="badge ${p.category}">${esc(p.category_label)}</span>
    <span class="muted"> ${fmtHa(p.area_ha)} · keyakinan ${fmtPct(p.confidence)}</span>
    <div style="margin-top:8px;font-size:12px">Skor aktivitas manusia: <b>${fmtPct(p.human_score)}</b></div>
    <div class="meter"><i style="left:calc(${p.human_score * 100}% - 1px)"></i></div>
    <div class="muted" style="display:flex;justify-content:space-between"><span>alami</span><span>manusia</span></div>
    <b>Bukti utama</b><ul>${p.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${p.notes.map((n) => `<div class="note">⚠ ${esc(n)}</div>`).join("")}
    <details><summary>Probabilitas kelas</summary><table>${probs}</table></details>
    <details><summary>Indikator terukur</summary><table>${rows}</table></details>
    <div class="muted" style="margin-top:6px">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} ·
      <a href="https://www.google.com/maps/@${p.lat},${p.lon},1500m/data=!3m1!1e3" target="_blank" rel="noopener">buka di Google Maps</a></div>
  </div>`;
}

// ---------------------------------------------------------------- pantauan (AOI)
$("#btn-save-aoi").addEventListener("click", async () => {
  if (!aoiGeom) return toast("Pilih atau gambar area terlebih dahulu.");
  const name = $("#an-name").value || prompt("Nama area pantauan:");
  if (!name) return;
  const interval = +(prompt("Interval pemantauan otomatis (hari):", "30") || 30);
  try {
    await api("/api/aois", {
      method: "POST",
      body: JSON.stringify({ name, geometry: aoiGeom, source: $("#source").value, interval_days: interval }),
    });
    toast(`Area "${name}" disimpan. Bila analisa otomatis aktif, pemantauan pertama berjalan dalam beberapa menit; atau klik "Jalankan" di tab Pantauan.`);
  } catch (err) { toast(err.message, 7000); }
});

async function loadAois() {
  const aois = await api("/api/aois");
  $("#aoi-list").innerHTML = aois.map((a) => {
    const s = a.last_analysis?.summary;
    const last = a.last_analysis
      ? `Terakhir: ${statusBadge(a.last_analysis.status)}${a.last_analysis.trigger === "auto" ? " otomatis" : ""} ${s ? `· manusia ${fmtHa(s.by_category.manusia.area_ha)} · alami ${fmtHa(s.by_category.alami.area_ha)}` : ""}`
      : "Belum pernah dianalisa";
    return `<li data-id="${a.id}"><div class="top" style="display:flex;justify-content:space-between;gap:6px"><b>${esc(a.name)}</b>
      <span class="badge ${a.active ? "done" : "rendah"}">${a.active ? `otomatis tiap ${a.interval_days} hari` : "pantau otomatis nonaktif"}</span></div>
      <div class="muted">${last} · ${a.runs_done} run selesai${a.last_run_at ? ` · dijalankan ${fmtTime(a.last_run_at)}` : ""}</div>
      ${sparkline(a.history)}
      <div class="row">
        <button class="ghost small" data-act="show">Lihat</button>
        <button class="ghost small" data-act="run">Jalankan sekarang</button>
        <button class="ghost small" data-act="toggle">${a.active ? "Hentikan pantau otomatis" : "Mulai pantau otomatis"}</button>
        <button class="ghost small" data-act="del">Hapus</button>
      </div></li>`;
  }).join("") || `<li class="muted">Belum ada area pantauan. Pilih area di tab Analisa lalu klik "Simpan sebagai pantauan".</li>`;
  $("#aoi-list")._aois = aois;
}
$("#aoi-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  const aoi = $("#aoi-list")._aois.find((a) => a.id === +li.dataset.id);
  const act = btn?.dataset.act ?? "focus";
  const bar = e.target.closest("[data-analysis]");
  if (bar) return showAnalysis(+bar.dataset.analysis).catch((err) => toast(err.message));
  try {
    if (act === "focus") setAoi(aoi.geometry);
    if (act === "show") {
      if (aoi.last_analysis?.status === "done") await showAnalysis(aoi.last_analysis.id);
      else { setAoi(aoi.geometry); toast("Belum ada hasil analisa selesai untuk area ini."); }
    }
    if (act === "run") {
      const a = await api(`/api/aois/${aoi.id}/run`, { method: "POST" });
      openSatTab("analisa");
      setAoi(aoi.geometry);
      await watchJob(a.id);
    }
    if (act === "toggle") {
      await api(`/api/aois/${aoi.id}`, { method: "PATCH", body: JSON.stringify({ active: !aoi.active }) });
      toast(aoi.active ? `Pantau otomatis "${aoi.name}" dihentikan.` : `Pantau otomatis "${aoi.name}" dimulai.`);
      loadAois(); loadMonitor();
    }
    if (act === "del" && confirm(`Hapus area pantauan "${aoi.name}"?`)) { await api(`/api/aois/${aoi.id}`, { method: "DELETE" }); loadAois(); }
  } catch (err) { toast(err.message, 7000); }
});

// ---------------------------------------------------------------- analisa otomatis
async function loadMonitor() {
  const [m, st] = await Promise.all([api("/api/monitor"), api("/api/stats")]);
  $("#auto-badge").className = `badge ${m.enabled ? "done" : "rendah"}`;
  $("#auto-badge").textContent = m.enabled ? "● Aktif" : "■ Berhenti";
  $("#btn-auto-start").hidden = m.enabled;
  $("#btn-auto-stop").hidden = !m.enabled;
  $("#btn-auto-check").hidden = !m.enabled;
  $("#auto-cancel").closest("label").hidden = !m.enabled;
  $("#auto-info").innerHTML = m.enabled
    ? `${m.active_aois} area aktif dipantau · cek tiap ${m.tick_minutes} menit (berikutnya ${fmtTime(m.next_tick)}) · ` +
      `${m.due_aois} area jatuh tempo · jadwal terdekat ${fmtTime(m.next_due)} · maks ${m.workers} analisa paralel` +
      (m.scheduler_running ? "" : `<br><b style="color:var(--human)">Penjadwal server tidak berjalan (GEOINT_SCHEDULER=0).</b>`)
    : `Analisa otomatis berhenti. Area pantauan tidak dianalisa ulang sampai dimulai lagi (analisa manual tetap bisa). Terakhir dicek ${fmtTime(m.last_tick)}.`;
  $("#auto-running").innerHTML = m.running.map((r) => `<li data-job="${r.id}">
      <div style="display:flex;justify-content:space-between;gap:6px"><span>#${r.id} ${esc(r.name)}</span>${statusBadge(r.status)}</div>
      <div class="progress"><div style="width:${Math.round(r.progress * 100)}%;height:100%;background:var(--accent)"></div></div>
      <div class="row"><span class="muted">${r.trigger === "auto" ? "otomatis" : "manual"}</span><button class="ghost small" data-cancel="${r.id}">Batalkan</button></div></li>`).join("");
  const tile = (label, v) => `<div class="stat"><span>${label}</span><b>${(v ?? 0).toLocaleString("id-ID")}</b></div>`;
  $("#acc-stats").innerHTML = tile("Analisa selesai", st.analyses_done) + tile("Di antaranya otomatis", st.analyses_auto) +
    tile("Area perubahan tercatat", st.detections) + tile("Area pantauan", st.aois);
  $("#acc-meta").textContent = st.first_analysis ? `Data dikumpulkan sejak ${fmtTime(st.first_analysis)}.` : "";
}
$("#btn-auto-start").addEventListener("click", async () => {
  try {
    const r = await api("/api/monitor/start", { method: "POST" });
    toast(`Analisa otomatis dimulai${r.started_analyses.length ? ` — ${r.started_analyses.length} area langsung dianalisa` : ""}.`);
    loadMonitor(); loadAois();
  } catch (err) { toast(err.message, 7000); }
});
$("#btn-auto-stop").addEventListener("click", async () => {
  const cancel = $("#auto-cancel").checked;
  if (!confirm(`Hentikan analisa otomatis?${cancel ? "\nAnalisa otomatis yang antri/berjalan juga dibatalkan." : ""}`)) return;
  try {
    const r = await api(`/api/monitor/stop?cancel_pending=${cancel}`, { method: "POST" });
    const n = r.cancelled.length + r.cancelling.length;
    toast(`Analisa otomatis dihentikan${n ? `; ${n} analisa dibatalkan` : ""}.`);
    loadMonitor(); loadAois();
  } catch (err) { toast(err.message, 7000); }
});
$("#btn-auto-check").addEventListener("click", async () => {
  try {
    const r = await api("/api/monitor/check", { method: "POST" });
    toast(r.started_analyses.length ? `${r.started_analyses.length} area mulai dianalisa.` : "Tidak ada area yang jatuh tempo.");
    loadMonitor(); loadAois();
  } catch (err) { toast(err.message, 7000); }
});
$("#auto-running").addEventListener("click", async (e) => {
  const cx = e.target.closest("button[data-cancel]");
  if (cx) {
    try { await api(`/api/analyses/${cx.dataset.cancel}/cancel`, { method: "POST" }); } catch (err) { toast(err.message, 7000); }
    return loadMonitor();
  }
  const li = e.target.closest("li[data-job]");
  if (li) { openSatTab("analisa"); watchJob(+li.dataset.job); }
});
// Segarkan status selama tab Pantauan terbuka.
setInterval(() => {
  if ($("#tab-pantauan").classList.contains("active") && !document.hidden) { loadMonitor().catch(() => {}); loadAois().catch(() => {}); }
}, 10000);

// Sparkline batang: luas aktivitas manusia per run (satu seri -> tanpa legenda; judul menamainya).
function sparkline(hist) {
  if (!hist?.length) return "";
  const W = 260, H = 40, gap = 2, n = hist.length;
  const bw = Math.min(16, (W - gap * (n - 1)) / n);
  const max = Math.max(...hist.map((h) => h.human_ha), 1e-9);
  const bars = hist.map((h, i) => {
    const x = i * (bw + gap);
    const bh = h.human_ha > 0 ? Math.max(2, (h.human_ha / max) * (H - 4)) : 0;
    const y = H - bh, r = Math.min(2, bw / 2, bh);
    const path = bh ? `M${x},${H}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${H}Z` : "";
    const tip = `${h.after_start} … ${h.after_end} (${h.trigger === "auto" ? "otomatis" : "manual"})\nManusia ${fmtHa(h.human_ha)} · alami ${fmtHa(h.natural_ha)} · verifikasi ${fmtHa(h.verify_ha)}\nKlik untuk membuka`;
    return `<g class="spark-bar" data-analysis="${h.id}"><title>${esc(tip)}</title>
      <rect x="${x - gap / 2}" y="0" width="${bw + gap}" height="${H}" fill="transparent"/>${path ? `<path d="${path}"/>` : `<rect x="${x}" y="${H - 1}" width="${bw}" height="1" class="zero"/>`}</g>`;
  }).join("");
  const lastH = hist[hist.length - 1].human_ha;
  return `<div class="spark"><div class="muted">Aktivitas manusia per run — terakhir ${fmtHa(lastH)}, tertinggi ${fmtHa(max)} (${n} run)</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="xMinYMax meet" role="img"
      aria-label="Luas aktivitas manusia per run pemantauan">${bars}<line x1="0" x2="${W}" y1="${H - 0.5}" y2="${H - 0.5}" class="base"/></svg></div>`;
}

// ---------------------------------------------------------------- alert
async function loadAlertCount() {
  try {
    const n = (await api("/api/alerts")).length;
    $("#alert-count").hidden = !n;
    $("#alert-count").textContent = n;
  } catch { /* server mungkin sedang restart */ }
}
async function loadAlerts() {
  const dom = $("#alert-domain").value;
  const alerts = (await api(`/api/alerts?include_ack=${$("#alert-all").checked}&limit=300`)).filter((a) => !dom || (a.domain || "sat") === dom);
  $("#alert-list").innerHTML = alerts.map((a) => `<li data-analysis="${a.analysis_id ?? ""}" data-domain="${a.domain || "sat"}" style="${a.acknowledged ? "opacity:.6" : ""}">
    <div style="display:flex;justify-content:space-between;gap:6px"><b>${esc(a.title)}</b><span class="badge ${a.severity}">${a.severity}</span></div>
    <div><span class="dom-chip" style="--dc:var(--${a.domain || "sat"})">${(a.domain || "sat").toUpperCase()}</span></div>
    <div class="muted">${esc(a.message)}</div>
    <div class="muted">${new Date(a.created_at).toLocaleString("id-ID")}</div>
    ${a.acknowledged ? "" : `<div class="row"><button class="ghost small" data-ack="${a.id}">Tandai sudah ditinjau</button></div>`}</li>`).join("")
    || `<li class="muted">Tidak ada alert.</li>`;
  loadAlertCount();
}
$("#alert-all").addEventListener("change", loadAlerts);
$("#alert-domain").addEventListener("change", loadAlerts);
$("#alert-list").addEventListener("click", async (e) => {
  const ack = e.target.closest("button[data-ack]");
  if (ack) { await api(`/api/alerts/${ack.dataset.ack}/ack`, { method: "POST" }); return loadAlerts(); }
  const li = e.target.closest("li[data-analysis]");
  if (!li) return;
  if (li.dataset.analysis) showAnalysis(+li.dataset.analysis).catch((err) => toast(err.message));
  else if (typeof showModule === "function") showModule(li.dataset.domain);  // alert domain tanpa analisa
});

// ---------------------------------------------------------------- riwayat
async function loadHistory() {
  const rows = await api("/api/analyses");
  $("#hist-list").innerHTML = rows.map((a) => `<li data-id="${a.id}" data-status="${a.status}">
    <div style="display:flex;justify-content:space-between;gap:6px"><b>#${a.id} ${esc(a.name)}</b>${statusBadge(a.status)}</div>
    <div class="muted">${a.before_start}…${a.before_end} → ${a.after_start}…${a.after_end} · ${esc(a.source)} · ${a.trigger === "auto" ? "otomatis" : "manual"}</div>
    ${a.summary ? `<div class="muted">${a.summary.n_detections} area · manusia ${fmtHa(a.summary.by_category.manusia.area_ha)} · alami ${fmtHa(a.summary.by_category.alami.area_ha)}</div>` : ""}
    ${a.status === "error" ? `<div class="muted" style="color:var(--human)">${esc(a.message)}</div>` : ""}
    <div class="row">${["done", "error", "cancelled"].includes(a.status) ? `<button class="ghost small" data-rerun="${a.id}">Analisa ulang</button>` : ""}
      ${["queued", "running"].includes(a.status) ? `<button class="ghost small" data-cancel="${a.id}">Batalkan</button>` : ""}
      <button class="ghost small" data-del="${a.id}">Hapus</button></div></li>`).join("")
    || `<li class="muted">Belum ada analisa.</li>`;
}
$("#hist-list").addEventListener("click", async (e) => {
  const cx = e.target.closest("button[data-cancel]");
  if (cx) {
    try { await api(`/api/analyses/${cx.dataset.cancel}/cancel`, { method: "POST" }); } catch (err) { toast(err.message, 7000); }
    return loadHistory();
  }
  const rr = e.target.closest("button[data-rerun]");
  if (rr) {
    try {
      const a = await api(`/api/analyses/${rr.dataset.rerun}/rerun`, { method: "POST" });
      openSatTab("analisa");
      await watchJob(a.id);
    } catch (err) { toast(err.message, 7000); }
    return;
  }
  const del = e.target.closest("button[data-del]");
  if (del) {
    if (confirm("Hapus analisa ini beserta citra tersimpannya?")) {
      try { await api(`/api/analyses/${del.dataset.del}`, { method: "DELETE" }); } catch (err) { toast(err.message, 7000); }
      loadHistory();
    }
    return;
  }
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  if (li.dataset.status === "done") showAnalysis(+li.dataset.id).catch((err) => toast(err.message));
  else if (["queued", "running"].includes(li.dataset.status)) { openSatTab("analisa"); watchJob(+li.dataset.id); }
});

// ---------------------------------------------------------------- log aktivitas
const ACT_ICONS = {
  server_start: "⏻", server_stop: "⏼", analisa_dibuat: "＋", analisa_mulai: "▶", analisa_selesai: "✓",
  analisa_gagal: "✗", analisa_dilanjutkan: "↻", analisa_diulang: "↻", analisa_dihapus: "🗑", pantauan_dibuat: "＋",
  pantauan_diubah: "✎", pantauan_dihapus: "🗑", pantauan_dijalankan: "▶", alert: "⚠", alert_ditinjau: "✓",
  analisa_dibatalkan: "⨯", otomatis_mulai: "▶", otomatis_berhenti: "■", pantauan_gagal: "✗",
};
let actOldest = null;
async function loadActivity(more = false) {
  const q = more && actOldest ? `&before_id=${actOldest}` : "";
  const rows = await api(`/api/activity?limit=100${q}`);
  const html = rows.map((r) => `<li ${r.analysis_id ? `data-analysis="${r.analysis_id}"` : ""} style="cursor:${r.analysis_id ? "pointer" : "default"}">
    <div><b>${ACT_ICONS[r.kind] ?? "•"}</b> ${esc(r.message)}</div>
    <div class="muted">${new Date(r.created_at).toLocaleString("id-ID")} · ${esc(r.kind)}</div></li>`).join("");
  $("#act-list").innerHTML = more ? $("#act-list").innerHTML + html : html || `<li class="muted">Belum ada aktivitas.</li>`;
  if (rows.length) actOldest = rows[rows.length - 1].id;
  $("#act-more").hidden = rows.length < 100;
}
$("#act-more").addEventListener("click", () => loadActivity(true));
$("#act-list").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-analysis]");
  if (li) showAnalysis(+li.dataset.analysis).catch((err) => toast(err.message));
});

init().catch((err) => toast(`Gagal memuat: ${err.message}`, 10000));
