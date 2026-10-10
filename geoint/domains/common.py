"""Fondasi bersama modul domain: registri sumber data, hak akses, HTTP, penyimpanan peristiwa, status feed."""

from __future__ import annotations

import json
import logging
import math
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import settings

log = logging.getLogger(__name__)
# UA jujur untuk API data (Overpass menolak UA bergaya browser dengan HTTP 406);
# UA bergaya browser hanya untuk situs berita yang menolak klien non-browser.
USER_AGENT = "WIRA-SPASIAL/0.2 (national geospatial monitoring)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WIRA-SPASIAL/0.2"


def ssl_context() -> ssl.SSLContext:
    """Pakai bundel CA certifi: penyimpanan CA bawaan sebagian instalasi Python/Windows sudah usang."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CONTEXT = ssl_context()

# --------------------------------------------------------------------------- domain
DOMAINS = {
    "sat": {"code": "SAT", "title": "Satellite", "desc": "Perubahan lahan, vegetasi, pesisir"},
    "dis": {"code": "DIS", "title": "Disaster", "desc": "Gempa, hotspot, paparan risiko"},
    "sea": {"code": "SEA", "title": "Maritime", "desc": "Port analytics, AIS berlisensi"},
    "mne": {"code": "MNE", "title": "Mining", "desc": "Perubahan lahan + lapisan izin"},
    "inf": {"code": "INF", "title": "Infrastructure", "desc": "Jalan, bangunan, kawasan urban"},
    "os": {"code": "OS", "title": "OSINT Fusion", "desc": "Berita dan geolokasi peristiwa"},
}

# Tingkat akses data. Data di atas tingkat pemanggil disembunyikan.
ACCESS_LEVELS = {"publik": 0, "terbatas": 1, "berlisensi": 2}

# --------------------------------------------------------------------------- registri sumber
# quality: resmi (instansi pemerintah) | ilmiah (lembaga riset/antariksa) | komunitas (OSM) |
#          OSINT (media/berita, perlu verifikasi) | estimasi (turunan algoritmik) | sesuai sumber
SOURCES = [
    {"id": "sentinel2", "domain": "sat", "label": "Sentinel-2 L2A (optik 10–20 m)", "quality": "ilmiah", "access": "publik",
     "license": "Copernicus — terbuka", "kind": "analisa"},
    {"id": "sentinel1", "domain": "sat", "label": "Sentinel-1 RTC (radar)", "quality": "ilmiah", "access": "publik",
     "license": "Copernicus — terbuka", "kind": "analisa"},
    {"id": "worldcover", "domain": "sat", "label": "ESA WorldCover 2021", "quality": "ilmiah", "access": "publik",
     "license": "CC BY 4.0", "kind": "analisa"},
    {"id": "jrc_gsw", "domain": "sat", "label": "JRC Global Surface Water", "quality": "ilmiah", "access": "publik",
     "license": "Copernicus — terbuka", "kind": "analisa"},
    {"id": "bmkg", "domain": "dis", "label": "BMKG — gempa terkini & dirasakan", "quality": "resmi", "access": "publik",
     "license": "Data terbuka BMKG (cantumkan sumber)", "kind": "feed", "interval_min": 15},
    {"id": "usgs", "domain": "dis", "label": "USGS — katalog gempa (M≥4)", "quality": "ilmiah", "access": "publik",
     "license": "Domain publik (USGS)", "kind": "feed", "interval_min": 30},
    {"id": "firms", "domain": "dis", "label": "NASA FIRMS — hotspot VIIRS 24 jam", "quality": "ilmiah", "access": "publik",
     "license": "NASA — terbuka", "kind": "feed", "interval_min": 60},
    {"id": "s1_ports", "domain": "sea", "label": "Deteksi kapal radar Sentinel-1", "quality": "estimasi", "access": "publik",
     "license": "Turunan Copernicus — terbuka", "kind": "feed", "interval_min": 720},
    {"id": "aisstream", "domain": "sea", "label": "AIS realtime (aisstream.io)", "quality": "komunitas", "access": "berlisensi",
     "license": "Sesuai ketentuan aisstream.io — perlu API key", "kind": "feed", "interval_min": 60, "requires": "AISSTREAM_API_KEY"},
    {"id": "ais_import", "domain": "sea", "label": "Impor AIS berlisensi (CSV)", "quality": "sesuai sumber", "access": "berlisensi",
     "license": "Sesuai lisensi penyedia AIS", "kind": "impor"},
    {"id": "permits", "domain": "mne", "label": "Lapisan izin & kawasan (impor GeoJSON/Shapefile/ArcGIS)", "quality": "sesuai sumber",
     "access": "terbatas", "license": "Sesuai pemilik data (mis. ESDM/KLHK)", "kind": "impor"},
    {"id": "osm", "domain": "inf", "label": "OpenStreetMap (jalan, bangunan, landuse)", "quality": "komunitas", "access": "publik",
     "license": "ODbL — © kontributor OpenStreetMap", "kind": "analisa"},
    {"id": "rss_media", "domain": "os", "label": "RSS media nasional (Antara, Tempo, CNN Indonesia)", "quality": "OSINT",
     "access": "publik", "license": "Judul & tautan; hak cipta milik media", "kind": "feed", "interval_min": 60},
    {"id": "gnews", "domain": "os", "label": "Google News RSS (kata kunci)", "quality": "OSINT", "access": "publik",
     "license": "Hanya untuk penggunaan pribadi non-komersial", "kind": "feed", "interval_min": 60},
    {"id": "gazetteer", "domain": "os", "label": "Gazetteer provinsi & kab/kota (OSM)", "quality": "komunitas", "access": "publik",
     "license": "ODbL — © kontributor OpenStreetMap", "kind": "referensi"},
]
SOURCE_BY_ID = {s["id"]: s for s in SOURCES}


def source_available(src: dict) -> tuple[bool, str]:
    req = src.get("requires")
    if req == "AISSTREAM_API_KEY" and not settings.aisstream_key:
        return False, "perlu API key (AISSTREAM_API_KEY)"
    if src["id"] == "gnews" and not settings.google_news:
        return False, "dinonaktifkan (GEOINT_GOOGLE_NEWS=0)"
    return True, ""


def feed_enabled(source_id: str) -> bool:
    return db.get_setting(f"feed_enabled:{source_id}", "1") == "1"


def set_feed_enabled(source_id: str, on: bool) -> None:
    db.set_setting(f"feed_enabled:{source_id}", "1" if on else "0")


def feed_status(source_id: str) -> dict:
    raw = db.get_setting(f"feed_status:{source_id}")
    return json.loads(raw) if raw else {}


def record_feed(source_id: str, ok: bool, count: int = 0, detail: str = "") -> None:
    prev = feed_status(source_id)
    st = {"last_run": db.now_iso(), "ok": ok, "count": count, "detail": detail[:300],
          "last_ok": db.now_iso() if ok else prev.get("last_ok")}
    db.set_setting(f"feed_status:{source_id}", json.dumps(st))


def feed_due(source_id: str, now: datetime | None = None) -> bool:
    src = SOURCE_BY_ID[source_id]
    last = feed_status(source_id).get("last_run")
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(last) + timedelta(minutes=src.get("interval_min", 60)) <= now


def registry() -> list[dict]:
    out = []
    for s in SOURCES:
        avail, why = source_available(s)
        out.append({**s, "available": avail, "unavailable_reason": why,
                    "enabled": feed_enabled(s["id"]) if s["kind"] == "feed" else None, "status": feed_status(s["id"])})
    return out


# --------------------------------------------------------------------------- hak akses
def caller_level(token: str | None) -> int:
    """Tanpa GEOINT_ACCESS_TOKEN: mode pengguna tunggal, semua data terbuka."""
    if not settings.access_token:
        return ACCESS_LEVELS["berlisensi"]
    return ACCESS_LEVELS["berlisensi"] if token and token == settings.access_token else ACCESS_LEVELS["publik"]


def allowed(access: str, level: int) -> bool:
    return ACCESS_LEVELS.get(access, 2) <= level


# --------------------------------------------------------------------------- HTTP
def http_get(url: str, timeout: int = 60, retries: int = 2, data: bytes | None = None, ua: str = USER_AGENT) -> bytes:
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if i < retries:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def http_json(url: str, **kw):
    return json.loads(http_get(url, **kw).decode("utf-8"))


# --------------------------------------------------------------------------- peristiwa
def upsert_event(domain: str, kind: str, source: str, external_id: str, event_time: str, lon, lat, title: str,
                 value=None, properties: dict | None = None, access: str = "publik") -> bool:
    """Simpan peristiwa; kembalikan True bila baru (belum pernah tersimpan)."""
    with db.connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO events (domain, kind, source, external_id, event_time, lon, lat, title, value, properties,"
            " access, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (domain, kind, source, str(external_id), event_time, lon, lat, title, value,
             json.dumps(properties or {}, ensure_ascii=False), access, db.now_iso()),
        )
        return cur.rowcount > 0


def upsert_events_bulk(rows: list[tuple]) -> int:
    """Simpan banyak peristiwa dalam satu transaksi. rows: (domain, kind, source, external_id, event_time, lon, lat,
    title, value, properties_dict, access). Kembalikan jumlah baris baru."""
    now = db.now_iso()
    data = [(*r[:9], json.dumps(r[9] or {}, ensure_ascii=False), r[10], now) for r in rows]
    with db.connect() as c:
        before = c.total_changes
        c.executemany(
            "INSERT OR IGNORE INTO events (domain, kind, source, external_id, event_time, lon, lat, title, value, properties,"
            " access, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", data)
        return c.total_changes - before


def query_events(kind: str | None = None, domain: str | None = None, since: str | None = None, level: int = 2,
                 limit: int = 2000, bbox=None, extra_where: str = "", params: tuple = ()) -> list[dict]:
    where, p = [], []
    if kind:
        where.append("kind = ?"), p.append(kind)
    if domain:
        where.append("domain = ?"), p.append(domain)
    if since:
        where.append("event_time >= ?"), p.append(since)
    if bbox:
        where.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"), p.extend([bbox[0], bbox[2], bbox[1], bbox[3]])
    allowed_access = [k for k, v in ACCESS_LEVELS.items() if v <= level]
    where.append(f"access IN ({','.join('?' * len(allowed_access))})"), p.extend(allowed_access)
    if extra_where:
        where.append(extra_where), p.extend(params)
    sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY event_time DESC LIMIT ?"
    return db.query(sql, (*p, limit))


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- geo
INDONESIA_BBOX = (94.0, -11.5, 141.5, 6.5)


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bbox_center(b) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def in_bbox(lon, lat, b, pad: float = 0.0) -> bool:
    return b[0] - pad <= lon <= b[2] + pad and b[1] - pad <= lat <= b[3] + pad


def make_alert(severity: str, title: str, message: str, domain: str, aoi_id: int | None = None,
               analysis_id: int | None = None, dedup_key: str | None = None) -> int | None:
    """Alert lintas domain. `dedup_key` mencegah alert ganda untuk peristiwa yang sama."""
    if dedup_key:
        if db.get_setting(f"alert_dedup:{dedup_key}"):
            return None
        db.set_setting(f"alert_dedup:{dedup_key}", "1")
    aid = db.insert("alerts", {"analysis_id": analysis_id, "aoi_id": aoi_id, "domain": domain, "severity": severity, "title": title,
                               "message": message, "created_at": db.now_iso()})
    db.log_activity("alert", f"Alert {severity}: {title}", aoi_id=aoi_id, alert_id=aid, analysis_id=analysis_id)
    return aid
