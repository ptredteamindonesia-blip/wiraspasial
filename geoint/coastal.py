"""SAT/pesisir — perubahan garis pantai: abrasi, akresi, reklamasi, tambak.

Alur:
1. Air sebelum/sesudah dari MNDWI (hijau vs SWIR1) komposit Sentinel-2.
2. "Laut/badan air besar" = komponen air periode sebelum ≥ 1 km²; zona pesisir = ≤ 1,5 km darinya.
3. Darat→air (abrasi/tambak) dan air→darat (akresi/reklamasi) di zona pesisir. Piksel yang sudah
   tercatat sebagai kehilangan hutan (termasuk mangrove→tambak) ditangani pipeline hutan.
4. Zona pasang surut disaring dengan JRC Global Surface Water: darat→air hanya dihitung pada
   piksel yang jarang tergenang (< 20%), air→darat pada piksel yang hampir selalu air (> 60%),
   karena komposit dua periode bisa diambil pada kondisi pasang yang berbeda.
"""

from __future__ import annotations

import math

import numpy as np
from rasterio import features as rfeatures
from rasterio.warp import transform_geom
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from .change import Patch, _nanmean, _shape_metrics
from .sources.base import Scene, Terrain

MNDWI_WATER = 0.10
MIN_SEA_KM2 = 1.0
COAST_ZONE_M = 1500


def _mndwi(b: dict) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (b["green"] - b["swir1"]) / (b["green"] + b["swir1"])


