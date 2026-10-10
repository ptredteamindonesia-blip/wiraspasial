"""Sumber data demo: citra sintetis dengan skenario perubahan yang diketahui.

Dipakai untuk demonstrasi offline dan pengujian otomatis. Grid 8x8 km (resolusi 20 m)
di pusat AOI, berisi gunung, sungai, dan hutan, lalu periode "sesudah" disisipi:
tambang terbuka, blok tebangan + jalan sarad, longsor, kebakaran, banjir,
perluasan permukiman, dan stres vegetasi alami.
"""

from __future__ import annotations

import hashlib

import numpy as np
from affine import Affine
from scipy import ndimage

from .base import BANDS, BBox, Period, Scene, terrain_from_dem

# Spektrum reflektansi tipikal (blue, green, red, nir, swir1, swir2).
SPECTRA = {
    "forest": (0.020, 0.050, 0.030, 0.350, 0.160, 0.070),
    "shrub": (0.030, 0.070, 0.050, 0.300, 0.220, 0.110),
    "laterite": (0.080, 0.120, 0.210, 0.260, 0.340, 0.300),
    "soil": (0.100, 0.130, 0.160, 0.220, 0.300, 0.260),
    "turbid_water": (0.080, 0.100, 0.115, 0.060, 0.030, 0.020),
    "water": (0.050, 0.060, 0.040, 0.020, 0.010, 0.005),
    "burned": (0.030, 0.040, 0.050, 0.120, 0.180, 0.170),
    "stressed": (0.030, 0.060, 0.060, 0.220, 0.190, 0.100),
    "roof": (0.200, 0.210, 0.220, 0.270, 0.300, 0.250),
}

N = 400  # piksel per sisi
RES_M = 20.0


def _seed(bbox: BBox) -> int:
    return int(hashlib.sha1(repr(tuple(round(v, 4) for v in bbox)).encode()).hexdigest()[:8], 16)


def _smooth_noise(rng, sigma: float) -> np.ndarray:
    n = ndimage.gaussian_filter(rng.standard_normal((N, N)), sigma)
    return (n - n.mean()) / (n.std() + 1e-9)


def _ellipse(yy, xx, cy, cx, ry, rx, angle_rad=0.0):
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    dy, dx = yy - cy, xx - cx
    u = dx * c + dy * s
    v = -dx * s + dy * c
    return (u / rx) ** 2 + (v / ry) ** 2 <= 1.0


def _rect(yy, xx, cy, cx, h, w, angle_rad=0.0):
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    dy, dx = yy - cy, xx - cx
    u = dx * c + dy * s
    v = -dx * s + dy * c
    return (np.abs(u) <= w / 2) & (np.abs(v) <= h / 2)


def _line(yy, xx, p0, p1, width_px):
    (y0, x0), (y1, x1) = p0, p1
    vy, vx = y1 - y0, x1 - x0
    L2 = vy * vy + vx * vx
    t = np.clip(((yy - y0) * vy + (xx - x0) * vx) / L2, 0, 1)
    d = np.hypot(yy - (y0 + t * vy), xx - (x0 + t * vx))
    return d <= width_px / 2


def _paint(bands, mask, cover, weight=1.0):
    for i, b in enumerate(BANDS):
        bands[b] = np.where(mask, (1 - weight) * bands[b] + weight * SPECTRA[cover][i], bands[b])


