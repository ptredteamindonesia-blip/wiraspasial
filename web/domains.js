/* WIRA-SPASIAL — enam domain, satu peta nasional.
 * Rail modul, beranda, lapisan peta nasional, modul DIS/SEA/MNE/INF/OS, dan Pengaturan.
 * Memakai global dari app.js: map, api, accessToken, $, esc, fmtHa, fmtPct, fmtTime, toast, statusBadge,
 * showAnalysis, openSatTab, loadAlerts, loadActivity, loadAlertCount, loadUiState, saveUiState, META.
 */
"use strict";

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtNum = (v, d = 0) => (v ?? 0).toLocaleString("id-ID", { maximumFractionDigits: d, minimumFractionDigits: d });
const ago = (iso) => {
  if (!iso) return "–";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  if (m < 1) return "baru saja";
  if (m < 60) return `${Math.round(m)} mnt lalu`;
  if (m < 1440) return `${Math.round(m / 60)} jam lalu`;
  return `${Math.round(m / 1440)} hari lalu`;
};
const accessBadge = (a) => `<span class="badge access ${a}">${esc(a)}</span>`;
const domChip = (d) => `<span class="dom-chip" style="--dc:var(--${d})">${d.toUpperCase()}</span>`;

async function apiRaw(path, body, contentType = "application/octet-stream") {
  const headers = { "Content-Type": contentType };
  const tok = accessToken();
  if (tok) headers["X-Wira-Token"] = tok;
  const res = await fetch(path, { method: "POST", body, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data));
  return data;
}

// Batang mini satu seri (tanpa legenda; judul kartu menamai serinya). Tooltip per batang via <title>.
function miniBars(values, titles, { height = 34, width = 260, color = "var(--accent)" } = {}) {
  if (!values.length) return "";
  const n = values.length, gap = 2, bw = Math.min(14, (width - gap * (n - 1)) / n);
  const max = Math.max(...values, 1e-9);
  const bars = values.map((v, i) => {
    const x = i * (bw + gap), bh = v > 0 ? Math.max(2, (v / max) * (height - 3)) : 0, y = height - bh, r = Math.min(2, bw / 2, bh);
    const path = bh ? `<path d="M${x},${height}V${y + r}Q${x},${y} ${x + r},${y}H${x + bw - r}Q${x + bw},${y} ${x + bw},${y + r}V${height}Z"/>` : "";
    return `<g class="b"><title>${esc(titles[i])}</title><rect x="${x - 1}" y="0" width="${bw + 2}" height="${height}" fill="transparent"/>${path}</g>`;
  }).join("");
  return `<svg class="minibars" style="--dc:${color}" viewBox="0 0 ${width} ${height}" width="100%" height="${height}"
    preserveAspectRatio="xMinYMax meet" role="img">${bars}<line class="base" x1="0" x2="${width}" y1="${height - .5}" y2="${height - .5}"/></svg>`;
}
const hbar = (label, v, max, txt, color) =>
  `<div class="hbar" style="--dc:${color}"><span title="${esc(label)}">${esc(label)}</span><div class="track"><div class="fill" style="width:${max ? (100 * v) / max : 0}%"></div></div><span class="v">${txt}</span></div>`;

// ---------------------------------------------------------------- modul
const MODULE_LAYERS = { dis: ["earthquakes", "hotspots"], sea: ["ports", "ships"], mne: ["detections", "permits"], inf: ["aois", "detections"], os: ["news"], sat: ["aois"] };
const LOADERS = {};

function showModule(id) {
  if (!document.getElementById(`mod-${id}`)) id = "home";
  document.querySelectorAll("#rail button[data-mod]").forEach((b) => b.classList.toggle("active", b.dataset.mod === id));
  document.querySelectorAll("#sidebar .mod").forEach((m) => m.classList.toggle("active", m.id === `mod-${id}`));
  saveUiState({ module: id });
  $("#sidebar").scrollTop = 0;
  if (MODULE_LAYERS[id]) MODULE_LAYERS[id].forEach((l) => natOn.add(l)), renderNatToggles(), refreshNational();
  const fn = LOADERS[id];
  if (fn) Promise.resolve(fn()).catch((err) => toast(err.message, 7000));
}
document.querySelectorAll("#rail button[data-mod]").forEach((b) => b.addEventListener("click", () => showModule(b.dataset.mod)));
LOADERS.alert = () => loadAlerts();
LOADERS.aktivitas = () => loadActivity();

// Tombol "↻" feed manual di semua modul.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-run]");
  if (!b) return;
  b.disabled = true;
  try {
    await api(`/api/sources/${b.dataset.run}/run`, { method: "POST" });
    toast("Feed dijalankan di latar belakang — data akan muncul dalam beberapa saat.");
    setTimeout(() => { const m = loadUiState().module; LOADERS[m]?.(); refreshNational(); }, 15000);
  } catch (err) { toast(err.message, 7000); }
  finally { setTimeout(() => (b.disabled = false), 3000); }
});

// ---------------------------------------------------------------- peta nasional
const NAT = {
  earthquakes: { label: "Gempa", dom: "dis" },
  hotspots: { label: "Hotspot (≤3 hari)", dom: "dis" },
  ports: { label: "Pelabuhan", dom: "sea" },
  ships: { label: "Kapal radar", dom: "sea" },
  detections: { label: "Deteksi terbaru", dom: "sat" },
  aois: { label: "Area pantauan", dom: "sat" },
  permits: { label: "Izin & kawasan", dom: "mne" },
  news: { label: "Berita tergeolokasi", dom: "os" },
};
const natOn = new Set(loadUiState().natLayers || ["earthquakes", "ports", "aois", "news"]);
const natGroups = {};
const natCanvas = L.canvas({ padding: 0.5 });
let natData = {};