def detect_coastal(before: Scene, after: Scene, terrain: Terrain, forest_labels: np.ndarray, aoi_mask=None,
                   ancillary=None, min_patch_ha: float = 0.5, label_offset: int = 0):
    """Kembalikan (patches, labels) perubahan pesisir; labels memakai nomor mulai label_offset+1."""
    shape_ = before.shape
    labels_out = np.zeros(shape_, np.int32)
    valid = before.valid & after.valid
    if aoi_mask is not None:
        valid &= aoi_mask
    mb, ma = _mndwi(before.bands), _mndwi(after.bands)
    water_b = valid & (np.nan_to_num(mb, nan=-1) > MNDWI_WATER)
    water_a = valid & (np.nan_to_num(ma, nan=-1) > MNDWI_WATER)
    px_area = before.pixel_area_m2
    px_m = float(np.mean(before.pixel_size_m))

    lab, n = ndimage.label(water_b)
    if not n:
        return [], labels_out
    sizes = ndimage.sum_labels(water_b, lab, np.arange(1, n + 1)) * px_area / 1e6
    big_ids = np.where(sizes >= MIN_SEA_KM2)[0] + 1
    if not len(big_ids):
        return [], labels_out
    sea = np.isin(lab, big_ids)
    dist_sea = ndimage.distance_transform_edt(~sea) * px_m
    # Laut terbuka pada periode SESUDAH: dipakai untuk membedakan akresi (daratan tumbuh menghadap laut)
    # dari pengurukan kolam/tambak di darat (dikelilingi daratan setelah diuruk).
    lab_a, n_a = ndimage.label(water_a)
    sea_after = np.zeros(shape_, bool)
    if n_a:
        sizes_a = ndimage.sum_labels(water_a, lab_a, np.arange(1, n_a + 1)) * px_area / 1e6
        sea_after = np.isin(lab_a, np.where(sizes_a >= MIN_SEA_KM2)[0] + 1)
    zone = dist_sea <= COAST_ZONE_M

    gsw = None
    if ancillary is not None and ancillary.layers.get("gsw_occurrence") is not None and ancillary.layers["gsw_occurrence"].shape == shape_:
        gsw = np.nan_to_num(ancillary.layers["gsw_occurrence"], nan=0)
    strong = np.abs(np.nan_to_num(ma - mb)) > 0.25  # perubahan MNDWI tegas (kurangi efek pasang surut/kekeruhan)
    to_water = zone & water_a & ~water_b & strong & (forest_labels == 0)
    to_land = zone & water_b & ~water_a & strong
    if gsw is not None:
        to_water &= gsw < 20
        to_land &= gsw > 60
    min_px = max(3, int(math.ceil(min_patch_ha * 10_000 / px_area)))

    from .indices import compute_all

    ia = compute_all(after.bands)
    albedo = (after.bands["blue"] + after.bands["green"] + after.bands["red"]) / 3
    built = None
    if ancillary is not None and ancillary.layers.get("worldcover") is not None and ancillary.layers["worldcover"].shape == shape_:
        built = (ancillary.layers["worldcover"] == 50).astype(np.float32)
    osm_built = ancillary.layers.get("osm_built") if ancillary is not None else None

    patches: list[Patch] = []
    next_label = label_offset
    for direction, mask in (("ke_air", to_water), ("ke_darat", to_land)):
        mask = ndimage.binary_opening(mask, structure=np.ones((2, 2)))
        lab, n = ndimage.label(mask, structure=np.ones((3, 3)))
        if not n:
            continue
        for i, sl in enumerate(ndimage.find_objects(lab), start=1):
            if sl is None:
                continue
            m = lab[sl] == i
            if m.sum() < min_px:
                continue
            next_label += 1
            labels_out[sl][m] = next_label
            sm = _shape_metrics(m)
            sm.pop("major_axis")
            # Cincin 300 m untuk konteks lahan terbangun/pelabuhan.
            pad = int(300 / px_m) + 1
            y0, y1 = max(0, sl[0].start - pad), min(shape_[0], sl[0].stop + pad)
            x0, x1 = max(0, sl[1].start - pad), min(shape_[1], sl[1].stop + pad)
            own = np.zeros((y1 - y0, x1 - x0), bool)
            own[sl[0].start - y0: sl[0].stop - y0, sl[1].start - x0: sl[1].stop - x0] = m
            ring = ndimage.binary_dilation(own, iterations=pad - 1) & ~own
            edge = ndimage.binary_dilation(own, iterations=2) & ~own  # tepi luar ±40 m
            f = {
                **sm,
                "direction": direction,
                "area_ha": float(m.sum() * px_area / 10_000),
                "width_m": float(sm["width_px"] * px_m),
                "length_m": float(sm["length_px"] * px_m),
                "ndvi_after": _nanmean(ia["ndvi"][sl][m]),
                "bsi_after": _nanmean(ia["bsi"][sl][m]),
                "albedo_after": _nanmean(albedo[sl][m]),
                "elev_mean": float(terrain.elevation[sl][m].mean()),
                "dist_sea_m": float(dist_sea[sl][m].min()),
                "open_sea_contact": float(sea_after[y0:y1, x0:x1][edge].mean()) if edge.any() else 0.0,
                "gsw_occ": _nanmean(gsw[sl][m]) if gsw is not None else None,
                "wc_built_ring": _nanmean(built[y0:y1, x0:x1][ring]) if built is not None and ring.any() else None,
                "osm_built_ring": _nanmean(osm_built[y0:y1, x0:x1][ring]) if osm_built is not None and ring.any() else None,
                "n_neighbors_1500m": 0,
                "hotspots": 0,
            }
            f = {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in f.items()}
            patches.append(Patch(label=next_label, features=f, geometry={}, centroid=(0.0, 0.0), slices=sl))

    if not patches:
        return [], labels_out
    # Tetangga sejenis (pola petak tambak berderet).
    cents = np.array([[(s.start + s.stop) / 2 for s in p.slices] for p in patches])
    for p, c in zip(patches, cents):
        d = np.hypot(*(cents - c).T) * px_m
        same = np.array([q.features["direction"] == p.features["direction"] for q in patches])
        p.features["n_neighbors_1500m"] = int(((d > 0) & (d < 1500) & same).sum())
    # Poligon & titik pusat (EPSG:4326).
    polys: dict[int, list] = {}
    for geom, val in rfeatures.shapes(labels_out, mask=labels_out > 0, connectivity=8, transform=before.transform):
        polys.setdefault(int(val), []).append(shape(geom))
    for p in patches:
        g = unary_union(polys.get(p.label, [])).simplify(abs(before.transform.a) * 0.5, preserve_topology=True)
        c = g.centroid
        geom, (lon, lat) = mapping(g), (c.x, c.y)
        if before.crs != "EPSG:4326":
            geom = transform_geom(before.crs, "EPSG:4326", geom)
            lon, lat = transform_geom(before.crs, "EPSG:4326", {"type": "Point", "coordinates": (lon, lat)})["coordinates"]
        p.geometry, p.centroid = dict(geom), (float(lon), float(lat))
    return patches, labels_out
