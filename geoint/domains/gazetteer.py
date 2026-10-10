"""Gazetteer provinsi & kabupaten/kota Indonesia (OpenStreetMap) untuk geolokasi teks dan nama wilayah.

Diunduh sekali dari Overpass (relasi admin_level 4 & 5 beserta titik pusat) lalu disimpan di
data/gazetteer.json. Geolokasi bersifat perkiraan: titik pusat kabupaten/kota atau provinsi.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
from functools import lru_cache

from ..config import settings
from .common import haversine_km, http_get

log = logging.getLogger(__name__)
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
QUERY = ('[out:json][timeout:170];area["ISO3166-1"="ID"][admin_level=2]->.id;'
         '(relation["boundary"="administrative"]["admin_level"~"^(4|5)$"](area.id););out tags center;')
_lock = threading.Lock()

# Kata tunggal yang juga kata umum bahasa Indonesia: hanya dicocokkan bila diawali "Kabupaten"/"Kota".
COMMON_WORDS = {"batu", "buru", "batang", "bone", "banjar", "lingga", "tegal", "kota", "timur", "barat", "utara",
                "selatan", "tengah", "raya", "jaya", "baru", "sabu", "pati", "kaur", "ende", "belu", "muna", "karo", "toba"}


def _path():
    return settings.data_dir / "gazetteer.json"


def ensure(download: bool = True) -> list[dict]:
    """Muat gazetteer; unduh dari OSM bila belum ada."""
    p = _path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if not download:
        return []
    with _lock:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        raw, last = None, None
        for url in OVERPASS:  # server cermin cadangan bila server utama bermasalah
            try:
                raw = http_get(url, timeout=200, retries=0, data=urllib.parse.urlencode({"data": QUERY}).encode())
                break
            except Exception as exc:
                last = exc
        if raw is None:
            raise RuntimeError(f"Gazetteer tidak dapat diunduh dari Overpass: {last}")
        els = json.loads(raw)["elements"]
        places = []
        for e in els:
            t, c = e.get("tags", {}), e.get("center")
            if not c or not t.get("name"):
                continue
            places.append({"name": t["name"], "level": "provinsi" if t.get("admin_level") == "4" else "kabkota",
                           "lon": c["lon"], "lat": c["lat"], "osm_id": e["id"]})
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
        _index.cache_clear()
        log.info("Gazetteer diunduh: %d wilayah", len(places))
        return places


def _aliases(place: dict) -> list[tuple[str, bool]]:
    """(alias, eksplisit) — eksplisit = memakai prefix Kabupaten/Kota/Provinsi."""
    name = place["name"].strip()
    low = name.lower()
    out = [(low, True)]
    base = re.sub(r"^(kabupaten|kota|provinsi|daerah istimewa|kab\.)\s+", "", low)
    if base != low:
        if low.startswith("kabupaten "):
            out.append((f"kab. {base}", True))
            out.append((f"kab {base}", True))
        out.append((base, False))
    elif place["level"] == "provinsi":
        out.append((f"provinsi {low}", True))
    return out


@lru_cache(maxsize=1)
def _index():
    places = ensure(download=False)
    entries = []
    for pl in places:
        for alias, explicit in _aliases(pl):
            if not explicit and (alias in COMMON_WORDS or len(alias) < 4):
                continue
            entries.append((alias, explicit, pl))
    # Alias terpanjang dicocokkan dulu ("kabupaten bandung barat" sebelum "bandung").
    entries.sort(key=lambda e: -len(e[0]))
    return entries


def geocode_text(text: str) -> dict | None:
    """Cari nama wilayah di teks. Prioritas: kab/kota (lebih presisi) lalu provinsi; alias terpanjang menang."""
    if not text:
        return None
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    best = None
    for alias, explicit, pl in _index():
        m = re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", low)
        if not m:
            continue
        score = (2 if pl["level"] == "kabkota" else 1, len(alias), explicit)
        # Nama polos yang dipakai kab & kota sekaligus (mis. "Bogor") -> pilih kota kecuali ada kata "kabupaten".
        if best is None or score > best[0]:
            best = (score, pl, alias)
    if best is None:
        return None
    _, pl, alias = best
    return {"name": pl["name"], "level": pl["level"], "lon": pl["lon"], "lat": pl["lat"], "matched": alias,
            "precision_km": 40 if pl["level"] == "kabkota" else 150}


def nearest(lon: float, lat: float, level: str = "kabkota", k: int = 1) -> list[dict]:
    places = [p for p in ensure(download=False) if p["level"] == level]
    ranked = sorted(places, key=lambda p: haversine_km(lon, lat, p["lon"], p["lat"]))
    return [{**p, "dist_km": round(haversine_km(lon, lat, p["lon"], p["lat"]), 1)} for p in ranked[:k]]


def within(lon: float, lat: float, radius_km: float, level: str = "kabkota") -> list[dict]:
    out = []
    for p in ensure(download=False):
        if p["level"] != level:
            continue
        d = haversine_km(lon, lat, p["lon"], p["lat"])
        if d <= radius_km:
            out.append({**p, "dist_km": round(d, 1)})
    return sorted(out, key=lambda p: p["dist_km"])


# --------------------------------------------------------------------------- batas negara
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson"
_shape_cache = {}


def indonesia_shape(buffer_deg: float = 0.02):
    """Daratan Indonesia (Natural Earth 1:10 jt, domain publik), disangga ~2 km untuk toleransi garis pantai.
    Diunduh sekali ke data/indonesia.geojson. None bila belum bisa diunduh."""
    import shapely
    from shapely.geometry import shape

    key = round(buffer_deg, 4)
    if key in _shape_cache:
        return _shape_cache[key]
    p = settings.data_dir / "indonesia.geojson"
    if not p.exists():
        try:
            data = json.loads(http_get(NE_URL, timeout=120))
            feat = next(f for f in data["features"] if f["properties"].get("ADM0_A3") == "IDN")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(feat["geometry"]), encoding="utf-8")
        except Exception as exc:
            log.warning("Batas Indonesia belum bisa diunduh: %s", exc)
            return None
    g = shape(json.loads(p.read_text(encoding="utf-8")))
    if buffer_deg:
        g = g.buffer(buffer_deg)
    shapely.prepare(g)
    _shape_cache[key] = g
    return g