function renderNatToggles() {
  $("#nat-layers").innerHTML = Object.entries(NAT).map(([k, v]) => `<label><input type="checkbox" data-nat="${k}" ${natOn.has(k) ? "checked" : ""}/>
    <span class="swatch" style="background:var(--${v.dom})"></span>${esc(v.label)}</label>`).join("");
  $("#mne-show").checked = natOn.has("permits");
}
$("#nat-layers").addEventListener("change", (e) => {
  const k = e.target.dataset.nat;
  if (!k) return;
  e.target.checked ? natOn.add(k) : natOn.delete(k);
  saveUiState({ natLayers: [...natOn] });
  refreshNational();
});
$("#nat-days").addEventListener("change", refreshNational);

const popBtn = (attr, id, label) => `<button class="ghost small" ${attr}="${id}">${label}</button>`;
const BUILDERS = {
  earthquakes: (fc) => L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { renderer: natCanvas, radius: Math.max(3, 3 + (f.properties.mag - 3) * 2.4),
      color: cssVar("--dis"), weight: 1.5, fillOpacity: 0.35 }),
    onEachFeature: (f, l) => l.bindPopup(`<div class="pop"><b>${esc(f.properties.title)}</b><div class="muted">${fmtTime(f.properties.time)} ·
      kedalaman ${fmtNum(f.properties.depth)} km · ${esc(f.properties.source.toUpperCase())}</div>
      ${f.properties.potensi ? `<div>${esc(f.properties.potensi)}</div>` : ""}${popBtn("data-eq", f.properties.id, "Paparan risiko")}</div>`),
  }),
  hotspots: (fc) => L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { renderer: natCanvas, radius: 2.5, color: "#ff7043", weight: 0, fillOpacity: 0.8 }),
    onEachFeature: (f, l) => l.bindPopup(`<div class="pop"><b>Hotspot VIIRS</b><div>FRP ${fmtNum(f.properties.frp, 1)} MW · keyakinan ${esc(f.properties.conf || "-")}</div>
      <div class="muted">${fmtTime(f.properties.time)}</div></div>`),
  }),
  ports: (fc) => L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 7, color: "#fff", weight: 2, fillColor: cssVar("--sea"), fillOpacity: f.properties.active ? 0.95 : 0.35 }),
    onEachFeature: (f, l) => {
      l.bindTooltip(esc(f.properties.name), { direction: "top", offset: [0, -6] });
      l.bindPopup(`<div class="pop"><b>${esc(f.properties.name)}</b>${f.properties.approx ? ` <span class="muted">(koordinat perkiraan)</span>` : ""}
        <div>${f.properties.vessels == null ? "Belum dipindai" : `${f.properties.vessels} kapal radar (pindaian terakhir)`}</div>${popBtn("data-port", f.properties.id, "Analitik pelabuhan")}</div>`);
    },
  }),
  ships: (fc) => L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { renderer: natCanvas, radius: 2.5, color: cssVar("--sea"), weight: 0, fillOpacity: 0.9 }),
    onEachFeature: (f, l) => l.bindPopup(`<div class="pop"><b>Kapal radar ~${fmtNum(f.properties.length_m)} m</b><div class="muted">${esc(f.properties.port || "")} · ${fmtTime(f.properties.time)}</div></div>`),
  }),
  detections: (fc) => L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { renderer: natCanvas, radius: Math.min(12, 3 + Math.sqrt(f.properties.area_ha)),
      color: META.classes[f.properties.class]?.color || "#999", weight: 1, fillOpacity: 0.7 }),
    onEachFeature: (f, l) => {
      const p = f.properties, c = META.classes[p.class];
      l.bindPopup(`<div class="pop"><b>${esc(c?.label || p.class)}</b> · ${fmtHa(p.area_ha)}<div><span class="badge ${p.category}">${esc(META.categories[p.category] || p.category)}</span>
        ${p.compliance ? ` <span class="badge warn">${esc(p.compliance.replaceAll("_", " "))}</span>` : ""}</div>${popBtn("data-open-det", `${p.analysis_id}:${p.id}`, "Buka & perbesar")}</div>`);
    },
  }),
  aois: (fc) => L.geoJSON(fc, {
    style: (f) => ({ color: cssVar("--sat"), weight: 1.5, dashArray: f.properties.active ? null : "4 4", fill: false }),
    onEachFeature: (f, l) => l.bindTooltip(esc(f.properties.name), { sticky: true }),
  }),
  permits: (fc) => L.geoJSON(fc, {
    style: (f) => ({ color: cssVar("--mne"), weight: 1.2, fillOpacity: f.properties.kind === "izin_tambang" ? 0.12 : 0.05,
      dashArray: f.properties.state === "tidak_aktif" ? "5 4" : null }),
    onEachFeature: (f, l) => {
      const p = f.properties;
      const attrs = Object.entries(p.attrs || {}).slice(0, 8).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
      l.bindPopup(`<div class="pop"><b>${esc(p.label)}</b><div class="muted">${esc(p.layer)} · status ${esc(p.state.replaceAll("_", " "))}</div><table>${attrs}</table></div>`);
    },
  }),
  news: (fc) => L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5 + Math.min(4, f.properties.score || 0), color: "#fff", weight: 1.5,
      fillColor: cssVar("--os"), fillOpacity: 0.9 }),
    onEachFeature: (f, l) => {
      const p = f.properties;
      l.bindPopup(`<div class="pop"><b>${esc(p.title)}</b><div class="muted">${esc(p.publisher || "")} · ${fmtTime(p.time)}</div>
        <div>${(p.topics || []).map((t) => `<span class="dom-chip" style="--dc:var(--os)">${esc(t)}</span>`).join(" ")}</div>
        <div class="muted">Lokasi: ${esc(p.place)} (±${p.precision_km} km)</div>${popBtn("data-news", p.id, "Fusi data")}</div>`);
    },
  }),
};

let natTimer = null;
async function refreshNational() {
  clearTimeout(natTimer);
  const want = [...natOn];
  const days = $("#nat-days").value;
  try {
    natData = want.length ? await api(`/api/map/layers?layers=${want.join(",")}&days=${days}`) : {};
  } catch (err) { return toast(`Lapisan nasional: ${err.message}`); }
  for (const k of Object.keys(NAT)) {
    if (natGroups[k]) { map.removeLayer(natGroups[k]); delete natGroups[k]; }
    if (natOn.has(k) && natData[k]) natGroups[k] = BUILDERS[k](natData[k]).addTo(map);
  }
  natGroups.aois?.bringToBack();
  natTimer = setTimeout(refreshNational, 120000);
}

