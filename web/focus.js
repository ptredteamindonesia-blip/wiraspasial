/* WIRA-SPASIAL — fokus & pembesaran objek perubahan.
 *
 * 1. Inset fokus: kotak penanda di peta utama + garis penghubung ke panel perbesaran.
 *    Panel berisi peta kedua yang bisa digeser/zoom sendiri; kotak di peta utama selalu
 *    mengikuti area yang tampil di panel. Mode: resolusi tinggi, Sentinel-2 sebelum,
 *    sesudah, dan geser-bandingkan (swipe) sebelum|sesudah.
 * 2. Lensa pembesar: lingkaran yang mengikuti kursor, menampilkan citra resolusi tinggi ×4.
 *
 * Memakai variabel global dari app.js: map, current, detFC, META, filteredFeatures, layerById, esc, fmtHa, fmtPct.
 */
"use strict";

const HIRES_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const hiresLayer = () => L.tileLayer(HIRES_URL, { maxZoom: 20, maxNativeZoom: 18 });

const focus = {
  id: null,           // id deteksi yang difokuskan
  mode: "hires",
  swipe: 0.5,         // posisi pemisah mode bandingkan (0..1)
  insetMap: null,
  hires: null,
  before: null,       // imageOverlay Sentinel-2 di peta inset
  after: null,
  outlines: null,     // garis tepi semua deteksi di peta inset
  target: null,       // garis tepi deteksi terfokus
  analysisId: null,
  box: null,          // kotak penanda di peta utama
  syncing: false,
};

// ---------------------------------------------------------------- DOM
const mapEl = map.getContainer();
mapEl.insertAdjacentHTML("beforeend", `
  <svg id="focus-lines" aria-hidden="true"></svg>
  <div id="focus-inset" hidden>
    <div class="fi-head">
      <span class="fi-title"><span class="swatch" id="fi-swatch"></span><b id="fi-title"></b></span>
      <span class="badge" id="fi-badge"></span>
      <span class="fi-nav">
        <button id="fi-prev" title="Objek sebelumnya (←)">‹</button>
        <span id="fi-idx" class="muted"></span>
        <button id="fi-next" title="Objek berikutnya (→)">›</button>
        <button id="fi-reset" title="Kembali ke objek">◎</button>
        <button id="fi-close" title="Tutup (Esc)">✕</button>
      </span>
    </div>
    <div id="fi-map-wrap">
      <div id="fi-map"></div>
      <div id="fi-swipe" hidden><span class="fi-swipe-lbl l">Sebelum</span><span class="fi-swipe-lbl r">Sesudah</span><div class="fi-handle"></div></div>
    </div>
    <div class="fi-modes" role="tablist">
      <button data-mode="hires">Resolusi tinggi</button>
      <button data-mode="before">Sebelum</button>
      <button data-mode="after">Sesudah</button>
      <button data-mode="swipe">Bandingkan</button>
    </div>
    <div class="fi-foot" id="fi-foot"></div>
  </div>
  <div id="lens" hidden><div id="lens-map"></div><span id="lens-lbl"></span></div>
`);
const inset = document.getElementById("focus-inset");
const svg = document.getElementById("focus-lines");
L.DomEvent.disableClickPropagation(inset);
L.DomEvent.disableScrollPropagation(inset);

// ---------------------------------------------------------------- peta inset
function ensureInsetMap() {
  if (focus.insetMap) return;
  focus.insetMap = L.map("fi-map", { attributionControl: false, zoomSnap: 0.25, zoomControl: true, maxZoom: 20 });
  focus.hires = hiresLayer().addTo(focus.insetMap);
  focus.insetMap.on("move zoom", () => { syncBox(); updateSwipeClip(); });
  focus.insetMap.on("zoomend moveend", updateSwipeClip);
}

// Overlay Sentinel-2 & garis tepi deteksi perlu dibuat ulang bila analisa/filter berubah.
function rebuildInsetLayers() {
  if (!focus.insetMap || !current) return;
  for (const k of ["before", "after", "outlines", "target"]) {
    if (focus[k]) focus.insetMap.removeLayer(focus[k]);
    focus[k] = null;
  }
  const p = current.products;
  if (p) {
    focus.before = L.imageOverlay(p.before, p.bounds, { className: "fi-raster" });
    focus.after = L.imageOverlay(p.after, p.bounds, { className: "fi-raster" });
  }
  focus.outlines = L.geoJSON({ type: "FeatureCollection", features: filteredFeatures() }, {
    interactive: false,
    style: (f) => ({ color: META.classes[f.properties.class].color, weight: 1.5, fill: false, opacity: 0.8 }),
  }).addTo(focus.insetMap);
  focus.analysisId = current.id;
}

