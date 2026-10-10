"""Deteksi perubahan tutupan hutan & ekstraksi ciri per patch perubahan.

Alur:
1. Hitung indeks spektral periode sebelum & sesudah.
2. Piksel "hutan sebelum" (NDVI tinggi, bukan air) yang mengalami penurunan NDVI atau
   kenaikan dNBR signifikan ditandai sebagai perubahan.
3. Piksel perubahan dikelompokkan menjadi patch (8-konektivitas); patch kecil dibuang.
4. Tiap patch diukur: luas, bentuk (keteraturan geometri, elongasi, lebar), spektral
   (tanah terbuka, air keruh, bekas terbakar), topografi (lereng, elevasi relatif,
   keselarasan dengan arah lereng), dan konteks (kedekatan patch lain, sungai).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from rasterio import features as rfeatures
from rasterio.warp import transform_geom
from scipy import ndimage
from shapely.geometry import MultiPoint, mapping, shape
from shapely.ops import unary_union

from .indices import compute_all
from .sources.base import Scene, Terrain

FOREST_NDVI = 0.60
LOSS_DNDVI = -0.20
LOSS_DNBR = 0.25
WATER_NDWI = 0.0


@dataclass
class Patch:
    label: int
    features: dict
    geometry: dict  # GeoJSON EPSG:4326
    centroid: tuple[float, float]  # (lon, lat)
    slices: tuple = field(repr=False, default=())


@dataclass
class ChangeResult:
    patches: list[Patch]
    labels: np.ndarray
    forest_before_ha: float
    loss_ha: float
    valid_fraction: float


def _shape_metrics(mask: np.ndarray) -> dict:
    """Metrik bentuk dari mask biner patch (koordinat piksel)."""
    ys, xs = np.nonzero(mask)
    n = len(ys)
    # Keliling = jumlah sisi piksel yang berbatasan dengan non-patch.
    m = np.pad(mask, 1)
    perim = int((m[1:, :] != m[:-1, :]).sum() + (m[:, 1:] != m[:, :-1]).sum())
    compactness = 4 * math.pi * n / perim**2 if perim else 0.0

    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    pts -= pts.mean(axis=0)
    if n >= 3:
        evals, evecs = np.linalg.eigh(np.cov(pts.T))
        evals = np.clip(evals, 1e-6, None)
        major = evecs[:, 1]
        # Elongasi dihitung dari varians + 1/12 (varians piksel kotak) agar patch 1 piksel lebar tetap wajar.
        elongation = math.sqrt((evals[1] + 1 / 12) / (evals[0] + 1 / 12))
        proj_major = pts @ evecs[:, 1]
        proj_minor = pts @ evecs[:, 0]
        length = float(proj_major.max() - proj_major.min() + 1)
        breadth = float(proj_minor.max() - proj_minor.min() + 1)
        rectangularity = n / (length * breadth)
    else:
        major = np.array([1.0, 0.0])
        elongation, length, rectangularity = 1.0, float(max(n, 1)), 1.0

    # Konveksitas: luas / luas convex hull (dari koordinat sudut piksel).
    corners = np.concatenate([np.stack([xs + dx, ys + dy], 1) for dx in (0, 1) for dy in (0, 1)], axis=0)
    hull_area = MultiPoint(corners.tolist()).convex_hull.area
    convexity = n / hull_area if hull_area > 0 else 1.0

    return {
        "n_px": n,
        "compactness": float(min(compactness, 1.0)),
        "elongation": float(elongation),
        # Lebar & panjang efektif dari luas/keliling: tetap akurat untuk objek berkelok (jalan, sungai).
        "width_px": float(2 * n / perim) if perim else 1.0,
        "length_px": float(max(perim / 2, length)),
        "rectangularity": float(min(rectangularity, 1.0)),
        "convexity": float(min(convexity, 1.0)),
        "major_axis": (float(major[0]), float(major[1])),
    }


def _cv(v: np.ndarray) -> float:
    """Koefisien variasi — heterogenitas permukaan (atap, jalan, halaman bervariasi tinggi)."""
    v = v[np.isfinite(v)]
    return float(v.std() / v.mean()) if v.size > 2 and v.mean() > 0 else float("nan")


def _nanmean(v: np.ndarray) -> float:
    v = v[np.isfinite(v)]
    return float(v.mean()) if v.size else float("nan")


def detect_changes(
    before: Scene,
    after: Scene,
    terrain: Terrain,
    aoi_mask: np.ndarray | None = None,
    min_patch_ha: float = 0.5,
    ancillary=None,
) -> ChangeResult:
    ib = compute_all(before.bands)
    ia = compute_all(after.bands)
    valid = before.valid & after.valid
    if aoi_mask is not None:
        valid &= aoi_mask

    px_area_ha = before.pixel_area_m2 / 10_000
    px_m = float(np.mean(before.pixel_size_m))

    with np.errstate(invalid="ignore"):
        water_before = valid & (ib["ndwi"] > WATER_NDWI)
        forest_before = valid & (ib["ndvi"] > FOREST_NDVI) & ~water_before
        dndvi = ia["ndvi"] - ib["ndvi"]
        dnbr = ib["nbr"] - ia["nbr"]
        loss = forest_before & ((dndvi < LOSS_DNDVI) | (dnbr > LOSS_DNBR))
    # Isi celah kecil (pohon tersisa di tengah bukaan) tanpa menghapus jalan selebar 1 piksel.
    loss |= ndimage.binary_closing(loss, structure=np.ones((3, 3))) & forest_before

    labels, n = ndimage.label(loss, structure=np.ones((3, 3)))
    if n:
        sizes = ndimage.sum_labels(loss, labels, index=np.arange(1, n + 1))
        min_px = max(3, int(math.ceil(min_patch_ha / px_area_ha)))
        keep = np.concatenate([[False], sizes >= min_px])
        remap = np.zeros(n + 1, np.int32)
        remap[keep] = np.arange(1, keep.sum() + 1)
        labels = remap[labels]
    n_kept = int(labels.max())

    # Jarak ke badan air (sungai/danau) pada periode sebelum, dalam meter.
    dist_water_m = (
        ndimage.distance_transform_edt(~water_before) * px_m if water_before.any() else np.full(labels.shape, 1e5)
    )
    elev_sorted = np.sort(terrain.elevation[valid] if valid.any() else terrain.elevation.ravel())

    polys: dict[int, list] = {}
    if n_kept:
        for geom, val in rfeatures.shapes(
            labels, mask=labels > 0, connectivity=8, transform=before.transform
        ):
            polys.setdefault(int(val), []).append(shape(geom))

    ax, ay = terrain.aspect_vec
    anc = _prepare_ancillary(ancillary, labels.shape, px_m)
    patches: list[Patch] = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        m = labels[sl] == i
        sm = _shape_metrics(m)

        water_px = (ia["ndwi"][sl] > WATER_NDWI) & m
        land = m & ~water_px
        albedo = (after.bands["blue"][sl] + after.bands["green"][sl] + after.bands["red"][sl]) / 3

        # Keselarasan sumbu utama patch dengan arah turun lereng (longsor memanjang searah lereng).
        mvx, mvy = float(ax[sl][m].mean()), float(ay[sl][m].mean())
        norm = math.hypot(mvx, mvy)
        major = sm.pop("major_axis")
        alignment = abs(major[0] * mvx + major[1] * mvy) / norm if norm > 1e-6 else 0.0

        elev_mean = float(terrain.elevation[sl][m].mean())
        slope_vals = terrain.slope_deg[sl][m]

        f = {
            **sm,
            "area_ha": float(sm["n_px"] * px_area_ha),
            "width_m": float(sm["width_px"] * px_m),
            "length_m": float(sm["length_px"] * px_m),
            "ndvi_before": _nanmean(ib["ndvi"][sl][m]),
            "ndvi_after": _nanmean(ia["ndvi"][sl][m]),
            "dndvi": _nanmean(dndvi[sl][m]),
            "dnbr": _nanmean(dnbr[sl][m]),
            "nbr_after": _nanmean(ia["nbr"][sl][m]),
            "bsi_after": _nanmean(ia["bsi"][sl][land]),
            "ferric_after": _nanmean(ia["ferric"][sl][land]),
            "albedo_after": _nanmean(albedo[land]),
            "albedo_cv": _cv(albedo[land]),
            "water_frac": float(water_px.sum() / m.sum()),
            "ndti_water": _nanmean(ia["ndti"][sl][water_px]),
            "slope_mean": float(slope_vals.mean()),
            "slope_p90": float(np.percentile(slope_vals, 90)),
            "elev_mean": elev_mean,
            "rel_elev": float(np.searchsorted(elev_sorted, elev_mean) / max(len(elev_sorted), 1)),
            "downslope_alignment": float(alignment),
            "dist_water_m": float(dist_water_m[sl][m].min()),
            "hotspots": 0,
        }
        f.update(_ancillary_features(anc, sl, m, water_px))

        geom_src = unary_union(polys.get(i, [])).simplify(abs(before.transform.a) * 0.5, preserve_topology=True)
        geom = mapping(geom_src)
        cy, cx = ndimage.center_of_mass(m)
        lon, lat = before.transform @ (sl[1].start + cx + 0.5, sl[0].start + cy + 0.5)
        if before.crs != "EPSG:4326":
            geom = transform_geom(before.crs, "EPSG:4326", geom)
            lon, lat = transform_geom(before.crs, "EPSG:4326", {"type": "Point", "coordinates": (lon, lat)})["coordinates"]
        patches.append(Patch(label=i, features=f, geometry=dict(geom), centroid=(float(lon), float(lat)), slices=sl))

    _add_context(patches, labels, px_m, valid & ~forest_before & ~water_before, valid, anc)

    return ChangeResult(
        patches=patches,
        labels=labels,
        forest_before_ha=float(forest_before.sum() * px_area_ha),
        loss_ha=float((labels > 0).sum() * px_area_ha),
        valid_fraction=float(valid.mean()),
    )


def _add_context(patches: list[Patch], labels: np.ndarray, px_m: float, nonforest_before: np.ndarray, valid: np.ndarray,
                 anc: dict | None = None) -> None:
    """Konteks spasial: patch tetangga, jarak ke patch terdekat, dan lahan non-hutan di sekelilingnya."""
    if not patches:
        return
    cents = np.array([[(s.start + s.stop) / 2 for s in p.slices] for p in patches])
    for p, c in zip(patches, cents):
        d = np.hypot(*(cents - c).T) * px_m
        p.features["n_neighbors_1500m"] = int(((d > 0) & (d < 1500)).sum())

    pad = int(math.ceil(600 / px_m))
    H, W = labels.shape
    for p in patches:
        sy, sx = p.slices
        y0, y1 = max(0, sy.start - pad), min(H, sy.stop + pad)
        x0, x1 = max(0, sx.start - pad), min(W, sx.stop + pad)
        win = labels[y0:y1, x0:x1]
        own = win == p.label
        # Fraksi lahan non-hutan (permukiman, kebun, lahan terbuka lama) dalam cincin 300 m pada periode sebelum.
        ring = ndimage.binary_dilation(own, iterations=max(1, int(300 / px_m))) & ~own & valid[y0:y1, x0:x1]
        p.features["ring_nonforest"] = float(nonforest_before[y0:y1, x0:x1][ring].mean()) if ring.any() else 0.0
        # Konteks dari data pendukung di cincin 300 m: lahan terbangun / tanah terbuka (WorldCover), objek OSM.
        for key, layer in (("wc_built_ring", "wc_built"), ("wc_bare_ring", "wc_bare"),
                           ("osm_built_ring", "osm_built"), ("osm_mining_ring", "osm_mining")):
            arr = (anc or {}).get(layer)
            p.features[key] = _nanmean(arr[y0:y1, x0:x1][ring]) if arr is not None and ring.any() else None
        others = (win > 0) & (win != p.label)
        if not others.any():
            p.features["nearest_patch_m"] = 9999.0
            p.features["adjacent_labels"] = []
            continue
        dist = ndimage.distance_transform_edt(~others)
        p.features["nearest_patch_m"] = float(dist[own].min() * px_m)
        near = ndimage.binary_dilation(own, iterations=max(1, int(150 / px_m))) & others
        p.features["adjacent_labels"] = sorted(int(v) for v in np.unique(win[near]))


def _prepare_ancillary(ancillary, shape, px_m: float) -> dict:
    """Turunkan layer bantu (mask kelas WorldCover, jarak ke jalan OSM, selisih radar) sekali per analisa."""
    if ancillary is None:
        return {}
    L = ancillary.layers
    out: dict = {}

    def ok(name):
        a = L.get(name)
        return a if a is not None and a.shape == shape else None

    wc = ok("worldcover")
    if wc is not None:
        known = np.isfinite(wc) & (wc > 0)
        for name, codes in (("wc_tree", (10, 95)), ("wc_agri", (20, 30, 40)), ("wc_built", (50,)), ("wc_bare", (60,)),
                            ("wc_mangrove", (95,))):
            out[name] = np.where(known, np.isin(wc, codes), np.nan).astype(np.float32)
    if ok("gsw_occurrence") is not None:
        out["gsw"] = L["gsw_occurrence"]
    vb, va = ok("vh_before_db"), ok("vh_after_db")
    if vb is not None and va is not None:
        out["dvh"] = (va - vb).astype(np.float32)
        out["vh_after"] = va
    for name in ("osm_mining", "osm_built", "osm_road"):
        if ok(name) is not None:
            out[name] = L[name]
    road = ok("osm_road")
    if road is not None:
        out["road_dist_m"] = (ndimage.distance_transform_edt(road < 0.5) * px_m).astype(np.float32) if road.any() else None
    return out


def _ancillary_features(anc: dict, sl, m: np.ndarray, water_px: np.ndarray) -> dict:
    """Indikator per patch dari data pendukung. Nilai None berarti sumber tidak tersedia."""
    if not anc:
        return {}
    f: dict = {}

    def frac(name):
        a = anc.get(name)
        return _nanmean(a[sl][m]) if a is not None else None

    f["wc_tree_frac"] = frac("wc_tree")
    f["wc_agri_frac"] = frac("wc_agri")
    f["wc_mangrove_frac"] = frac("wc_mangrove")
    f["osm_mining_frac"] = frac("osm_mining")
    f["osm_built_frac"] = frac("osm_built")
    f["osm_road_frac"] = frac("osm_road")
    rd = anc.get("road_dist_m")
    f["road_dist_m"] = float(rd[sl][m].min()) if rd is not None else (99999.0 if "osm_road" in anc else None)
    if "dvh" in anc:
        f["dvh_db"] = _nanmean(anc["dvh"][sl][m])
        f["vh_after_db"] = _nanmean(anc["vh_after"][sl][m])
    else:
        f["dvh_db"] = f["vh_after_db"] = None
    gsw = anc.get("gsw")
    if gsw is not None:
        g = gsw[sl]
        f["gsw_water_occ"] = _nanmean(g[water_px]) if water_px.any() else None
        # Air "baru": tergenang sekarang tetapi hampir tidak pernah tergenang 1984-2021 (mis. lubang tambang).
        f["new_water_frac"] = float((water_px & (np.nan_to_num(g, nan=0) < 5)).sum() / m.sum())
    else:
        f["gsw_water_occ"] = f["new_water_frac"] = None
    return {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in f.items()}