class SyntheticSource:
    name = "demo"
    label = "Data demo sintetis (offline)"

    def load_pair(self, bbox: BBox, before: Period, after: Period, progress=None):
        rng = np.random.default_rng(_seed(bbox))
        if progress:
            progress(0.2, "Membangkitkan citra demo sintetis")
        yy, xx = np.mgrid[0:N, 0:N].astype(np.float32)

        # --- Terrain: gunung di barat laut, dataran rendah di tenggara.
        peak_y, peak_x, sig = 120.0, 110.0, 55.0
        mountain = 900.0 * np.exp(-((yy - peak_y) ** 2 + (xx - peak_x) ** 2) / (2 * sig**2))
        elev = 30.0 + mountain + 25.0 * (1 - (xx + yy) / (2 * N)) + 8.0 * _smooth_noise(rng, 12)
        terrain = terrain_from_dem(elev, (RES_M, RES_M))

        # --- Grid geografis: 8x8 km di pusat AOI (EPSG:4326).
        cx_lon, cy_lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        dlat = RES_M / 110_574
        dlon = RES_M / (111_320 * np.cos(np.radians(cy_lat)))
        transform = Affine(dlon, 0, cx_lon - dlon * N / 2, 0, -dlat, cy_lat + dlat * N / 2)

        # --- Periode sebelum: hutan + sungai + ladang lama.
        canopy = 0.04 * _smooth_noise(rng, 3)
        base = {b: np.full((N, N), SPECTRA["forest"][i], np.float32) for i, b in enumerate(BANDS)}
        base["nir"] = base["nir"] * (1 + canopy)
        river_t = np.linspace(0, 1, 200)
        river_pts = [(330 - 140 * t + 12 * np.sin(t * 18), 400 * t) for t in river_t]
        river = np.zeros((N, N), bool)
        for (y0, x0), (y1, x1) in zip(river_pts[:-1], river_pts[1:]):
            river |= _line(yy, xx, (y0, x0), (y1, x1), 4)
        _paint(base, river, "water")
        old_field = _ellipse(yy, xx, 230, 40, 25, 30)
        _paint(base, old_field, "shrub")

        # Desa lama: campuran atap & kebun pekarangan (heterogen).
        def settlement(mask):
            _paint(base_ref, mask, "shrub")
            _paint(base_ref, mask, "roof", rng.uniform(0.1, 0.9, (N, N)))

        base_ref = base
        village = _ellipse(yy, xx, 25, 215, 16, 20)
        settlement(village)

        before_b = {k: v.copy() for k, v in base.items()}
        after_b = {k: v.copy() for k, v in base.items()}

        # --- 1. Tambang terbuka (nikel laterit) dengan kolam sedimen & jalan angkut.
        mine = np.zeros((N, N), bool)
        for cy, cx, ry, rx, ang in [(70, 290, 22, 30, 0.3), (95, 330, 18, 20, -0.4), (55, 345, 14, 18, 0.9),
                                    (115, 285, 12, 16, 0.0)]:
            mine |= _ellipse(yy, xx, cy, cx, ry, rx, ang)
        mine &= _smooth_noise(rng, 4) > -1.2
        _paint(after_b, mine, "laterite")
        pits = (_ellipse(yy, xx, 72, 292, 7, 10) | _ellipse(yy, xx, 96, 333, 5, 6)) & mine
        _paint(after_b, pits, "turbid_water")
        haul = _line(yy, xx, (118, 290), (200, 360), 2) | _line(yy, xx, (200, 360), (230, 399), 2)
        _paint(after_b, haul & ~mine, "soil")

        # --- 2. Blok tebangan (pembukaan lahan) teratur + jalan sarad.
        blocks = np.zeros((N, N), bool)
        for i in range(3):
            for j in range(2):
                blk = _rect(yy, xx, 270 + j * 34, 250 + i * 34, 28, 28, 0.05)
                blocks |= blk
                _paint(after_b, blk, "shrub", 0.6)
                _paint(after_b, blk, "soil", 0.5)
        skid = _line(yy, xx, (250, 236), (250, 399), 1.6)
        _paint(after_b, skid, "soil", 0.9)

        # --- 3. Longsor di lereng terjal, memanjang searah turunan lereng.
        ang = np.arctan2(170 - peak_y, 175 - peak_x)
        slide_c = (peak_y + 1.15 * sig * np.sin(ang), peak_x + 1.15 * sig * np.cos(ang))
        slide = _ellipse(yy, xx, slide_c[0], slide_c[1], 3.5, 18, ang)
        _paint(after_b, slide, "soil")

        # --- 4. Kebakaran: bentuk tak beraturan.
        fire = ndimage.gaussian_filter(_ellipse(yy, xx, 320, 110, 32, 42).astype(np.float32), 6) + 0.12 * _smooth_noise(rng, 5)
        fire = fire > 0.5
        _paint(after_b, fire, "burned")

        # --- 5. Banjir/genangan di dataran rendah tepi sungai.
        flood = ndimage.binary_dilation(river, iterations=10) & ~river & (xx > 165) & (xx < 215)
        _paint(after_b, flood, "water", 0.95)

        # --- 6. Perluasan permukiman dari desa lama.
        base_ref = after_b
        expansion = _ellipse(yy, xx, 27, 215, 30, 36) & ~village & (_smooth_noise(rng, 3) > -0.8)
        settlement(expansion)

        # --- 7. Stres vegetasi / degradasi alami (musim kering): penurunan NDVI ringan, tidak beraturan.
        stress = (_smooth_noise(rng, 8) > 1.6) & (yy > 150) & (yy < 230) & (xx > 160) & (xx < 260)
        _paint(after_b, stress, "stressed", 0.9)

        # --- Derau sensor & sedikit piksel berawan permanen.
        for bands in (before_b, after_b):
            for b in BANDS:
                bands[b] = np.clip(bands[b] + rng.normal(0, 0.004, (N, N)), 0.001, 1).astype(np.float32)
        cloud = _ellipse(yy, xx, 380, 380, 10, 14)
        for b in BANDS:
            after_b[b][cloud] = np.nan

        if progress:
            progress(0.5, "Citra demo siap")
        crs = "EPSG:4326"
        self._masks = {
            "bbox": bbox, "river": river, "old_field": old_field, "village": village, "mine": mine, "pits": pits,
            "haul": haul, "blocks": blocks, "skid": skid, "slide": slide, "fire": fire, "flood": flood,
            "expansion": expansion, "stress": stress, "yy": yy, "xx": xx, "seed": _seed(bbox),
        }
        return (
            Scene(before_b, transform, crs, before, n_scenes=1, meta={"synthetic": True}),
            Scene(after_b, transform, crs, after, n_scenes=1, meta={"synthetic": True}),
            terrain,
        )


    def load_ancillary(self, scene: Scene, bbox: BBox, before: Period, after: Period, progress=None):
        """Data pendukung sintetis yang konsisten dengan skenario (WorldCover, GSW, Sentinel-1, OSM)."""
        from ..ancillary import Ancillary

        if getattr(self, "_masks", None) is None or self._masks["bbox"] != bbox:
            self.load_pair(bbox, before, after)
        m = self._masks
        rng = np.random.default_rng(m["seed"] + 1)
        if progress:
            progress(0.6, "Membangkitkan data pendukung demo")

        wc = np.full((N, N), 10, np.float32)  # hutan
        wc[m["old_field"]] = 30
        wc[m["village"]] = 50
        wc[m["river"]] = 80

        gsw = np.zeros((N, N), np.float32)
        gsw[ndimage.binary_dilation(m["river"], iterations=10)] = 25  # dataran banjir musiman
        gsw[m["river"]] = 95

        vb = -13.0 + rng.normal(0, 0.4, (N, N))
        vb[m["river"]] = -24
        vb[m["village"]] = -9
        vb[m["old_field"]] = -16
        va = vb + rng.normal(0, 0.3, (N, N))
        for key, val in (("mine", -17.5), ("haul", -16), ("blocks", -15.8), ("skid", -15.5), ("slide", -17),
                         ("fire", -15.5), ("flood", -21), ("expansion", -9.5)):
            va[m[key]] = val + rng.normal(0, 0.4, int(m[key].sum()))
        va[m["pits"]] = -24
        # Stres vegetasi alami: struktur tegakan utuh -> backscatter VH praktis tidak berubah.

        yy, xx = m["yy"], m["xx"]
        road = _line(yy, xx, (392, 0), (392, 399), 1.5) | (_line(yy, xx, (25, 150), (25, 260), 1.2))
        osm_mining = (_ellipse(yy, xx, 70, 290, 22, 30, 0.3)).astype(np.float32)  # sebagian tambang sudah dipetakan
        osm_built = m["village"].astype(np.float32)

        sources = {
            "worldcover": {"status": "ok", "detail": "sintetis (demo)"},
            "gsw": {"status": "ok", "detail": "sintetis (demo)"},
            "s1": {"status": "ok", "detail": "sintetis (demo)"},
            "osm": {"status": "ok", "detail": "sintetis (demo)"},
        }
        return Ancillary(
            layers={
                "worldcover": wc, "gsw_occurrence": gsw,
                "vh_before_db": vb.astype(np.float32), "vh_after_db": va.astype(np.float32),
                "osm_road": road.astype(np.float32), "osm_mining": osm_mining, "osm_built": osm_built,
            },
            sources=sources,
        )