function featureById(id) {
  return detFC?.features.find((f) => f.id === id);
}

// ---------------------------------------------------------------- buka / tutup
function openFocus(id) {
  const f = featureById(id);
  if (!f) return;
  const first = inset.hidden;
  inset.hidden = false;
  ensureInsetMap();
  if (first) focus.insetMap.invalidateSize();
  if (focus.analysisId !== current.id) rebuildInsetLayers();
  focus.id = id;

  const p = f.properties;
  const cls = META.classes[p.class];
  document.getElementById("fi-swatch").style.background = cls.color;
  document.getElementById("fi-title").textContent = p.class_label;
  const badge = document.getElementById("fi-badge");
  badge.className = `badge ${p.category}`;
  badge.textContent = p.category_label;
  const list = filteredFeatures();
  document.getElementById("fi-idx").textContent = `${list.findIndex((x) => x.id === id) + 1}/${list.length}`;

  if (focus.target) focus.insetMap.removeLayer(focus.target);
  // Garis ganda (gelap + warna kelas) agar tetap terlihat di atas citra terang maupun gelap.
  focus.target = L.layerGroup([
    L.geoJSON(f, { interactive: false, style: { color: "#000", weight: 5, opacity: 0.55, fill: false } }),
    L.geoJSON(f, { interactive: false, style: { color: cls.color, weight: 2.5, fill: false, dashArray: p.category === "perlu_verifikasi" ? "6 4" : null } }),
  ]).addTo(focus.insetMap);

  const bounds = L.geoJSON(f).getBounds();
  focus.insetMap.fitBounds(bounds.pad(0.2), { maxZoom: 18, animate: false });
  setMode(focus.mode);
  frameMainMap(bounds);
  updateFooter(p);
  highlightListItem(id);
  syncBox();
}

function closeFocus() {
  inset.hidden = true;
  focus.id = null;
  if (focus.box) { map.removeLayer(focus.box); focus.box = null; }
  svg.innerHTML = "";
  highlightListItem(null);
}

function stepFocus(delta) {
  const list = filteredFeatures();
  if (!list.length) return;
  const i = list.findIndex((x) => x.id === focus.id);
  openFocus(list[(i + delta + list.length) % list.length].id);
}

// Peta utama menampilkan konteks sekitar objek, digeser agar tidak tertutup panel inset.
function frameMainMap(bounds) {
  const pad = insetPadding();
  map.fitBounds(bounds.pad(2.5), { ...pad, maxZoom: 16 });
}

function insetPadding() {
  const r = inset.getBoundingClientRect();
  const m = mapEl.getBoundingClientRect();
  if (insetLayout() === "bottom") return { paddingTopLeft: [20, 20], paddingBottomRight: [20, r.height + 30] };
  const panel = document.getElementById("layer-panel");
  const right = panel && !panel.hidden ? panel.getBoundingClientRect().width + 30 : 30;
  return { paddingTopLeft: [r.right - m.left + 30, 30], paddingBottomRight: [right, 30] };
}

function insetLayout() {
  // Mengikuti CSS: di layar sempit panel inset melebar di bagian bawah peta.
  return getComputedStyle(inset).right !== "auto" ? "bottom" : "left";
}

// ---------------------------------------------------------------- mode tampilan
function setMode(mode) {
  if (!focus.before && mode !== "hires") mode = "hires";
  focus.mode = mode;
  document.querySelectorAll("#focus-inset .fi-modes button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.disabled = b.dataset.mode !== "hires" && !focus.before;
  });
  const im = focus.insetMap;
  const show = (layer, on) => layer && (on ? layer.addTo(im) : im.removeLayer(layer));
  show(focus.before, mode === "before" || mode === "swipe");
  show(focus.after, mode === "after" || mode === "swipe");
  focus.outlines?.bringToFront();
  focus.target?.eachLayer((l) => l.bringToFront());
  document.getElementById("fi-swipe").hidden = mode !== "swipe";
  const afterEl = focus.after?.getElement();
  if (afterEl) afterEl.style.clipPath = "";
  updateSwipeClip();
  const f = featureById(focus.id);
  if (f) updateFooter(f.properties);
}

function updateSwipeClip() {
  if (focus.mode !== "swipe" || !focus.after) return;
  const el = focus.after.getElement();
  if (!el) return;
  const wrap = document.getElementById("fi-map-wrap").getBoundingClientRect();
  const x = wrap.left + focus.swipe * wrap.width;
  const img = el.getBoundingClientRect();
  // Bagian kiri pemisah = sebelum (citra 'after' dipotong), kanan = sesudah.
  const scale = img.width / (el.offsetWidth || img.width);
  el.style.clipPath = `inset(0 0 0 ${Math.max(0, (x - img.left) / scale)}px)`;
  document.querySelector("#fi-swipe .fi-handle").style.left = `${focus.swipe * 100}%`;
}

