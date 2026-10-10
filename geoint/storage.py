"""Penyimpanan data mentah analisa (komposit sebelum/sesudah + DEM) sebagai GeoTIFF.

Dengan data ini tersimpan, analisa yang terputus karena server berhenti dapat dilanjutkan,
dan analisa dapat dijalankan ulang (mis. setelah aturan klasifikasi diperbarui) tanpa
mengunduh citra satelit lagi.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import rasterio

from .config import settings
from .sources.base import BANDS, Scene, Terrain, terrain_from_dem

PROFILE = {"driver": "GTiff", "compress": "deflate", "predictor": 3, "tiled": True, "blockxsize": 256, "blockysize": 256}


def analysis_dir(analysis_id: int) -> Path:
    return settings.products_dir / str(analysis_id)


def inputs_dir(analysis_id: int) -> Path:
    return analysis_dir(analysis_id) / "inputs"


def _write(path: Path, arrays: list[np.ndarray], scene: Scene, names: list[str]) -> None:
    h, w = arrays[0].shape
    profile = {**PROFILE, "height": h, "width": w, "count": len(arrays), "dtype": "float32",
               "crs": scene.crs, "transform": scene.transform, "nodata": np.nan}
    if h < 256 or w < 256:
        profile.pop("tiled"), profile.pop("blockxsize"), profile.pop("blockysize")
    tmp = path.with_suffix(".tmp.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        for i, (arr, name) in enumerate(zip(arrays, names), start=1):
            dst.write(arr.astype(np.float32), i)
            dst.set_band_description(i, name)
    tmp.replace(path)  # tulis atomik: file tidak pernah setengah jadi


def save_inputs(analysis_id: int, before: Scene, after: Scene, terrain: Terrain) -> Path:
    d = inputs_dir(analysis_id)
    d.mkdir(parents=True, exist_ok=True)
    _write(d / "before.tif", [before.bands[b] for b in BANDS], before, list(BANDS))
    _write(d / "after.tif", [after.bands[b] for b in BANDS], after, list(BANDS))
    _write(d / "dem.tif", [terrain.elevation], before, ["elevation_m"])
    meta = {
        "before": {"period": list(before.period), "n_scenes": before.n_scenes, "meta": before.meta},
        "after": {"period": list(after.period), "n_scenes": after.n_scenes, "meta": after.meta},
    }
    # meta.json ditulis terakhir: keberadaannya menandakan data lengkap.
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return d


def has_inputs(analysis_id: int) -> bool:
    d = inputs_dir(analysis_id)
    return all((d / f).exists() for f in ("before.tif", "after.tif", "dem.tif", "meta.json"))


def load_inputs(analysis_id: int) -> tuple[Scene, Scene, Terrain] | None:
    if not has_inputs(analysis_id):
        return None
    d = inputs_dir(analysis_id)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    scenes = []
    for key in ("before", "after"):
        with rasterio.open(d / f"{key}.tif") as src:
            bands = {b: src.read(i + 1).astype(np.float32) for i, b in enumerate(BANDS)}
            m = meta[key]
            scenes.append(Scene(bands, src.transform, src.crs.to_string(), tuple(m["period"]),
                                n_scenes=m["n_scenes"], meta=m["meta"]))
    with rasterio.open(d / "dem.tif") as src:
        dem = src.read(1)
    return scenes[0], scenes[1], terrain_from_dem(dem, scenes[0].pixel_size_m)


def delete_analysis_files(analysis_id: int) -> None:
    shutil.rmtree(analysis_dir(analysis_id), ignore_errors=True)


# --------------------------------------------------------------------------- data pendukung
def save_ancillary(analysis_id: int, scene: Scene, anc) -> None:
    d = inputs_dir(analysis_id)
    d.mkdir(parents=True, exist_ok=True)
    names = [n for n in anc.layers if anc.layers[n] is not None and anc.layers[n].shape == scene.shape]
    if names:
        _write(d / "ancillary.tif", [anc.layers[n] for n in names], scene, names)
    (d / "ancillary.json").write_text(json.dumps({"layers": names, "sources": anc.sources}, indent=1), encoding="utf-8")


def load_ancillary(analysis_id: int):
    from .ancillary import Ancillary

    d = inputs_dir(analysis_id)
    if not (d / "ancillary.json").exists():
        return None
    meta = json.loads((d / "ancillary.json").read_text(encoding="utf-8"))
    layers = {}
    if meta["layers"] and (d / "ancillary.tif").exists():
        with rasterio.open(d / "ancillary.tif") as src:
            layers = {n: src.read(i + 1).astype(np.float32) for i, n in enumerate(meta["layers"])}
    return Ancillary(layers=layers, sources=meta["sources"])
