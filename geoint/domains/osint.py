"""OS — OSINT fusion: berita → topik → geolokasi → fusi dengan data domain lain.

1. Berita dikumpulkan dari RSS media nasional (Antara, Tempo, CNN Indonesia; bisa diatur) dan
   Google News RSS per kata kunci (lisensi pribadi non-komersial; bisa dimatikan).
2. Topik ditentukan dengan aturan kata kunci (tambang ilegal, pembalakan, karhutla, banjir, ...).
   Berita tanpa topik pemantauan diabaikan.
3. Geolokasi: nama kabupaten/kota atau provinsi di judul/ringkasan dicocokkan dengan gazetteer
   OSM -> titik pusat wilayah (presisi ±40 km kab/kota, ±150 km provinsi). Ini perkiraan.
4. Fusi: dicari data pendukung di sekitar lokasi & waktu berita — deteksi satelit sejenis, area
   pantauan, gempa, hotspot, kapal, izin tambang. Berita yang terkonfirmasi data lain diberi skor
   korroborasi lebih tinggi. Berita BUKAN bukti; ini alat prioritisasi verifikasi.
"""

from __future__ import annotations

import hashlib
import json
import html
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .. import db
from ..config import settings
from . import gazetteer
from .common import BROWSER_UA, haversine_km, http_get, make_alert, query_events, record_feed, upsert_event

log = logging.getLogger(__name__)

# topik -> (domain terkait, label, pola kata kunci, kelas deteksi satelit terkait)
TOPICS = {
    "tambang_ilegal": ("mne", "Tambang ilegal / PETI", r"tambang (emas )?ilegal|penambangan (liar|ilegal|tanpa izin)|\bpeti\b|galian c ilegal|tambang liar", {"tambang", "jalan_akses"}),
    "tambang": ("mne", "Pertambangan", r"\btambang\b|pertambangan|nikel|batu ?bara|smelter|\biup\b", {"tambang", "jalan_akses"}),
    "pembalakan": ("sat", "Pembalakan / deforestasi", r"pembalakan liar|penebangan liar|illegal logging|kayu ilegal|perambahan hutan|deforestasi|alih fungsi hutan", {"penebangan", "jalan_akses"}),
    "karhutla": ("dis", "Kebakaran hutan & lahan", r"karhutla|kebakaran hutan|kebakaran lahan|titik panas|hotspot|kabut asap", {"kebakaran"}),
    "banjir": ("dis", "Banjir", r"banjir( bandang)?|luapan sungai|tanggul jebol", {"banjir"}),
    "longsor": ("dis", "Tanah longsor", r"longsor|tanah bergerak|pergerakan tanah", {"longsor"}),
    "gempa": ("dis", "Gempa / tsunami", r"\bgempa\b|tsunami|guncangan", set()),
    "maritim": ("sea", "Maritim & pelabuhan", r"kapal (asing|tenggelam|ikan)|pelabuhan|illegal fishing|pencurian ikan|perompak|tumpahan minyak|bakamla", set()),
    "infrastruktur": ("inf", "Infrastruktur", r"jalan tol|pembangunan jalan|jembatan (putus|ambruk|baru)|bendungan|bandara baru|kawasan industri|proyek strategis", {"pembangunan", "jalan_akses"}),
    "konflik_lahan": ("os", "Konflik lahan", r"konflik (lahan|agraria)|sengketa lahan|masyarakat adat|penggusuran|bentrok warga", set()),
    "pencemaran": ("sat", "Pencemaran", r"pencemaran|limbah|sungai tercemar|air keruh tambang", set()),
}
_TOPIC_RE = {k: re.compile(v[2], re.I) for k, v in TOPICS.items()}
GNEWS_QUERIES = ["tambang ilegal", "penambangan liar", "pembalakan liar", "kebakaran hutan", "karhutla", "banjir bandang",
                 "tanah longsor", "deforestasi", "kapal ikan asing", "konflik lahan", "pencemaran sungai tambang"]
GNEWS_URL = "https://news.google.com/rss/search?q={q}+when:7d&hl=id&gl=ID&ceid=ID:id"