function updateFooter(p) {
  const s = current?.summary?.scenes;
  const src = {
    hires: "Citra resolusi tinggi Esri World Imagery — tanggal akuisisi bervariasi, belum tentu sesuai periode analisa.",
    before: `Sentinel-2 komposit ${current.before_start} … ${current.before_end}${s?.before?.n ? ` (${s.before.n} akuisisi)` : ""}, piksel 20 m.`,
    after: `Sentinel-2 komposit ${current.after_start} … ${current.after_end}${s?.after?.n ? ` (${s.after.n} akuisisi)` : ""}, piksel 20 m.`,
    swipe: "Geser pemisah: kiri = sebelum, kanan = sesudah (Sentinel-2, piksel 20 m).",
  }[focus.mode];
  document.getElementById("fi-foot").innerHTML =
    `<div>${fmtHa(p.area_ha)} · keyakinan ${fmtPct(p.confidence)} · skor manusia ${fmtPct(p.human_score)}</div>` +
    (p.reasons[0] ? `<div>• ${esc(p.reasons[0])}</div>` : "") +
    `<div class="muted">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} · ${esc(src)}</div>`;
}

// ---------------------------------------------------------------- kotak & garis penghubung
function syncBox() {
  if (!focus.id || !focus.insetMap) return;
  const b = focus.insetMap.getBounds();
  if (!focus.box) {
    focus.box = L.rectangle(b, { color: "#fff", weight: 2, fill: false, interactive: false, className: "fi-box" }).addTo(map);
  } else {
    focus.box.setBounds(b);
  }
  drawLines();
}

function drawLines() {
  if (!focus.box || inset.hidden) { svg.innerHTML = ""; return; }
  const b = focus.box.getBounds();
  const a = map.latLngToContainerPoint(b.getNorthWest());
  const c = map.latLngToContainerPoint(b.getSouthEast());
  const m = mapEl.getBoundingClientRect();
  const r = document.getElementById("fi-map-wrap").getBoundingClientRect();
  const box = [[a.x, a.y], [c.x, a.y], [c.x, c.y], [a.x, c.y]];                       // TL, TR, BR, BL
  const ins = [[r.left - m.left, r.top - m.top], [r.right - m.left, r.top - m.top],
               [r.right - m.left, r.bottom - m.top], [r.left - m.left, r.bottom - m.top]];
  const overlap = !(c.x < ins[0][0] || a.x > ins[1][0] || c.y < ins[0][1] || a.y > ins[2][1]);
  if (overlap) { svg.innerHTML = ""; return; }

  // Dua garis singgung luar: pasangan sudut yang paling jauh ke kiri/kanan relatif arah kotak→inset.
  const cen = (q) => [(q[0][0] + q[2][0]) / 2, (q[0][1] + q[2][1]) / 2];
  const [bx, by] = cen(box), [ix, iy] = cen(ins);
  const n = [-(iy - by), ix - bx];
  const proj = box.map(([x, y]) => (x - bx) * n[0] + (y - by) * n[1]);
  const iMax = proj.indexOf(Math.max(...proj)), iMin = proj.indexOf(Math.min(...proj));
  const line = (i) => `<line x1="${box[i][0]}" y1="${box[i][1]}" x2="${ins[i][0]}" y2="${ins[i][1]}"/>`;
  svg.innerHTML = `<g class="shadow">${line(iMax)}${line(iMin)}</g><g class="main">${line(iMax)}${line(iMin)}</g>`;
}
map.on("move zoom viewreset resize", drawLines);
window.addEventListener("resize", () => { if (focus.insetMap) focus.insetMap.invalidateSize(); drawLines(); });

// ---------------------------------------------------------------- interaksi
inset.querySelector(".fi-modes").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]");
  if (b && !b.disabled) setMode(b.dataset.mode);
});
document.getElementById("fi-close").addEventListener("click", closeFocus);
document.getElementById("fi-prev").addEventListener("click", () => stepFocus(-1));
document.getElementById("fi-next").addEventListener("click", () => stepFocus(1));
document.getElementById("fi-reset").addEventListener("click", () => openFocus(focus.id));

