"""DIS — gempa (BMKG, USGS), hotspot (NASA FIRMS), dan paparan risiko.

Paparan risiko gempa memakai estimasi kasar intensitas guncangan (MMI) dari magnitudo & jarak
hiposenter:  I ≈ 1,5·M − 3,5·log10(R) + 3   (R dalam km)
Ini relasi atenuasi sederhana untuk penapisan cepat, BUKAN pengganti peta guncangan resmi
(BMKG ShakeMap / USGS ShakeMap). Hasilnya dipakai untuk mendaftar wilayah, area pantauan,
pelabuhan, dan deteksi perubahan yang mungkin terdampak.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np
from shapely.geometry import Point, shape

from .. import db
from . import gazetteer
from .common import (INDONESIA_BBOX, haversine_km, http_get, http_json, iso_days_ago, make_alert, query_events,
                     record_feed, upsert_event, upsert_events_bulk)

log = logging.getLogger(__name__)

BMKG_FEEDS = {
    "terkini": "https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json",      # 15 gempa M5+ terakhir
    "dirasakan": "https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json",  # 15 gempa dirasakan terakhir
}
USGS_URL = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime={start}"
            "&minlatitude={s}&maxlatitude={n}&minlongitude={w}&maxlongitude={e}&minmagnitude={m}&orderby=time&limit=2000")
FIRMS_PUBLIC = {
    "VIIRS_SNPP": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_SouthEast_Asia_24h.csv",
    "VIIRS_NOAA20": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_SouthEast_Asia_24h.csv",
}


# --------------------------------------------------------------------------- gempa
def _depth_km(txt) -> float | None:
    try:
        return float(str(txt).split()[0])
    except (ValueError, IndexError):
        return None


def fetch_bmkg() -> int:
    new = 0
    for feed, url in BMKG_FEEDS.items():
        data = http_json(url, timeout=30)
        for g in data["Infogempa"]["gempa"]:
            lat, lon = (float(v) for v in g["Coordinates"].split(","))
            mag = float(g["Magnitude"])
            props = {"wilayah": g.get("Wilayah"), "kedalaman_km": _depth_km(g.get("Kedalaman")),
                     "potensi": g.get("Potensi"), "dirasakan": g.get("Dirasakan"), "feed": feed}
            ext = f"{g['DateTime']}|{g['Coordinates']}"
            if upsert_event("dis", "earthquake", "bmkg", ext, g["DateTime"], lon, lat,
                            f"M{mag:.1f} {g.get('Wilayah', '')}", mag, props):
                new += 1
    return new


def fetch_usgs(days: int = 7, min_mag: float = 4.0) -> int:
    w, s, e, n = INDONESIA_BBOX
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    data = http_json(USGS_URL.format(start=start, s=s, n=n, w=w, e=e, m=min_mag), timeout=60)
    new = 0
    for f in data["features"]:
        p = f["properties"]
        lon, lat, depth = f["geometry"]["coordinates"]
        t = datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
        dup = _match_bmkg(lon, lat, t)
        props = {"wilayah": p.get("place"), "kedalaman_km": depth, "tsunami": p.get("tsunami"), "url": p.get("url"),
                 "duplicate_of": dup}
        if upsert_event("dis", "earthquake", "usgs", f["id"], t, lon, lat, f"M{p['mag']:.1f} {p.get('place', '')}",
                        p["mag"], props):
            new += 1
    return new


def _match_bmkg(lon, lat, t_iso) -> int | None:
    """Gempa USGS yang sama dengan catatan BMKG (±2 menit, ≤100 km) ditandai duplikat agar tidak dobel di peta."""
    t = datetime.fromisoformat(t_iso)
    lo, hi = (t - timedelta(minutes=2)).isoformat(), (t + timedelta(minutes=2)).isoformat()
    for ev in db.query("SELECT id, lon, lat FROM events WHERE source='bmkg' AND event_time BETWEEN ? AND ?", (lo, hi)):
        if haversine_km(lon, lat, ev["lon"], ev["lat"]) <= 100:
            return ev["id"]
    return None


def mmi_at(mag: float, dist_km: float, depth_km: float = 10.0) -> float:
    r = max(math.hypot(dist_km, depth_km or 10.0), 1.0)
    return 1.5 * mag - 3.5 * math.log10(r) + 3.0


def radius_for_mmi(mag: float, mmi: float, depth_km: float = 10.0) -> float:
    """Jarak episenter (km) di mana estimasi intensitas turun ke `mmi`."""
    r = 10 ** ((1.5 * mag + 3.0 - mmi) / 3.5)
    d = depth_km or 10.0
    return round(math.sqrt(r * r - d * d), 1) if r > d else 0.0


def earthquake_exposure(ev: dict) -> dict:
    """Paparan: wilayah, area pantauan, pelabuhan & deteksi perubahan dalam zona intensitas V dan VI+."""
    mag, lon, lat = ev["value"], ev["lon"], ev["lat"]
    depth = (ev.get("properties") or {}).get("kedalaman_km") or 10.0
    r4, r5, r6, r7 = (radius_for_mmi(mag, m, depth) for m in (4, 5, 6, 7))
    zones = {"VII+": r7, "VI": r6, "V": r5, "IV": r4}

    def zone(d):
        for name, r in zones.items():
            if d <= r:
                return name
        return None

    places = [{**p, "zona": zone(p["dist_km"]), "mmi": round(mmi_at(mag, p["dist_km"], depth), 1)}
              for p in gazetteer.within(lon, lat, r4)]
    aois = []
    for a in db.query("SELECT id, name, bbox FROM aois"):
        b = a["bbox"]
        d = haversine_km(lon, lat, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        if d <= r4:
            aois.append({"id": a["id"], "name": a["name"], "dist_km": round(d, 1), "zona": zone(d),
                         "mmi": round(mmi_at(mag, d, depth), 1)})
    ports = []
    for p in db.query("SELECT id, name, lon, lat FROM ports"):
        d = haversine_km(lon, lat, p["lon"], p["lat"])
        if d <= r4:
            ports.append({"id": p["id"], "name": p["name"], "dist_km": round(d, 1), "zona": zone(d)})
    # Deteksi longsor/perubahan dari analisa selesai dalam zona VI+ (kandidat verifikasi pasca-gempa).
    dets = []
    if r6 > 0:
        for d in db.query(
            "SELECT d.id, d.class, d.area_ha, d.lon, d.lat, d.analysis_id FROM detections d JOIN analyses a ON a.id=d.analysis_id "
            "WHERE a.status='done' AND d.lon BETWEEN ? AND ? AND d.lat BETWEEN ? AND ?",
            (lon - r6 / 100, lon + r6 / 100, lat - r6 / 100, lat + r6 / 100),
        ):
            dist = haversine_km(lon, lat, d["lon"], d["lat"])
            if dist <= r6:
                dets.append({**d, "dist_km": round(dist, 1)})
    return {
        "event_id": ev["id"], "magnitude": mag, "depth_km": depth,
        "radius_km": {"IV": r4, "V": r5, "VI": r6, "VII": r7},
        "mmi_epicenter": round(mmi_at(mag, 0, depth), 1),
        "places": places[:60], "n_places": len(places), "aois": aois, "ports": ports,
        "detections": sorted(dets, key=lambda x: x["dist_km"])[:50],
        "method": "Estimasi kasar I≈1,5M−3,5·log10(R)+3; gunakan ShakeMap resmi BMKG/USGS untuk keputusan.",
    }


def alert_earthquakes(since_iso: str) -> int:
    n = 0
    for ev in query_events(kind="earthquake", since=since_iso, extra_where="value >= 5.0"):
        if (ev["properties"] or {}).get("duplicate_of"):
            continue
        ex = earthquake_exposure(ev)
        hit = [a for a in ex["aois"] if a["zona"] in ("VI", "VII+")]
        if hit:
            names = ", ".join(a["name"] for a in hit[:5])
            if make_alert("tinggi" if ev["value"] >= 6 else "sedang", f"Gempa {ev['title']} — {len(hit)} area pantauan dalam zona guncangan",
                          f"Estimasi MMI ≥ VI: {names}. Periksa longsor/kerusakan; jalankan analisa ulang area terdampak.",
                          "dis", aoi_id=hit[0]["id"], dedup_key=f"eq:{ev['id']}"):
                n += 1
    return n


# --------------------------------------------------------------------------- hotspot
def fetch_firms() -> int:
    """Hotspot 24 jam terakhir (file publik FIRMS, tanpa API key), disaring ke wilayah Indonesia."""
    new = 0
    w, s, e, n = INDONESIA_BBOX
    errors = []
    rows = []
    for sat, url in FIRMS_PUBLIC.items():
        try:
            text = http_get(url, timeout=120).decode("utf-8", "replace")
        except Exception as exc:
            errors.append(f"{sat}: {exc}")
            continue
        for row in csv.DictReader(io.StringIO(text)):
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            except (KeyError, ValueError):
                continue
            if not (w <= lon <= e and s <= lat <= n):
                continue
            conf = (row.get("confidence") or "").lower()
            if conf in ("l", "low"):  # keyakinan rendah VIIRS: sering pantulan/derau, dibuang
                continue
            hhmm = row["acq_time"].zfill(4)
            t = f"{row['acq_date']}T{hhmm[:2]}:{hhmm[2:]}:00+00:00"
            frp = float(row.get("frp") or 0)
            props = {"confidence": conf, "satellite": sat, "daynight": row.get("daynight"), "bright_ti4": row.get("bright_ti4")}
            rows.append(("dis", "hotspot", "firms", f"{sat}|{row['acq_date']}|{row['acq_time']}|{lat:.4f}|{lon:.4f}",
                         t, lon, lat, f"Hotspot {sat} FRP {frp:.1f} MW", frp, props, "publik"))
    land = gazetteer.indonesia_shape()
    if land is not None and rows:
        import shapely

        keep = shapely.contains_xy(land, np.array([r[5] for r in rows]), np.array([r[6] for r in rows]))
        rows = [r for r, k in zip(rows, keep) if k]  # buang titik di Malaysia/Singapura/Timor Leste/PNG
    new = upsert_events_bulk(rows)
    if errors and not rows:
        raise RuntimeError("; ".join(errors))
    return new


def _point_in_aoi(lon, lat, aois):
    for a in aois:
        b = a["bbox"]
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and a["_shape"].contains(Point(lon, lat)):
            yield a


def hotspot_exposure(days: float = 1.0) -> dict:
    """Ringkasan hotspot: per provinsi (perkiraan wilayah terdekat), di dalam area pantauan & lapisan izin."""
    hs = query_events(kind="hotspot", since=iso_days_ago(days), limit=20000)
    aois = [{**a, "_shape": shape(a["geometry"])} for a in db.query("SELECT id, name, bbox, geometry FROM aois")]
    in_aoi: dict[int, dict] = {}
    by_place: dict[str, int] = {}
    places = [p for p in gazetteer.ensure(download=False) if p["level"] == "kabkota"]
    if places and hs:
        # Kab/kota dengan titik pusat terdekat (514 titik: jauh lebih tepat daripada pusat provinsi).
        P = np.array([[p["lon"], p["lat"]] for p in places])
        H = np.array([[h["lon"], h["lat"]] for h in hs])
        for start in range(0, len(H), 4000):
            chunk = H[start:start + 4000]
            k = np.cos(np.radians(chunk[:, 1:2]))
            d2 = ((chunk[:, None, 0] - P[None, :, 0]) * k) ** 2 + (chunk[:, None, 1] - P[None, :, 1]) ** 2
            for idx in d2.argmin(axis=1):
                name = places[idx]["name"]
                by_place[name] = by_place.get(name, 0) + 1
    for h in hs:
        for a in _point_in_aoi(h["lon"], h["lat"], aois):
            in_aoi.setdefault(a["id"], {"id": a["id"], "name": a["name"], "count": 0, "max_frp": 0})
            in_aoi[a["id"]]["count"] += 1
            in_aoi[a["id"]]["max_frp"] = max(in_aoi[a["id"]]["max_frp"], h["value"] or 0)
    from .mining import features_containing  # impor lambat: hindari siklus

    in_layers: dict[str, int] = {}
    for h in hs:
        for lf in features_containing(h["lon"], h["lat"]):
            key = f"{lf['layer_name']} — {lf['label']}"
            in_layers[key] = in_layers.get(key, 0) + 1
    # Titik panas berulang di sel ~1 km yang sama pada >= 3 hari berbeda (10 hari terakhir): kemungkinan sumber
    # panas industri (flare gas, smelter), bukan kebakaran lahan.
    cells: dict[tuple, set] = {}
    for h in query_events(kind="hotspot", since=iso_days_ago(10), limit=200000):
        cells.setdefault((round(h["lon"], 2), round(h["lat"], 2)), set()).add(h["event_time"][:10])
    static = {c for c, d in cells.items() if len(d) >= 3}
    n_static = sum(1 for h in hs if (round(h["lon"], 2), round(h["lat"], 2)) in static)
    return {
        "days": days, "total": len(hs), "persistent": n_static,
        "persistent_note": "Titik berulang ≥3 hari di lokasi sama dalam 10 hari — kemungkinan flare/industri.",
        "by_place": sorted(({"name": k, "count": v} for k, v in by_place.items()), key=lambda x: -x["count"])[:20],
        "in_aois": sorted(in_aoi.values(), key=lambda x: -x["count"]),
        "in_layers": sorted(({"name": k, "count": v} for k, v in in_layers.items()), key=lambda x: -x["count"])[:20],
        "note": "Wilayah = kab/kota dengan titik pusat terdekat (perkiraan, bukan batas administratif).",
    }


def alert_hotspots() -> int:
    ex = hotspot_exposure(1.0)
    n = 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for a in ex["in_aois"]:
        if make_alert("tinggi" if a["count"] >= 5 else "sedang", f"{a['count']} hotspot di area pantauan {a['name']}",
                      f"Hotspot VIIRS 24 jam terakhir, FRP maks {a['max_frp']:.1f} MW. Verifikasi kebakaran & jalankan analisa.",
                      "dis", aoi_id=a["id"], dedup_key=f"hs:{a['id']}:{day}"):
            n += 1
    return n


# --------------------------------------------------------------------------- feed runner
def run_feed(source_id: str) -> int:
    try:
        if source_id == "bmkg":
            n = fetch_bmkg()
            alert_earthquakes(iso_days_ago(2))
        elif source_id == "usgs":
            n = fetch_usgs()
            alert_earthquakes(iso_days_ago(2))
        elif source_id == "firms":
            n = fetch_firms()
            alert_hotspots()
        else:
            raise ValueError(source_id)
        record_feed(source_id, True, n, f"{n} peristiwa baru")
        return n
    except Exception as exc:
        log.warning("Feed %s gagal: %s", source_id, exc)
        record_feed(source_id, False, 0, str(exc))
        return 0