// Tombol di popup peta.
map.getContainer().addEventListener("click", (e) => {
  const t = e.target.closest("[data-eq],[data-port],[data-news],[data-open-det]");
  if (!t) return;
  map.closePopup();
  if (t.dataset.eq) { showModule("dis"); openExposure(+t.dataset.eq); }
  if (t.dataset.port) { showModule("sea"); openPort(+t.dataset.port); }
  if (t.dataset.news) { showModule("os"); openNews(+t.dataset.news); }
  if (t.dataset.openDet) openDetection(...t.dataset.openDet.split(":").map(Number));
});

async function openDetection(analysisId, detId) {
  await showAnalysis(analysisId);
  if (typeof openFocus === "function") setTimeout(() => openFocus(detId), 400);
}

// ---------------------------------------------------------------- beranda
LOADERS.home = async () => {
  const [doms, mon] = await Promise.all([api("/api/domains"), api("/api/monitor")]);
  $("#dom-cards").innerHTML = doms.map((d) => `<button class="dom-card" data-go="${d.id}" style="--dc:var(--${d.id})">
      <span class="code">${d.code}</span><h3>${esc(d.title.toUpperCase())}</h3><p>${esc(d.desc)}</p>
      <ul>${d.stats.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
      <div class="chips">${d.access.map(accessBadge).join("")}${d.needs_setup.length ? `<span class="badge warn" title="${esc(d.needs_setup.join(", "))}">perlu pengaturan</span>` : ""}</div>
      <span class="bar"></span></button>`).join("");
  $("#home-auto").className = `badge ${mon.enabled ? "done" : "rendah"}`;
  $("#home-auto").textContent = mon.enabled ? "● Otomatis aktif" : "■ Otomatis berhenti";
  $("#home-sys").innerHTML = `${mon.active_aois} area pantauan aktif · ${mon.running.length} analisa berjalan` +
    (mon.running_feeds?.length ? ` · feed berjalan: ${mon.running_feeds.map(esc).join(", ")}` : "") +
    ` · pemeriksaan berikutnya ${fmtTime(mon.next_tick)}`;
};
$("#dom-cards").addEventListener("click", (e) => { const c = e.target.closest("[data-go]"); if (c) showModule(c.dataset.go); });

// ---------------------------------------------------------------- DIS
const exposureLayer = L.layerGroup().addTo(map);
let eqCache = [];
LOADERS.dis = async () => {
  // Ringkasan hotspot lebih berat: dimuat terpisah agar daftar gempa langsung tampil.
  loadHotspotSummary().catch((err) => ($("#hs-summary").innerHTML = `<p class="muted">${esc(err.message)}</p>`));
  const [eqs, src] = await Promise.all([
    api(`/api/dis/earthquakes?days=${$("#eq-days").value}&min_mag=${$("#eq-mag").value}`),
    api("/api/sources"),
  ]);
  eqCache = eqs;
  const st = Object.fromEntries(src.map((s) => [s.id, s]));
  $("#eq-status").textContent = ["bmkg", "usgs", "firms"].map((k) => `${k.toUpperCase()}: ${st[k].status.last_run ? `${st[k].status.ok ? "✓" : "✗"} ${ago(st[k].status.last_run)}` : "belum"}`).join(" · ");
  $("#eq-list").innerHTML = eqs.map((e) => {
    const p = e.properties || {};
    const pct = Math.round(Math.min(100, Math.max(25, (e.value - 3) * 25)));
    return `<li data-eq="${e.id}"><div class="li-top"><span class="mag" style="--p:${pct}%">M${fmtNum(e.value, 1)}</span>
      <div style="flex:1;min-width:0"><b>${esc(p.wilayah || e.title)}</b><div class="muted">${fmtTime(e.event_time)} · ${fmtNum(p.kedalaman_km)} km · ${e.source.toUpperCase()}</div>
      ${p.potensi ? `<div class="muted">${esc(p.potensi)}</div>` : ""}</div></div></li>`;
  }).join("") || `<li class="muted">Tidak ada gempa pada filter ini. Klik BMKG ↻ untuk mengambil data.</li>`;
};
async function loadHotspotSummary() {
  const hs = await api("/api/dis/hotspots/summary?days=1");
  const maxP = Math.max(1, ...hs.by_place.map((p) => p.count));
  $("#hs-summary").innerHTML = `<div class="stats"><div class="stat"><span>Hotspot 24 jam</span><b>${fmtNum(hs.total)}</b></div>
      <div class="stat" title="${esc(hs.persistent_note)}"><span>Berulang (kemungkinan industri)</span><b>${fmtNum(hs.persistent)}</b></div></div>
    <h3>Kab/kota terbanyak (perkiraan)</h3>${hs.by_place.slice(0, 10).map((p) => hbar(p.name, p.count, maxP, fmtNum(p.count), "#ff7043")).join("") || `<p class="muted">Belum ada data.</p>`}
    ${hs.in_aois.length ? `<h3>Di area pantauan</h3>${hs.in_aois.map((a) => `<div>${esc(a.name)} — <b>${a.count}</b> titik, FRP maks ${fmtNum(a.max_frp, 1)} MW</div>`).join("")}` : ""}
    ${hs.in_layers.length ? `<h3>Di lapisan izin/kawasan</h3>${hs.in_layers.slice(0, 8).map((a) => `<div>${esc(a.name)} — <b>${a.count}</b></div>`).join("")}` : ""}
    <p class="hint">${esc(hs.note)}</p>`;
};
["#eq-days", "#eq-mag"].forEach((s) => $(s).addEventListener("change", () => LOADERS.dis()));
$("#eq-list").addEventListener("click", (e) => { const li = e.target.closest("li[data-eq]"); if (li) openExposure(+li.dataset.eq); });