// Pemisah mode bandingkan: seret di mana saja pada area peta inset sambil menekan pemisah.
(() => {
  const swipeEl = document.getElementById("fi-swipe");
  const handle = swipeEl.querySelector(".fi-handle");
  let dragging = false;
  const moveTo = (clientX) => {
    const r = swipeEl.getBoundingClientRect();
    focus.swipe = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
    updateSwipeClip();
  };
  handle.addEventListener("pointerdown", (e) => { dragging = true; handle.setPointerCapture(e.pointerId); e.preventDefault(); });
  handle.addEventListener("pointermove", (e) => dragging && moveTo(e.clientX));
  handle.addEventListener("pointerup", () => (dragging = false));
})();

document.addEventListener("keydown", (e) => {
  if (inset.hidden || e.target.closest("input, select, textarea")) return;
  if (e.key === "Escape") closeFocus();
  else if (e.key === "ArrowRight") stepFocus(1);
  else if (e.key === "ArrowLeft") stepFocus(-1);
});

// Tombol "Fokus & perbesar" di popup deteksi.
mapEl.addEventListener("click", (e) => {
  const b = e.target.closest("[data-focus]");
  if (!b) return;
  map.closePopup();
  openFocus(+b.dataset.focus);
});

function highlightListItem(id) {
  document.querySelectorAll("#det-list li[data-id]").forEach((li) => {
    const on = +li.dataset.id === id;
    li.classList.toggle("active", on);
    if (on) li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

// ---------------------------------------------------------------- lensa pembesar
const lens = { on: false, map: null, outlines: null, el: document.getElementById("lens"), zoomBoost: 2 };

function ensureLensMap() {
  if (lens.map) return;
  lens.el.hidden = false;
  lens.map = L.map("lens-map", {
    attributionControl: false, zoomControl: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
    boxZoom: false, keyboard: false, touchZoom: false, zoomAnimation: false, fadeAnimation: false, maxZoom: 20, zoomSnap: 0,
  });
  hiresLayer().addTo(lens.map);
  lens.map.setView(map.getCenter(), map.getZoom() + lens.zoomBoost);
  rebuildLensOutlines();
}

function rebuildLensOutlines() {
  if (!lens.map) return;
  if (lens.outlines) lens.map.removeLayer(lens.outlines);
  lens.outlines = null;
  if (!detFC) return;
  lens.outlines = L.geoJSON({ type: "FeatureCollection", features: filteredFeatures() }, {
    interactive: false,
    style: (f) => ({ color: META.classes[f.properties.class].color, weight: 2, fill: false }),
  }).addTo(lens.map);
}

function setLens(on) {
  lens.on = on;
  mapEl.classList.toggle("lens-on", on);
  if (on) ensureLensMap();
  lens.el.hidden = true;
}

map.on("mousemove", (e) => {
  if (!lens.on) return;
  // Sembunyikan lensa saat kursor di atas panel inset/kontrol.
  if (e.originalEvent.target.closest?.("#focus-inset, .leaflet-control, #layer-panel")) { lens.el.hidden = true; return; }
  lens.el.hidden = false;
  const size = lens.el.offsetWidth;
  lens.el.style.transform = `translate(${e.containerPoint.x - size / 2}px, ${e.containerPoint.y - size / 2}px)`;
  const z = Math.min(map.getZoom() + lens.zoomBoost, 20);
  if (!lens.sized) { lens.map.invalidateSize({ pan: false }); lens.sized = true; }
  lens.map.setView(e.latlng, z, { animate: false });
  document.getElementById("lens-lbl").textContent = `×${2 ** (z - map.getZoom())}`;
});
map.on("mouseout", () => { lens.el.hidden = true; });
map.on("zoomstart", () => { lens.el.hidden = true; });

// Kursor tanpa hover (layar sentuh) tidak bisa memakai lensa.
const lensToggle = document.getElementById("lyr-lens");
if (lensToggle) {
  if (window.matchMedia("(hover: none)").matches) lensToggle.closest("label").hidden = true;
  lensToggle.addEventListener("change", (e) => setLens(e.target.checked));
}
document.addEventListener("keydown", (e) => {
  if (e.key.toLowerCase() === "l" && !e.target.closest("input, select, textarea") && lensToggle && !e.ctrlKey && !e.metaKey) {
    lensToggle.checked = !lensToggle.checked;
    setLens(lensToggle.checked);
  }
});

// ---------------------------------------------------------------- dipanggil app.js
// Setelah deteksi digambar ulang (analisa baru dimuat atau filter berubah).
function onDetectionsChanged() {
  rebuildLensOutlines();
  if (!focus.insetMap) return;
  rebuildInsetLayers();
  if (focus.id && featureById(focus.id) && filteredFeatures().some((f) => f.id === focus.id)) openFocus(focus.id);
  else closeFocus();
}
