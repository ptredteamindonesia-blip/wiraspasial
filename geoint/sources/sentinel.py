"""Sumber data nyata: Sentinel-2 L2A + Copernicus DEM GLO-30 dari Microsoft Planetary Computer.

Tidak memerlukan API key. Komposit dibuat per periode dengan median piksel bebas awan
(berdasarkan Scene Classification Layer / SCL), sehingga cocok untuk wilayah tropis berawan.
"""

from __future__ import annotations

import logging
import math
import warnings

import numpy as np

from ..config import settings
from .base import BANDS, BBox, Period, Scene, Terrain, terrain_from_dem

log = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
S2_ASSETS = {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08", "swir1": "B11", "swir2": "B12"}
# SCL: 0 no-data, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow
SCL_INVALID = (0, 1, 3, 8, 9, 10, 11)


class DataUnavailable(RuntimeError):
    pass


def utm_epsg(bbox: BBox) -> int:
    lon = (bbox[0] + bbox[2]) / 2
    lat = (bbox[1] + bbox[3]) / 2
    zone = int(math.floor((lon + 180) / 6)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def bbox_area_km2(bbox: BBox) -> float:
    lat = math.radians((bbox[1] + bbox[3]) / 2)
    return abs(bbox[2] - bbox[0]) * 111.32 * math.cos(lat) * abs(bbox[3] - bbox[1]) * 110.574


class SentinelSource:
    name = "sentinel2"
    label = "Sentinel-2 L2A (Planetary Computer)"

    MAX_PIXELS = 2_500_000

    def __init__(self) -> None:
        import planetary_computer
        import pystac_client

        self._catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)

    # ------------------------------------------------------------------ helpers
    def _geobox(self, bbox: BBox):
        from odc.geo.geobox import GeoBox
        from odc.geo.geom import BoundingBox

        epsg = f"EPSG:{utm_epsg(bbox)}"
        res = settings.resolution_m
        npx = bbox_area_km2(bbox) * 1e6 / res**2
        if npx > self.MAX_PIXELS:
            # Turunkan resolusi otomatis agar memori tetap aman.
            res = math.ceil(res * math.sqrt(npx / self.MAX_PIXELS))
            log.info("AOI besar: resolusi dinaikkan ke %s m", res)
        bb = BoundingBox(*bbox, crs="EPSG:4326").to_crs(epsg)
        return GeoBox.from_bbox(bb, crs=epsg, resolution=res)

    def _search(self, bbox: BBox, period: Period):
        search = self._catalog.search(
            collections=["sentinel-2-l2a"],
            bbox=bbox,
            datetime=f"{period[0]}/{period[1]}",
            query={"eo:cloud_cover": {"lt": settings.max_cloud_pct}},
        )
        items = sorted(search.items(), key=lambda it: it.properties.get("eo:cloud_cover", 100))
        return items[: settings.max_scenes_per_period * 3]

    def _composite(self, bbox: BBox, period: Period, geobox) -> Scene:
        import odc.stac

        items = self._search(bbox, period)
        if not items:
            raise DataUnavailable(
                f"Tidak ada citra Sentinel-2 dengan awan < {settings.max_cloud_pct:.0f}% pada {period[0]}..{period[1]}. "
                "Perlebar rentang tanggal."
            )
        # Batasi jumlah tanggal akuisisi yang dimuat (sudah urut dari yang paling sedikit awan).
        by_day: dict[str, list] = {}
        for it in items:
            by_day.setdefault(it.datetime.strftime("%Y-%m-%d"), []).append(it)
        days = list(by_day)[: settings.max_scenes_per_period]
        items = [it for d in days for it in by_day[d]]
        baseline = {d: float(by_day[d][0].properties.get("s2:processing_baseline", "0") or 0) for d in days}

        ds = odc.stac.load(
            items,
            bands=list(S2_ASSETS.values()) + ["SCL"],
            geobox=geobox,
            groupby="solar_day",
            resampling={"SCL": "nearest", "*": "bilinear"},
            chunks=None,
        )
        scl = ds["SCL"].values
        clear = ~np.isin(scl, SCL_INVALID)
        # Offset -1000 DN berlaku untuk processing baseline >= 04.00 (sejak Jan 2022).
        offsets = np.array(
            [-1000.0 if baseline.get(str(t)[:10], 0) >= 4.0 else 0.0 for t in ds.time.values], dtype=np.float32
        )[:, None, None]

        bands: dict[str, np.ndarray] = {}
        for name, asset in S2_ASSETS.items():
            dn = ds[asset].values.astype(np.float32)
            refl = np.where(clear & (dn > 0), (dn + offsets) / 10_000.0, np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # piksel yang selalu berawan -> NaN
                bands[name] = np.nanmedian(refl, axis=0).astype(np.float32)
            del dn, refl
        valid_frac = float(np.isfinite(bands["red"]).mean())
        return Scene(
            bands=bands,
            transform=geobox.affine,
            crs=str(geobox.crs),
            period=period,
            n_scenes=int(ds.sizes["time"]),
            meta={"dates": [str(t)[:10] for t in ds.time.values], "valid_fraction": valid_frac},
        )

    def _terrain(self, bbox: BBox, geobox) -> Terrain:
        import odc.stac

        items = list(self._catalog.search(collections=["cop-dem-glo-30"], bbox=bbox).items())
        if not items:
            log.warning("DEM tidak tersedia, terrain diasumsikan datar")
            dem = np.zeros(geobox.shape, dtype=np.float32)
        else:
            ds = odc.stac.load(items, bands=["data"], geobox=geobox, resampling="bilinear", chunks=None)
            dem = ds["data"].isel(time=0).values.astype(np.float32) if "time" in ds.dims else ds["data"].values
        res = abs(geobox.affine.a), abs(geobox.affine.e)
        return terrain_from_dem(dem, res)

    # ------------------------------------------------------------------ public
    def load_pair(self, bbox: BBox, before: Period, after: Period, progress=None):
        def report(p, msg):
            if progress:
                progress(p, msg)

        geobox = self._geobox(bbox)
        report(0.10, f"Memuat komposit Sentinel-2 periode sebelum ({before[0]}..{before[1]})")
        b = self._composite(bbox, before, geobox)
        report(0.35, f"Memuat komposit Sentinel-2 periode sesudah ({after[0]}..{after[1]})")
        a = self._composite(bbox, after, geobox)
        report(0.55, "Memuat DEM Copernicus GLO-30 untuk analisa lereng")
        t = self._terrain(bbox, geobox)
        return b, a, t


    def load_ancillary(self, scene: Scene, bbox: BBox, before: Period, after: Period, progress=None):
        from ..ancillary import load_ancillary

        return load_ancillary(scene, bbox, before, after, progress)


__all__ = ["SentinelSource", "DataUnavailable", "BANDS", "utm_epsg", "bbox_area_km2"]