async function openExposure(id) {
  const ex = await api(`/api/dis/earthquakes/${id}/exposure`);
  const ev = eqCache.find((e) => e.id === id) || (await api(`/api/dis/earthquakes?days=365&include_duplicates=true`)).find((e) => e.id === id);
  exposureLayer.clearLayers();
  const ll = [ev.lat, ev.lon];
  const zones = [["IV", 0.06], ["V", 0.1], ["VI", 0.16], ["VII", 0.24]];
  let biggest = null;
  for (const [z, op] of zones) {
    const r = ex.radius_km[z];
    if (!r) continue;
    const c = L.circle(ll, { radius: r * 1000, color: cssVar("--dis"), weight: 1, fillOpacity: op, interactive: false }).addTo(exposureLayer);
    biggest = biggest || c;
  }
  L.circleMarker(ll, { radius: 6, color: "#fff", weight: 2, fillColor: cssVar("--dis"), fillOpacity: 1 }).addTo(exposureLayer)
    .bindTooltip(`${esc(ev.title)}`, { permanent: false });
  biggest ? map.fitBounds(biggest.getBounds(), { padding: [30, 30] }) : map.setView(ll, 8);
  const zoneRow = (z) => (ex.radius_km[z] ? `<span>MMI ${z}</span><span>≤ ${fmtNum(ex.radius_km[z])} km</span>` : "");
  const list = (arr, fmt) => arr.length ? `<ul class="list compact">${arr.map(fmt).join("")}</ul>` : `<p class="muted">Tidak ada.</p>`;
  $("#eq-exposure").hidden = false;
  $("#eq-exposure").innerHTML = `<div class="card-head"><h2>Paparan risiko — ${esc(ev.title)}</h2></div>
    <p class="muted">${fmtTime(ev.event_time)} · kedalaman ${fmtNum(ex.depth_km)} km · estimasi MMI di episenter ${fmtNum(ex.mmi_epicenter, 1)}</p>
    <div class="kv">${zoneRow("VII")}${zoneRow("VI")}${zoneRow("V")}${zoneRow("IV")}</div>
    <h3>Area pantauan (${ex.aois.length})</h3>${list(ex.aois, (a) => `<li>${esc(a.name)} — zona ${a.zona}, ${fmtNum(a.dist_km)} km, MMI≈${fmtNum(a.mmi, 1)}</li>`)}
    ${ex.aois.some((a) => ["VI", "VII+"].includes(a.zona)) ? `<button class="primary small" id="eq-prio" data-id="${id}">Prioritaskan analisa area zona VI+</button>` : ""}
    <h3>Kab/kota (${ex.n_places})</h3>${list(ex.places.slice(0, 25), (p) => `<li>${esc(p.name)} — zona ${p.zona}, ${fmtNum(p.dist_km)} km</li>`)}
    <h3>Pelabuhan</h3>${list(ex.ports, (p) => `<li>${esc(p.name)} — zona ${p.zona}, ${fmtNum(p.dist_km)} km</li>`)}
    <h3>Deteksi perubahan dalam zona VI+</h3>${list(ex.detections.slice(0, 15), (d) => `<li data-open-det="${d.analysis_id}:${d.id}">${esc(META.classes[d.class]?.label || d.class)} ${fmtHa(d.area_ha)} — ${fmtNum(d.dist_km)} km</li>`)}
    <p class="hint">${esc(ex.method)}</p>`;
  $("#eq-exposure").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("#eq-exposure").addEventListener("click", async (e) => {
  const d = e.target.closest("[data-open-det]");
  if (d) return openDetection(...d.dataset.openDet.split(":").map(Number));
  if (e.target.id === "eq-prio") {
    const r = await api(`/api/dis/earthquakes/${e.target.dataset.id}/prioritize`, { method: "POST" });
    toast(`${r.aois.length} area pantauan akan dianalisa pada pemeriksaan otomatis berikutnya.`);
  }
});

// ---------------------------------------------------------------- SEA
const portLayer = L.layerGroup().addTo(map);
LOADERS.sea = async () => {
  const [ports, ais, scans] = await Promise.all([api("/api/sea/ports"), api("/api/sea/ais/status"), api("/api/sea/scans")]);
  $("#port-list").innerHTML = ports.map((p) => {
    const obs = p.observations || [];
    const last = p.latest;
    const trend = p.trend_pct == null ? "" : ` · tren ${p.trend_pct > 0 ? "+" : ""}${fmtNum(p.trend_pct)}%`;
    return `<li data-port="${p.id}" style="${p.active ? "" : "opacity:.55"}"><div class="li-top"><b>${esc(p.name)}${p.approx ? " <span class='muted' title='koordinat perkiraan'>≈</span>" : ""}</b>
      <span>${last ? `<b>${last.vessels}</b> kapal` : `<span class="muted">belum dipindai</span>`}</span></div>
      <div class="muted">${last ? `${fmtTime(last.obs_time)} · rata-rata ${fmtNum(p.mean_vessels)}${trend}` : esc(p.province || "")}${scans.includes(p.id) ? " · ⏳ memindai…" : ""}</div>
      ${obs.length > 1 ? miniBars(obs.map((o) => o.vessels), obs.map((o) => `${fmtTime(o.obs_time)}: ${o.vessels} kapal, ${o.static_targets} target statis`), { color: "var(--sea)", height: 26 }) : ""}
      <div class="row tight" style="margin-top:4px"><button class="ghost small" data-scan="${p.id}">Pindai</button>
        <button class="ghost small" data-toggle-port="${p.id}" data-on="${p.active ? 0 : 1}">${p.active ? "Nonaktifkan" : "Aktifkan"}</button></div></li>`;
  }).join("");
  $("#ais-status").innerHTML = !ais.visible ? `Data AIS berlisensi tersembunyi — isi token akses di Pengaturan.` :
    `${ais.key_configured ? "aisstream.io aktif (API key terpasang)." : "aisstream.io belum aktif — isi AISSTREAM_API_KEY di server untuk AIS realtime."}
     ${ais.n ? `<br>${fmtNum(ais.n)} posisi tersimpan dari ${fmtNum(ais.vessels)} kapal (${fmtTime(ais.first)} – ${fmtTime(ais.last)}).` : "<br>Belum ada data AIS tersimpan."}`;
};
$("#port-list").addEventListener("click", async (e) => {
  const sc = e.target.closest("[data-scan]"), tg = e.target.closest("[data-toggle-port]"), li = e.target.closest("li[data-port]");
  try {
    if (sc) { await api(`/api/sea/ports/${sc.dataset.scan}/scan`, { method: "POST" }); toast("Pemindaian radar berjalan (±1–2 menit)."); setTimeout(LOADERS.sea, 1500); return; }
    if (tg) { await api(`/api/sea/ports/${tg.dataset.togglePort}/active?on=${tg.dataset.on === "1"}`, { method: "POST" }); return LOADERS.sea(); }
    if (li) openPort(+li.dataset.port);
  } catch (err) { toast(err.message, 7000); }
});

async function openPort(id) {
  const p = await api(`/api/sea/ports/${id}`);
  portLayer.clearLayers();
  const b = p.bbox;
  L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: cssVar("--sea"), weight: 1.5, fill: false, dashArray: "6 4", interactive: false }).addTo(portLayer);
  for (const s of p.ships) {
    L.circleMarker([s.lat, s.lon], { radius: 3 + Math.min(5, (s.value || 0) / 60), color: "#fff", weight: 1, fillColor: cssVar("--sea"), fillOpacity: 1 })
      .bindTooltip(`~${fmtNum(s.value)} m`).addTo(portLayer);
  }
  map.fitBounds([[b[1], b[0]], [b[3], b[2]]]);
  const obs = p.observations;
  const rows = obs.slice(-12).reverse().map((o) => `<tr><td>${fmtTime(o.obs_time)}</td><td>${esc(o.properties?.orbit || "")}</td><td>${o.vessels}</td>
    <td>${o.static_targets ?? "–"}</td><td>${o.properties?.large_vessels ?? "–"}</td><td title="Kapal radar tanpa pasangan AIS">${o.dark_vessels ?? "n/a"}</td></tr>`).join("");
  const aisRows = p.ais_daily == null ? `<p class="muted">Data AIS berlisensi tersembunyi (butuh token akses).</p>` :
    p.ais_daily.length ? `<table class="tbl"><tr><th>Hari</th><th>Kapal AIS</th><th>Posisi</th></tr>${p.ais_daily.slice(-10).reverse().map((d) => `<tr><td>${d.day}</td><td>${d.vessels}</td><td>${d.positions}</td></tr>`).join("")}</table>` :
    `<p class="muted">Belum ada data AIS untuk pelabuhan ini.</p>`;
  $("#port-detail").hidden = false;
  $("#port-detail").innerHTML = `<div class="card-head"><h2>${esc(p.name)}</h2>${p.approx ? `<span class="badge warn">koordinat perkiraan</span>` : ""}</div>
    <div class="stats"><div class="stat"><span>Kapal (pindaian terakhir)</span><b>${p.latest ? p.latest.vessels : "–"}</b></div>
      <div class="stat"><span>Rata-rata / tren</span><b>${p.mean_vessels ?? "–"}${p.trend_pct == null ? "" : ` · ${p.trend_pct > 0 ? "+" : ""}${fmtNum(p.trend_pct)}%`}</b></div></div>
    ${obs.length > 1 ? `<h3>Kapal per akuisisi radar</h3>${miniBars(obs.map((o) => o.vessels), obs.map((o) => `${fmtTime(o.obs_time)}: ${o.vessels} kapal`), { color: "var(--sea)" })}` : ""}
    ${rows ? `<table class="tbl"><tr><th>Waktu</th><th>Orbit</th><th>Kapal</th><th>Statis</th><th>≥100 m</th><th>Dark</th></tr>${rows}</table>` : `<p class="muted">Belum ada pindaian. Klik "Pindai".</p>`}
    <h3>AIS harian ${accessBadge("berlisensi")}</h3>${aisRows}
    <p class="hint">Titik biru di peta = kapal radar pindaian terakhir. "Dark" = kapal radar tanpa AIS dalam ±30 menit (n/a bila tak ada data AIS saat itu).</p>`;
}

