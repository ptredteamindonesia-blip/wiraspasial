"""Struktur data citra & antarmuka sumber data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from affine import Affine

BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")

BBox = tuple[float, float, float, float]  # (west, south, east, north) dalam derajat
Period = tuple[str, str]  # (YYYY-MM-DD, YYYY-MM-DD)


@dataclass
class Scene:
    """Komposit citra bebas awan untuk satu periode, pada grid raster tertentu."""

    bands: dict[str, np.ndarray]  # reflektansi 0..1, float32, NaN = tidak valid
    transform: Affine
    crs: str
    period: Period
    n_scenes: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return self.bands["red"].shape

    @property
    def valid(self) -> np.ndarray:
        v = np.ones(self.shape, dtype=bool)
        for b in BANDS:
            v &= np.isfinite(self.bands[b])
        return v

    @property
    def pixel_area_m2(self) -> float:
        if self.crs.upper() in ("EPSG:4326", "OGC:CRS84"):
            # Aproksimasi pada lintang tengah (dipakai sumber demo).
            lat = self.transform.f + self.transform.e * self.shape[0] / 2
            mx = abs(self.transform.a) * 111_320 * np.cos(np.radians(lat))
            my = abs(self.transform.e) * 110_574
            return float(mx * my)
        return float(abs(self.transform.a * self.transform.e))

    @property
    def pixel_size_m(self) -> tuple[float, float]:
        if self.crs.upper() in ("EPSG:4326", "OGC:CRS84"):
            lat = self.transform.f + self.transform.e * self.shape[0] / 2
            return (abs(self.transform.a) * 111_320 * float(np.cos(np.radians(lat))), abs(self.transform.e) * 110_574)
        return (abs(self.transform.a), abs(self.transform.e))


@dataclass
class Terrain:
    elevation: np.ndarray  # meter
    slope_deg: np.ndarray
    aspect_vec: tuple[np.ndarray, np.ndarray]  # arah turun lereng (dx, dy) di ruang piksel, ternormalisasi


def terrain_from_dem(dem: np.ndarray, px_m: tuple[float, float]) -> Terrain:
    dem = dem.astype(np.float32)
    if np.isnan(dem).any():
        dem = np.where(np.isnan(dem), np.nanmean(dem) if np.isfinite(dem).any() else 0.0, dem)
    dzdy, dzdx = np.gradient(dem, px_m[1], px_m[0])
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)
    mag = np.hypot(dzdx, dzdy) + 1e-9
    # Arah turun lereng = -gradien.
    return Terrain(elevation=dem, slope_deg=slope, aspect_vec=(-dzdx / mag, -dzdy / mag))


class DataSource(Protocol):
    name: str

    def load_pair(self, bbox: BBox, before: Period, after: Period, progress=None) -> tuple[Scene, Scene, Terrain]:
        """Muat komposit periode sebelum & sesudah pada grid yang sama, plus data terrain."""
        ...

    def load_ancillary(self, scene: Scene, bbox: BBox, before: Period, after: Period, progress=None):
        """Muat data pendukung (radar, tutupan lahan, riwayat air, OSM) pada grid `scene`."""
        ...