def classify_topics(text: str) -> list[str]:
    topics = [k for k, rx in _TOPIC_RE.items() if rx.search(text)]
    if "tambang_ilegal" in topics and "tambang" in topics:
        topics.remove("tambang")
    return topics


def _clean(s: str | None) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _parse_rss(raw: bytes, feed_name: str) -> list[dict]:
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        desc = _clean(it.findtext("description"))
        src = it.find("source")
        pub = it.findtext("pubDate")
        try:
            t = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            t = datetime.now(timezone.utc)
        out.append({"title": title, "link": link, "desc": desc, "time": t.replace(microsecond=0).isoformat(),
                    "publisher": (src.text if src is not None else feed_name) or feed_name})
    return out


def ingest(items: list[dict], source: str) -> int:
    new = 0
    for it in items:
        text = f"{it['title']} . {it['desc']}"
        topics = classify_topics(text)
        if not topics:
            continue
        loc = gazetteer.geocode_text(text)
        props = {"link": it["link"], "publisher": it["publisher"], "summary": it["desc"][:400], "topics": topics,
                 "topic_labels": [TOPICS[t][1] for t in topics], "domains": sorted({TOPICS[t][0] for t in topics}),
                 "location": loc, "feed": source}
        ext = hashlib.sha1((it["link"] or it["title"]).encode()).hexdigest()
        lon, lat = (loc["lon"], loc["lat"]) if loc else (None, None)
        if upsert_event("os", "news", source, ext, it["time"], lon, lat, it["title"], len(topics), props):
            new += 1
    return new


def fetch_rss_media() -> int:
    new, errors = 0, []
    for url in [u.strip() for u in settings.rss_feeds.split(",") if u.strip()]:
        try:
            new += ingest(_parse_rss(http_get(url, timeout=40, ua=BROWSER_UA), urllib.parse.urlparse(url).netloc), "rss")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if errors and not new:
        raise RuntimeError("; ".join(errors)[:300])
    return new


def fetch_gnews() -> int:
    new = 0
    for q in GNEWS_QUERIES:
        try:
            new += ingest(_parse_rss(http_get(GNEWS_URL.format(q=urllib.parse.quote(q)), timeout=40, ua=BROWSER_UA), "Google News"), "gnews")
        except Exception as exc:
            log.warning("Google News '%s' gagal: %s", q, exc)
    return new