$("#port-center").addEventListener("click", () => { const c = map.getCenter(); $("#port-lat").value = c.lat.toFixed(4); $("#port-lon").value = c.lng.toFixed(4); });
$("#port-add").addEventListener("click", async () => {
  try {
    await api("/api/sea/ports", { method: "POST", body: JSON.stringify({ name: $("#port-name").value, lat: +$("#port-lat").value,
      lon: +$("#port-lon").value, half_km: +$("#port-half").value }) });
    toast("Pelabuhan ditambahkan."); LOADERS.sea(); refreshNational();
  } catch (err) { toast(err.message, 7000); }
});
$("#ais-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const r = await apiRaw(`/api/sea/ais/import?source=${encodeURIComponent(f.name)}`, f, "text/csv");
    toast(`${fmtNum(r.inserted)} posisi AIS baru diimpor.`); LOADERS.sea();
  } catch (err) { toast(err.message, 8000); }
  e.target.value = "";
});

// ---------------------------------------------------------------- MNE
const STATUS_COLOR = { dalam_izin: "var(--natural)", sebagian: "var(--verify)", izin_tidak_aktif: "#e07b39", di_luar_izin: "var(--human)", tanpa_data_izin: "var(--muted)" };
let mneKindsLoaded = false;
LOADERS.mne = async () => {
  const [dash, lay] = await Promise.all([api("/api/mne/dashboard"), api("/api/mne/layers")]);
  if (!mneKindsLoaded) {
    for (const [k, v] of Object.entries(lay.kinds)) $("#mne-kind").add(new Option(v, k));
    mneKindsLoaded = true;
  }
  $("#mne-stats").innerHTML = Object.entries(dash.by_status).map(([k, v]) => `<div class="stat" style="border-left-color:${STATUS_COLOR[k]}">
    <span>${esc(v.label)}</span><b>${fmtHa(v.area_ha)}</b><span>${v.count} area</span></div>`).join("");
  $("#mne-outside").innerHTML = dash.outside.map((d) => `<li data-open-det="${d.analysis_id}:${d.id}"><div class="li-top">
      <b>${esc(META.classes[d.class]?.label || d.class)} · ${fmtHa(d.area_ha)}</b><span class="badge" style="background:${STATUS_COLOR[d.status]}">${esc(d.status.replaceAll("_", " "))}</span></div>
      <div class="muted">${esc(d.analysis || "")} · keyakinan ${fmtPct(d.confidence)}</div></li>`).join("") || `<li class="muted">Tidak ada.</li>`;
  const maxP = Math.max(1e-9, ...dash.permits.map((p) => p.area_ha));
  $("#mne-permits").innerHTML = dash.permits.length ? dash.permits.slice(0, 15).map((p) => `<li>${hbar(p.label, p.area_ha, maxP, fmtHa(p.area_ha), "var(--mne)")}
      <div class="muted">${esc(p.layer)} · izin ${esc(p.state.replaceAll("_", " "))}</div></li>`).join("") : `<li class="muted">Belum ada gangguan di dalam izin yang terdeteksi (atau belum ada lapisan izin).</li>`;
  $("#mne-layers").innerHTML = lay.layers.map((l) => `<li><div class="li-top"><b>${esc(l.name)}</b>${accessBadge(l.access)}</div>
      <div class="muted">${esc(lay.kinds[l.kind] || l.kind)} · ${fmtNum(l.feature_count)} fitur · ${esc(l.source || "")}${l.license ? ` · ${esc(l.license)}` : ""}</div>
      ${l.visible ? `<div class="row tight" style="margin-top:4px"><button class="ghost small" data-del-layer="${l.id}">Hapus</button></div>` : `<div class="muted">Tersembunyi — butuh token akses.</div>`}</li>`).join("")
    || `<li class="muted">Belum ada lapisan. Impor data izin (IUP/WIUP) atau kawasan hutan di bawah.</li>`;
};
$("#mne-outside").addEventListener("click", (e) => { const d = e.target.closest("[data-open-det]"); if (d) openDetection(...d.dataset.openDet.split(":").map(Number)); });
$("#mne-layers").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del-layer]");
  if (!b || !confirm("Hapus lapisan ini? Status izin semua analisa akan dihitung ulang.")) return;
  try { await api(`/api/mne/layers/${b.dataset.delLayer}`, { method: "DELETE" }); toast("Lapisan dihapus."); setTimeout(LOADERS.mne, 1500); refreshNational(); }
  catch (err) { toast(err.message, 7000); }
});
$("#mne-upload").addEventListener("click", async () => {
  const f = $("#mne-file").files[0];
  if (!f) return toast("Pilih berkas GeoJSON atau Shapefile .zip.");
  const q = new URLSearchParams({ name: $("#mne-name").value || f.name, filename: f.name, kind: $("#mne-kind").value,
    access: $("#mne-access").value, license: $("#mne-license").value });
  try {
    const r = await apiRaw(`/api/mne/layers/upload?${q}`, f);
    toast(`Lapisan "${r.name}" diimpor: ${fmtNum(r.feature_count)} fitur. Status izin dihitung ulang…`);
    natOn.add("permits"); renderNatToggles(); refreshNational();
    setTimeout(LOADERS.mne, 2500);
  } catch (err) { toast(err.message, 9000); }
});
$("#arc-import").addEventListener("click", async () => {
  const b = map.getBounds();
  const body = { name: $("#mne-name").value || "Lapisan ArcGIS", kind: $("#mne-kind").value, access: $("#mne-access").value,
    url: $("#arc-url").value, license: $("#mne-license").value,
    bbox: $("#arc-bbox").checked ? [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()] : null };
  try {
    const r = await api("/api/mne/layers/arcgis", { method: "POST", body: JSON.stringify(body) });
    toast(`Lapisan "${r.name}" diimpor: ${fmtNum(r.feature_count)} fitur.`); setTimeout(LOADERS.mne, 2500); refreshNational();
  } catch (err) { toast(err.message, 9000); }
});
$("#mne-recompute").addEventListener("click", async () => {
  try { const r = await api("/api/mne/recompute", { method: "POST" }); toast(`${r.analyses} analisa dihitung ulang.`); LOADERS.mne(); }
  catch (err) { toast(err.message, 7000); }
});
$("#mne-show").addEventListener("change", (e) => {
  e.target.checked ? natOn.add("permits") : natOn.delete("permits");
  saveUiState({ natLayers: [...natOn] }); renderNatToggles(); refreshNational();
});

