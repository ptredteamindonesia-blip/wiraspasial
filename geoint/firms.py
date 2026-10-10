"""Integrasi opsional NASA FIRMS (titik panas VIIRS) untuk memperkuat deteksi kebakaran.

Aktif bila environment variable FIRMS_MAP_KEY diisi (daftar gratis di
https://firms.modaps.eosdis.nasa.gov/api/map_key/). API area FIRMS membatasi rentang
per permintaan, jadi rentang dipecah menjadi beberapa permintaan (maks. MAX_REQUESTS).
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from datetime import date, timedelta

from .config import settings

log = logging.getLogger(__name__)

API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/{days}/{date}"
CHUNK_DAYS = 5
MAX_REQUESTS = 36  # ~180 hari terakhir sebelum akhir periode "sesudah"


def fetch_hotspots(bbox, start: str, end: str) -> list[tuple[float, float]]:
    """Kembalikan daftar (lon, lat) titik panas di bbox antara start..end. Kosong bila nonaktif/gagal."""
    if not settings.firms_map_key:
        return []
    d_start, d_end = date.fromisoformat(start), date.fromisoformat(end)
    area = ",".join(f"{v:.4f}" for v in bbox)
    points: list[tuple[float, float]] = []
    cursor = d_end
    for _ in range(MAX_REQUESTS):
        if cursor < d_start:
            break
        chunk_start = max(d_start, cursor - timedelta(days=CHUNK_DAYS - 1))
        days = (cursor - chunk_start).days + 1
        # Arsip "SP" (standard processing) untuk data lama, "NRT" untuk ~2 bulan terakhir.
        source = "VIIRS_SNPP_NRT" if (date.today() - chunk_start).days < 60 else "VIIRS_SNPP_SP"
        url = API.format(key=settings.firms_map_key, source=source, area=area, days=days, date=chunk_start.isoformat())
        try:
            from .domains.common import SSL_CONTEXT

            with urllib.request.urlopen(url, timeout=30, context=SSL_CONTEXT) as resp:
                text = resp.read().decode("utf-8", "replace")
            for row in csv.DictReader(io.StringIO(text)):
                try:
                    points.append((float(row["longitude"]), float(row["latitude"])))
                except (KeyError, ValueError):
                    continue
        except Exception as exc:  # jaringan/kuota: jangan gagalkan analisa
            log.warning("FIRMS gagal (%s): %s", chunk_start, exc)
            break
        cursor = chunk_start - timedelta(days=1)
    return points