# --------------------------------------------------------------------------- fusi
def fuse(ev: dict) -> dict:
    """Cari data pendukung di sekitar berita: deteksi satelit sejenis, area pantauan, gempa, hotspot, kapal, izin."""
    props = ev["properties"] or {}
    loc = props.get("location")
    if not loc:
        return {"links": [], "score": 0, "note": "Berita tanpa lokasi yang dikenali — tidak bisa difusikan."}
    lon, lat = loc["lon"], loc["lat"]
    radius = loc.get("precision_km", 40) * 1.25
    t = datetime.fromisoformat(ev["event_time"])
    topics = props.get("topics", [])
    want_classes = set().union(*(TOPICS[k][3] for k in topics if k in TOPICS)) if topics else set()
    links = []

    for a in db.query("SELECT id, name, bbox FROM aois"):
        b = a["bbox"]
        d = haversine_km(lon, lat, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        if d <= radius:
            links.append({"type": "aoi", "id": a["id"], "label": f"Area pantauan {a['name']}", "dist_km": round(d, 1), "weight": 1})
    if want_classes:
        dlat = radius / 111
        rows = db.query(
            f"SELECT d.id, d.class, d.area_ha, d.lon, d.lat, d.analysis_id, d.compliance, a.after_end FROM detections d "
            f"JOIN analyses a ON a.id = d.analysis_id WHERE a.status='done' AND d.class IN ({','.join('?' * len(want_classes))}) "
            f"AND d.lat BETWEEN ? AND ? AND d.lon BETWEEN ? AND ? ORDER BY d.area_ha DESC LIMIT 400",
            (*want_classes, lat - dlat, lat + dlat, lon - dlat * 1.1, lon + dlat * 1.1))
        for r in rows:
            d = haversine_km(lon, lat, r["lon"], r["lat"])
            if d <= radius:
                comp = (r["compliance"] or {}).get("status_label")
                links.append({"type": "detection", "id": r["id"], "analysis_id": r["analysis_id"], "dist_km": round(d, 1),
                              "label": f"Deteksi {r['class']} {r['area_ha']:.1f} ha (periode s/d {r['after_end']})" + (f" — {comp}" if comp else ""),
                              "weight": 3 if comp in ("Di luar izin terdaftar", "Izin tidak aktif") else 2})
    win = (t - timedelta(days=4)).isoformat(), (t + timedelta(days=2)).isoformat()
    if "gempa" in topics or "longsor" in topics or "banjir" in topics:
        for q in query_events(kind="earthquake", extra_where="event_time BETWEEN ? AND ?", params=win, limit=200):
            d = haversine_km(lon, lat, q["lon"], q["lat"])
            if d <= max(radius, 250):
                links.append({"type": "earthquake", "id": q["id"], "label": q["title"], "dist_km": round(d, 1), "weight": 2})
    if "karhutla" in topics:
        hs = [h for h in query_events(kind="hotspot", extra_where="event_time BETWEEN ? AND ?", params=win, limit=20000)
              if haversine_km(lon, lat, h["lon"], h["lat"]) <= radius]
        if hs:
            links.append({"type": "hotspot", "id": hs[0]["id"], "label": f"{len(hs)} hotspot VIIRS dalam ±{radius:.0f} km",
                          "dist_km": round(min(haversine_km(lon, lat, h["lon"], h["lat"]) for h in hs), 1), "weight": 2})
    if "maritim" in topics:
        for p in db.query("SELECT id, name, lon, lat FROM ports"):
            d = haversine_km(lon, lat, p["lon"], p["lat"])
            if d <= radius:
                links.append({"type": "port", "id": p["id"], "label": f"Pelabuhan {p['name']}", "dist_km": round(d, 1), "weight": 1})
    if "tambang" in topics or "tambang_ilegal" in topics:
        from .mining import features_containing

        for lf in features_containing(lon, lat, kinds={"izin_tambang"})[:5]:
            links.append({"type": "permit", "id": lf["feature_id"], "label": f"Izin: {lf['label']} ({lf['state']})", "dist_km": 0, "weight": 1})
    links.sort(key=lambda l: (-l["weight"], l["dist_km"]))
    score = min(10, sum(l["weight"] for l in links))
    return {"links": links[:40], "score": score, "radius_km": round(radius),
            "note": "Lokasi berita = pusat wilayah yang disebut (perkiraan). Gunakan sebagai petunjuk, bukan bukti."}


def update_fusion_and_alert(since_iso: str) -> int:
    n = 0
    for ev in query_events(kind="news", since=since_iso, limit=500):
        fx = fuse(ev)
        props = ev["properties"] or {}
        props["fusion_score"] = fx["score"]
        props["fusion_links"] = len(fx["links"])
        db.execute("UPDATE events SET properties = ? WHERE id = ?", (json.dumps(props, ensure_ascii=False), ev["id"]))
        aoi_links = [l for l in fx["links"] if l["type"] == "aoi"]
        if aoi_links and set(props.get("topics", [])) & {"tambang_ilegal", "pembalakan", "karhutla", "longsor", "banjir"}:
            if make_alert("sedang", f"Berita {', '.join(props.get('topic_labels', [])[:2])} dekat {aoi_links[0]['label']}",
                          f"{ev['title']} — {props.get('publisher', '')}. Skor korroborasi {fx['score']}/10.",
                          "os", aoi_id=aoi_links[0]["id"], dedup_key=f"news:{ev['id']}"):
                n += 1
    return n


def run_feed(source_id: str) -> int:
    try:
        gazetteer.ensure()
        if source_id == "rss_media":
            n = fetch_rss_media()
        elif source_id == "gnews":
            n = fetch_gnews()
        else:
            raise ValueError(source_id)
        update_fusion_and_alert((datetime.now(timezone.utc) - timedelta(hours=6)).isoformat())
        record_feed(source_id, True, n, f"{n} berita relevan baru")
        return n
    except Exception as exc:
        log.warning("Feed %s gagal: %s", source_id, exc)
        record_feed(source_id, False, 0, str(exc))
        return 0
