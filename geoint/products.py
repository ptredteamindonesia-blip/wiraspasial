"""Produk visual: citra true-color sebelum/sesudah & peta kelas perubahan (PNG untuk overlay peta)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from rasterio.warp import transform_bounds

from .classifier import CLASSES
from .sources.base import Scene


# Stretch reflektansi tetap (bukan persentil) agar warna antar periode & antar AOI sebanding,
# dan tanah terbuka yang terang tidak jenuh putih di wilayah yang didominasi hutan.
TC_MAX = 0.22
TC_GAMMA = 0.75


def true_color(scene: Scene) -> Image.Image:
    rgb = np.dstack([scene.bands["red"], scene.bands["green"], scene.bands["blue"]])
    valid = np.isfinite(rgb).all(axis=2)
    img = np.clip(np.nan_to_num(rgb) / TC_MAX, 0, 1) ** TC_GAMMA
    rgba = np.dstack([(img * 255).astype(np.uint8), (valid * 255).astype(np.uint8)])
    return Image.fromarray(rgba, "RGBA")


def class_map(labels: np.ndarray, label_class: dict[int, str]) -> Image.Image:
    rgba = np.zeros((*labels.shape, 4), np.uint8)
    for lbl, cls in label_class.items():
        hexc = CLASSES[cls]["color"].lstrip("#")
        color = [int(hexc[i : i + 2], 16) for i in (0, 2, 4)] + [210]
        rgba[labels == lbl] = color
    return Image.fromarray(rgba, "RGBA")


def wgs84_bounds(scene: Scene) -> list[list[float]]:
    h, w = scene.shape
    t = scene.transform
    xs = (t.c, t.c + t.a * w)
    ys = (t.f, t.f + t.e * h)
    west, south, east, north = transform_bounds(scene.crs, "EPSG:4326", min(xs), min(ys), max(xs), max(ys))
    return [[south, west], [north, east]]  # format Leaflet


def write_products(out_dir: Path, before: Scene, after: Scene, labels: np.ndarray, label_class: dict[int, str]) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    true_color(before).save(out_dir / "before.png", optimize=True)
    true_color(after).save(out_dir / "after.png", optimize=True)
    class_map(labels, label_class).save(out_dir / "changes.png", optimize=True)
    return {
        "bounds": wgs84_bounds(before),
        "before": "before.png",
        "after": "after.png",
        "changes": "changes.png",
    }
