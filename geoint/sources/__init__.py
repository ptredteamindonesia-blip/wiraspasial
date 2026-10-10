from __future__ import annotations

from .base import BBox, DataSource, Period, Scene, Terrain
from .synthetic import SyntheticSource

SOURCES = {
    "sentinel2": "Sentinel-2 L2A (Planetary Computer)",
    "demo": "Data demo sintetis (offline)",
}


def get_source(name: str) -> DataSource:
    if name == "demo":
        return SyntheticSource()
    if name == "sentinel2":
        from .sentinel import SentinelSource

        return SentinelSource()
    raise ValueError(f"Sumber data tidak dikenal: {name}")


__all__ = ["BBox", "DataSource", "Period", "Scene", "Terrain", "SOURCES", "get_source"]