// ---------------------------------------------------------------- INF
const infLayer = L.layerGroup().addTo(map);
LOADERS.inf = async () => {
  const rows = await api("/api/inf/overview");
  $("#inf-list").innerHTML = rows.map((a) => `<li data-inf="${a.id}"><div class="li-top"><b>${esc(a.name)}</b>
      <span class="muted">${a.roads_total_km == null ? "belum ada inventaris" : `${fmtNum(a.roads_total_km)} km jalan · ${fmtNum(a.buildings)} bangunan`}</span></div>
      <div class="muted">Run terakhir: jalan baru ${fmtHa(a.new_roads_ha)} · bangunan baru ${fmtHa(a.new_built_ha)}${a.built_frac_wc != null ? ` · terbangun ${fmtPct(a.built_frac_wc)}` : ""}
      ${a.unmapped_km ? ` · <b>${fmtNum(a.unmapped_km, 1)} km jalan baru belum di OSM</b>` : ""}</div></li>`).join("")
    || `<li class="muted">Belum ada area pantauan. Simpan area di modul SAT terlebih dulu.</li>`;
};
$("#inf-list").addEventListener("click", (e) => { const li = e.target.closest("li[data-inf]"); if (li) openInf(+li.dataset.inf); });

async function openInf(id, refresh = false) {
  $("#inf-detail").hidden = false;
  $("#inf-detail").innerHTML = `<p class="muted">Memuat inventaris OpenStreetMap…</p>`;
  const d = await api(`/api/inf/aois/${id}?refresh=${refresh}`);
  infLayer.clearLayers();
  const b = d.aoi.bbox;
  map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [20, 20] });
  for (const u of d.changes.unmapped_roads) {
    L.circleMarker([u.lat, u.lon], { radius: 6, color: cssVar("--inf"), weight: 2, fillOpacity: 0.3 }).bindTooltip(`Jalan baru ${fmtNum(u.length_km, 2)} km`).addTo(infLayer);
  }
  const inv = d.inventory;
  const maxR = inv ? Math.max(1e-9, ...Object.values(inv.roads_km)) : 1;
  const s = d.changes.series;
  $("#inf-detail").innerHTML = `<div class="card-head"><h2>${esc(d.aoi.name)}</h2><button class="ghost small" id="inf-refresh" data-id="${id}">Perbarui OSM</button></div>
    ${d.inventory_error ? `<p class="muted" style="color:var(--human)">${esc(d.inventory_error)}</p>` : ""}
    ${inv ? `<h3>Jalan (OSM)</h3>${Object.entries(inv.roads_km).filter(([, v]) => v > 0).map(([k, v]) => hbar(k, v, maxR, `${fmtNum(v, 1)} km`, "var(--inf)")).join("")}
      <div class="kv"><span>Total jalan</span><span>${fmtNum(inv.roads_total_km, 1)} km</span><span>Jembatan</span><span>${fmtNum(inv.bridges)}</span>
      <span>Bangunan</span><span>${fmtNum(inv.buildings)}</span>${Object.entries(inv.landuse_ha).map(([k, v]) => `<span>Kawasan ${esc(k)}</span><span>${fmtHa(v)}</span>`).join("")}</div>
      <p class="hint">${esc(inv.source)} · snapshot ${fmtTime(inv.snapshot_at)}</p>` : ""}
    ${s.length ? `<h3>Bangunan baru per run (satelit)</h3>${miniBars(s.map((x) => x.built_ha), s.map((x) => `s/d ${x.after_end}: ${fmtHa(x.built_ha)} bangunan baru, ${fmtHa(x.roads_ha)} jalan baru`), { color: "var(--inf)" })}` : `<p class="muted">Belum ada analisa satelit untuk area ini.</p>`}
    <h3>Jalan baru belum terpetakan di OSM (${fmtNum(d.changes.unmapped_km, 1)} km)</h3>
    ${d.changes.unmapped_roads.length ? `<ul class="list compact">${d.changes.unmapped_roads.slice(0, 20).map((u) => `<li data-open-det="${u.analysis_id}:${u.id}">${fmtNum(u.length_km, 2)} km · ${fmtHa(u.area_ha)}${u.osm_checked ? "" : " <span class='muted'>(OSM belum dicek)</span>"}</li>`).join("")}</ul>` : `<p class="muted">Tidak ada.</p>`}`;
}
$("#inf-detail").addEventListener("click", (e) => {
  if (e.target.id === "inf-refresh") return openInf(+e.target.dataset.id, true).catch((err) => toast(err.message, 7000));
  const d = e.target.closest("[data-open-det]");
  if (d) openDetection(...d.dataset.openDet.split(":").map(Number));
});

