"""Sumber data pendukung untuk memperkuat akurasi klasifikasi.

Semua gratis & tanpa API key, dimuat pada grid raster yang sama dengan citra Sentinel-2:

  s1         Sentinel-1 RTC (radar C-band, VH/VV) — menembus awan; penurunan backscatter VH
             menandakan hilangnya struktur tegakan (pohon ditebang/digali), sedangkan stres
             vegetasi alami biasanya tidak mengubah VH. (Microsoft Planetary Computer)
  worldcover ESA WorldCover 10 m (2021) — konteks tutupan lahan: terbangun, tanah terbuka,
             pertanian, hutan, mangrove. (Planetary Computer)
  gsw        JRC Global Surface Water — frekuensi kemunculan air 1984–2021: genangan di area yang
             memang sering tergenang (dataran banjir) vs. kolam baru (lubang tambang/bendungan).
             (Planetary Computer)
  osm        OpenStreetMap (Overpass API) — jalan, area tambang/quarry, permukiman & industri.

Setiap sumber bersifat opsional: bila gagal (jaringan/kuota) analisa tetap berjalan dan
status sumber dicatat di ringkasan analisa.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field

import numpy as np

from .config import settings
from .sources.base import BBox, Period, Scene

log = logging.getLogger(__name__)

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "WIRA-SPASIAL/0.2 (national geospatial monitoring)"

# Kode kelas ESA WorldCover.
WC_TREE, WC_SHRUB, WC_GRASS, WC_CROP, WC_BUILT, WC_BARE, WC_WATER, WC_WETLAND, WC_MANGROVE = 10, 20, 30, 40, 50, 60, 80, 90, 95

SOURCE_LABELS = {
    "s1": "Sentinel-1 radar (VH/VV)",
    "worldcover": "ESA WorldCover 2021",
    "gsw": "JRC Global Surface Water",
    "osm": "OpenStreetMap",
}

LAYERS = ("worldcover", "gsw_occurrence", "vh_before_db", "vh_after_db", "osm_road", "osm_mining", "osm_built")


@dataclass
class Ancillary:
    layers: dict[str, np.ndarray] = field(default_factory=dict)  # float32, NaN = tidak tersedia
    sources: dict[str, dict] = field(default_factory=dict)       # {"s1": {"status": "ok"|"error"|"off", "detail": ...}}

    def get(self, name: str) -> np.ndarray | None:
        return self.layers.get(name)


def enabled_sources() -> list[str]:
    return [s.strip() for s in settings.ancillary.split(",") if s.strip() in SOURCE_LABELS]


# --------------------------------------------------------------------------- Planetary Computer
def _catalog():
    import planetary_computer
    import pystac_client

    return pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=planetary_computer.sign_inplace)


def _geobox(scene: Scene):
    from odc.geo.geobox import GeoBox

    return GeoBox(scene.shape, scene.transform, scene.crs)


def _load_static(catalog, collection: str, asset: str, bbox: BBox, gbox, prefer_latest=True) -> tuple[np.ndarray, str]:
    import odc.stac

    items = list(catalog.search(collections=[collection], bbox=bbox).items())
    if not items:
        raise RuntimeError("tidak ada data untuk area ini")
    # WorldCover: ambil versi terbaru (2021) untuk tiap ubin.
    items = sorted(items, key=lambda i: i.id)
    if prefer_latest:
        latest = items[-1].id.split("_")[3] if collection == "esa-worldcover" else None
        if latest:
            items = [i for i in items if latest in i.id]
    ds = odc.stac.load(items, bands=[asset], geobox=gbox, resampling="nearest", chunks=None)
    arr = ds[asset].values.astype(np.float32)
    nodata = ds[asset].attrs.get("nodata")
    if arr.ndim == 3:  # satu irisan per ubin: gabungkan, ambil nilai valid pertama
        out = np.full(arr.shape[1:], np.nan, np.float32)
        for sl in arr:
            ok = np.isnan(out) & ((sl != nodata) if nodata is not None else True)
            out[ok] = sl[ok]
        arr = out
    elif nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    return arr, ", ".join(sorted({i.id for i in items}))


def _load_s1(catalog, bbox: BBox, period: Period, gbox) -> tuple[np.ndarray, int, str]:
    import odc.stac

    items = list(catalog.search(collections=["sentinel-1-rtc"], bbox=bbox, datetime=f"{period[0]}/{period[1]}").items())
    if not items:
        raise RuntimeError(f"tidak ada akuisisi Sentinel-1 pada {period[0]}..{period[1]}")
    # Satu orbit (arah pass) saja agar geometri pantulan konsisten antar periode.
    by_dir: dict[str, list] = {}
    for it in items:
        by_dir.setdefault(it.properties.get("sat:orbit_state", "?"), []).append(it)
    direction, items = max(by_dir.items(), key=lambda kv: len(kv[1]))
    items = sorted(items, key=lambda i: i.datetime)[:: max(1, len(items) // settings.max_scenes_per_period)][: settings.max_scenes_per_period]
    ds = odc.stac.load(items, bands=["vh"], geobox=gbox, groupby="solar_day", chunks=None, resampling="bilinear")
    vh = ds["vh"].values.astype(np.float32)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(np.where(vh > 0, vh, np.nan), axis=0)
        db = 10 * np.log10(med)
    # Filter speckle ringan (median 3x3) — radar sangat berbintik pada resolusi piksel.
    from scipy import ndimage

    filled = np.where(np.isfinite(db), db, np.nanmedian(db))
    db = np.where(np.isfinite(db), ndimage.median_filter(filled, size=3), np.nan)
    return db.astype(np.float32), int(ds.sizes["time"]), direction


# --------------------------------------------------------------------------- OpenStreetMap
def _overpass(bbox: BBox) -> list[dict]:
    w, s, e, n = bbox
    b = f"{s},{w},{n},{e}"
    query = (
        f"[out:json][timeout:60];("
        f'way["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|track"]({b});'
        f'way["landuse"~"quarry|industrial|residential|commercial|construction"]({b});'
        f'relation["landuse"~"quarry|industrial|residential"]({b});'
        f'way["man_made"~"mineshaft|adit"]({b});way["industrial"="mine"]({b});'
        f");out tags geom;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            from .domains.common import SSL_CONTEXT

            with urllib.request.urlopen(req, timeout=90, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))["elements"]
        except Exception as exc:  # coba server cermin berikutnya
            last = exc
    raise RuntimeError(f"Overpass tidak dapat dihubungi: {last}")


def _rasterize_osm(elements: list[dict], scene: Scene) -> dict[str, np.ndarray]:
    from rasterio import features as rfeatures
    from rasterio.warp import transform_geom

    roads, mining, built = [], [], []
    for el in elements:
        tags = el.get("tags", {})
        rings = []
        if el["type"] == "way" and "geometry" in el:
            rings = [[(p["lon"], p["lat"]) for p in el["geometry"]]]
        elif el["type"] == "relation":
            rings = [[(p["lon"], p["lat"]) for p in m["geometry"]] for m in el.get("members", []) if m.get("role") == "outer" and "geometry" in m]
        for coords in rings:
            if len(coords) < 2:
                continue
            if "highway" in tags:
                roads.append({"type": "LineString", "coordinates": coords})
            elif len(coords) >= 4 and coords[0] == coords[-1]:
                poly = {"type": "Polygon", "coordinates": [coords]}
                if tags.get("landuse") == "quarry" or tags.get("industrial") == "mine" or "man_made" in tags:
                    mining.append(poly)
                else:
                    built.append(poly)

    def burn(geoms):
        if not geoms:
            return np.zeros(scene.shape, np.float32)
        if scene.crs != "EPSG:4326":
            geoms = [transform_geom("EPSG:4326", scene.crs, g) for g in geoms]
        return rfeatures.rasterize(geoms, out_shape=scene.shape, transform=scene.transform, all_touched=True, dtype="uint8").astype(np.float32)

    return {"osm_road": burn(roads), "osm_mining": burn(mining), "osm_built": burn(built)}


# --------------------------------------------------------------------------- publik
def load_ancillary(scene: Scene, bbox: BBox, before: Period, after: Period, progress=None) -> Ancillary:
    """Muat semua sumber pendukung yang aktif untuk grid `scene`. Tidak pernah melempar error."""
    anc = Ancillary()
    wanted = enabled_sources()
    for s in SOURCE_LABELS:
        if s not in wanted:
            anc.sources[s] = {"status": "off", "detail": "dinonaktifkan (GEOINT_ANCILLARY)"}

    def step(name, p, msg, fn):
        if name not in wanted:
            return
        if progress:
            progress(p, msg)
        try:
            detail = fn()
            anc.sources[name] = {"status": "ok", "detail": detail}
        except Exception as exc:
            log.warning("Sumber %s gagal: %s", name, exc)
            anc.sources[name] = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]}

    catalog = None
    if {"s1", "worldcover", "gsw"} & set(wanted):
        try:
            catalog = _catalog()
        except Exception as exc:
            for s in ("s1", "worldcover", "gsw"):
                if s in wanted:
                    anc.sources[s] = {"status": "error", "detail": f"katalog STAC: {exc}"}
            wanted = [w for w in wanted if w == "osm"]
    gbox = _geobox(scene) if catalog else None

    def wc():
        anc.layers["worldcover"], ids = _load_static(catalog, "esa-worldcover", "map", bbox, gbox)
        return ids

    def gsw():
        arr, ids = _load_static(catalog, "jrc-gsw", "occurrence", bbox, gbox, prefer_latest=False)
        anc.layers["gsw_occurrence"] = np.where(arr > 100, np.nan, arr)  # 255 = tanpa data
        return ids

    def s1():
        vb, nb, d = _load_s1(catalog, bbox, before, gbox)
        va, na, _ = _load_s1(catalog, bbox, after, gbox)
        anc.layers["vh_before_db"], anc.layers["vh_after_db"] = vb, va
        return f"{nb} + {na} akuisisi (orbit {d.lower()})"

    def osm():
        elements = _overpass(bbox)
        anc.layers.update(_rasterize_osm(elements, scene))
        return f"{len(elements)} objek"

    step("worldcover", 0.58, "Memuat ESA WorldCover (konteks tutupan lahan)", wc)
    step("gsw", 0.60, "Memuat JRC Global Surface Water (riwayat genangan)", gsw)
    step("s1", 0.62, "Memuat Sentinel-1 radar sebelum & sesudah (menembus awan)", s1)
    step("osm", 0.66, "Memuat OpenStreetMap (jalan, tambang, permukiman)", osm)
    return anc
