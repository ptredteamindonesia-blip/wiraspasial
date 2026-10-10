"""Indeks spektral untuk analisa vegetasi, tanah terbuka, air, dan area terbakar.

Semua input berupa reflektansi permukaan (0..1) dalam bentuk numpy array.
"""

from __future__ import annotations

import numpy as np


def _nd(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (a - b) / (a + b)
    return np.where(np.isfinite(out), out, np.nan).astype(np.float32)


def ndvi(b: dict[str, np.ndarray]) -> np.ndarray:
    """Normalized Difference Vegetation Index — kerapatan vegetasi hijau."""
    return _nd(b["nir"], b["red"])


def nbr(b: dict[str, np.ndarray]) -> np.ndarray:
    """Normalized Burn Ratio — sensitif terhadap area terbakar."""
    return _nd(b["nir"], b["swir2"])


def ndwi(b: dict[str, np.ndarray]) -> np.ndarray:
    """NDWI (McFeeters) — badan air terbuka bernilai positif."""
    return _nd(b["green"], b["nir"])


def bsi(b: dict[str, np.ndarray]) -> np.ndarray:
    """Bare Soil Index — tanah terbuka (tambang, galian, longsoran) bernilai tinggi."""
    return _nd(b["swir1"] + b["red"], b["nir"] + b["blue"])


def ferric(b: dict[str, np.ndarray]) -> np.ndarray:
    """Rasio red/blue — tanah laterit kaya besi (mis. tambang nikel Sulawesi/Maluku) bernilai tinggi."""
    with np.errstate(divide="ignore", invalid="ignore"):
        out = b["red"] / b["blue"]
    return np.where(np.isfinite(out), out, np.nan).astype(np.float32)


def turbidity(b: dict[str, np.ndarray]) -> np.ndarray:
    """Normalized Difference Turbidity Index — air keruh bersedimen (kolam tambang) bernilai tinggi."""
    return _nd(b["red"], b["green"])


def compute_all(bands: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {
        "ndvi": ndvi(bands),
        "nbr": nbr(bands),
        "ndwi": ndwi(bands),
        "bsi": bsi(bands),
        "ferric": ferric(bands),
        "ndti": turbidity(bands),
    }