// ---------------------------------------------------------------- OS
const newsLayer = L.layerGroup().addTo(map);
let osCache = [], osTopicsLoaded = false;
LOADERS.os = async () => {
  if (!osTopicsLoaded) {
    const topics = await api("/api/os/topics");
    for (const [k, v] of Object.entries(topics)) $("#os-topic").add(new Option(v.label, k));
    osTopicsLoaded = true;
  }
  const q = new URLSearchParams({ days: $("#os-days").value, geolocated: $("#os-geo").checked, limit: 300 });
  if ($("#os-topic").value) q.set("topic", $("#os-topic").value);
  if ($("#os-q").value) q.set("q", $("#os-q").value);
  osCache = await api(`/api/os/news?${q}`);
  $("#os-list").innerHTML = osCache.map((n) => {
    const p = n.properties, loc = p.location;
    return `<li data-news="${n.id}"><b>${esc(n.title)}</b>
      <div class="muted">${esc(p.publisher || "")} · ${fmtTime(n.event_time)}</div>
      <div>${(p.domains || []).map(domChip).join(" ")} ${(p.topic_labels || []).map((t) => `<span class="muted">${esc(t)}</span>`).join(", ")}</div>
      <div class="muted">${loc ? `📍 ${esc(loc.name)} (±${loc.precision_km} km)` : "tanpa lokasi"}${p.fusion_score ? ` · korroborasi ${p.fusion_score}/10` : ""}</div></li>`;
  }).join("") || `<li class="muted">Tidak ada berita pada filter ini. Klik RSS ↻ / Google News ↻.</li>`;
};
["#os-topic", "#os-days", "#os-geo"].forEach((s) => $(s).addEventListener("change", () => LOADERS.os()));
let osDeb = null;
$("#os-q").addEventListener("input", () => { clearTimeout(osDeb); osDeb = setTimeout(() => LOADERS.os(), 400); });
$("#os-list").addEventListener("click", (e) => { const li = e.target.closest("li[data-news]"); if (li) openNews(+li.dataset.news); });

async function openNews(id) {
  let n = osCache.find((x) => x.id === id);
  if (!n) n = (await api(`/api/os/news?days=365&limit=1000`)).find((x) => x.id === id);
  if (!n) return toast("Berita tidak ditemukan.");
  const fx = await api(`/api/os/news/${id}/fusion`);
  const p = n.properties, loc = p.location;
  newsLayer.clearLayers();
  if (loc) {
    const c = L.circle([loc.lat, loc.lon], { radius: loc.precision_km * 1000, color: cssVar("--os"), weight: 1, fillOpacity: 0.08, dashArray: "5 4", interactive: false }).addTo(newsLayer);
    L.circleMarker([loc.lat, loc.lon], { radius: 6, color: "#fff", weight: 2, fillColor: cssVar("--os"), fillOpacity: 1 }).addTo(newsLayer);
    map.fitBounds(c.getBounds());
  }
  const typeLabel = { aoi: "Area pantauan", detection: "Deteksi satelit", earthquake: "Gempa", hotspot: "Hotspot", port: "Pelabuhan", permit: "Izin tambang" };
  $("#os-detail").hidden = false;
  $("#os-detail").innerHTML = `<h2>${esc(n.title)}</h2>
    <p class="muted">${esc(p.publisher || "")} · ${fmtTime(n.event_time)} · <a href="${esc(p.link)}" target="_blank" rel="noopener noreferrer">buka sumber ↗</a></p>
    ${p.summary ? `<p>${esc(p.summary)}</p>` : ""}
    <div>${(p.domains || []).map(domChip).join(" ")} ${(p.topic_labels || []).map(esc).join(", ")}</div>
    <p class="muted">${loc ? `Lokasi: ${esc(loc.name)} — dicocokkan dari kata "${esc(loc.matched)}" (±${loc.precision_km} km)` : "Lokasi tidak dikenali."}</p>
    <div class="card-head"><h3>Fusi data (radius ±${fx.radius_km ?? "–"} km)</h3><span class="badge ${fx.score >= 4 ? "done" : "rendah"}">korroborasi ${fx.score}/10</span></div>
    ${fx.links.length ? `<ul class="list compact">${fx.links.map((l) => `<li data-link='${esc(JSON.stringify(l))}'><b>${esc(typeLabel[l.type] || l.type)}</b> — ${esc(l.label)}
      <span class="muted">${l.dist_km ? `· ${fmtNum(l.dist_km)} km` : ""}</span></li>`).join("")}</ul>` : `<p class="muted">Belum ada data lain di sekitar lokasi & waktu berita.</p>`}
    <p class="hint">${esc(fx.note)}</p>`;
  $("#os-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("#os-detail").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-link]");
  if (!li) return;
  const l = JSON.parse(li.dataset.link);
  if (l.type === "detection") return openDetection(l.analysis_id, l.id);
  if (l.type === "earthquake") { showModule("dis"); return openExposure(l.id); }
  if (l.type === "port") { showModule("sea"); return openPort(l.id); }
  if (l.type === "aoi") {
    const f = natData.aois?.features.find((x) => x.id === l.id) || (await api("/api/map/layers?layers=aois")).aois.features.find((x) => x.id === l.id);
    if (f) map.fitBounds(L.geoJSON(f).getBounds(), { padding: [30, 30] });
  }
  if (l.type === "hotspot") { natOn.add("hotspots"); renderNatToggles(); refreshNational(); }
});

// ---------------------------------------------------------------- Pengaturan
LOADERS.settings = async () => {
  const [acc, src] = await Promise.all([api("/api/access"), api("/api/sources")]);
  $("#acc-info").innerHTML = acc.token_required
    ? `Server memakai token akses. Tingkat Anda saat ini: ${accessBadge(acc.level)}. Data <i>terbatas</i> & <i>berlisensi</i> hanya tampil dengan token yang benar.`
    : `Mode pengguna tunggal (GEOINT_ACCESS_TOKEN belum diisi di server): semua data terbuka untuk siapa pun yang bisa membuka aplikasi ini. Tingkat: ${accessBadge(acc.level)}.`;
  $("#acc-token").value = accessToken();
  $("#theme-light").checked = document.documentElement.dataset.theme === "light";
  const byDom = {};
  for (const s of src) (byDom[s.domain] = byDom[s.domain] || []).push(s);
  $("#src-table").innerHTML = Object.entries(byDom).map(([d, list]) => `<h3>${domChip(d)}</h3>` + list.map((s) => {
    const st = s.status || {};
    const status = !s.available ? `<span class="badge warn">${esc(s.unavailable_reason)}</span>` :
      s.kind !== "feed" ? `<span class="muted">${esc(s.kind)}</span>` :
      st.last_run ? `${st.ok ? "✓" : "✗"} ${ago(st.last_run)} · ${esc(st.detail || "")}` : `<span class="muted">belum pernah berjalan</span>`;
    return `<div class="src-row"><div class="li-top"><b>${esc(s.label)}</b>${accessBadge(s.access)}</div>
      <div class="muted">Kualitas: ${esc(s.quality)} · ${esc(s.license)}</div>
      <div class="muted">${status}</div>
      ${s.kind === "feed" && s.available ? `<div class="row tight" style="margin-top:4px">
        <label class="inline"><input type="checkbox" data-feed-toggle="${s.id}" ${s.enabled ? "checked" : ""}/> otomatis</label>
        <button class="ghost small" data-run="${s.id}">Jalankan sekarang</button></div>` : ""}</div>`;
  }).join("")).join("");
};
$("#src-table").addEventListener("change", async (e) => {
  const id = e.target.dataset.feedToggle;
  if (!id) return;
  try { await api(`/api/sources/${id}/enable?on=${e.target.checked}`, { method: "POST" }); toast(e.target.checked ? "Feed otomatis diaktifkan." : "Feed otomatis dihentikan."); }
  catch (err) { toast(err.message, 7000); }
});
$("#acc-save").addEventListener("click", () => {
  try { localStorage.setItem("geoint.token", $("#acc-token").value.trim()); } catch {}
  toast("Token disimpan di browser ini."); LOADERS.settings(); refreshNational();
});
$("#acc-clear").addEventListener("click", () => {
  try { localStorage.removeItem("geoint.token"); } catch {}
  $("#acc-token").value = ""; LOADERS.settings(); refreshNational();
});
$("#theme-light").addEventListener("change", (e) => {
  const light = e.target.checked;
  light ? (document.documentElement.dataset.theme = "light") : delete document.documentElement.dataset.theme;
  try { localStorage.setItem("geoint.theme", light ? "light" : ""); } catch {}
  refreshNational();
});

// ---------------------------------------------------------------- awal
(async function initDomains() {
  for (const [k, v] of Object.entries({ sat: "SAT", dis: "DIS", sea: "SEA", mne: "MNE", inf: "INF", os: "OS" })) $("#alert-domain").add(new Option(v, k));
  renderNatToggles();
  // Tunggu META dari app.js (dipakai untuk warna kelas deteksi).
  for (let i = 0; i < 100 && (typeof META === "undefined" || !META); i++) await new Promise((r) => setTimeout(r, 100));
  showModule(loadUiState().module || "home");
  refreshNational();
})();
