"""SEA — analitik pelabuhan dari radar Sentinel-1 + AIS berlisensi.

Deteksi kapal radar (terbuka, tanpa lisensi):
  1. Untuk tiap pelabuhan, ambil akuisisi Sentinel-1 RTC (VV) terbaru pada grid 10 m.
  2. Masker laut dari JRC Global Surface Water (frekuensi air ≥ 50%), dikikis 2 piksel agar
     dermaga & garis pantai tidak terhitung.
  3. Target = piksel jauh lebih terang dari latar laut (median + 6·MAD, min −12 dB),
     dikelompokkan menjadi objek 3–3000 piksel.
  4. Filter persistensi: objek yang muncul di lokasi sama pada ≥ 60% tanggal adalah bangunan
     tetap (bagan, platform, rambu, dermaga) dan tidak dihitung sebagai kapal.
AIS (berlisensi): posisi kapal dari aisstream.io (API key) atau impor CSV penyedia AIS.
Dark vessel: deteksi radar tanpa posisi AIS dalam ±30 menit & 1 km — hanya dihitung bila ada
data AIS pada waktu akuisisi.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy import ndimage

from .. import db
from ..config import settings
from .common import haversine_km, record_feed, upsert_event

log = logging.getLogger(__name__)

# Pelabuhan utama awal (koordinat PERKIRAAN; dapat diedit/ditambah dari dashboard).
SEED_PORTS = [
    ("Tanjung Priok, Jakarta", 106.885, -6.095, "DKI Jakarta"), ("Tanjung Perak, Surabaya", 112.730, -7.200, "Jawa Timur"),
    ("Belawan, Medan", 98.695, 3.790, "Sumatera Utara"), ("Kuala Tanjung, Batu Bara", 99.460, 3.360, "Sumatera Utara"),
    ("Tanjung Emas, Semarang", 110.420, -6.945, "Jawa Tengah"), ("Patimban, Subang", 107.910, -6.250, "Jawa Barat"),
    ("Makassar (Soekarno-Hatta)", 119.405, -5.115, "Sulawesi Selatan"), ("Balikpapan", 116.810, -1.270, "Kalimantan Timur"),
    ("Samarinda (Palaran)", 117.170, -0.560, "Kalimantan Timur"), ("Bitung", 125.190, 1.440, "Sulawesi Utara"),
    ("Pontianak (Dwikora)", 109.340, -0.020, "Kalimantan Barat"), ("Banjarmasin (Trisakti)", 114.560, -3.330, "Kalimantan Selatan"),
    ("Palembang (Boom Baru)", 104.780, -2.980, "Sumatera Selatan"), ("Batam (Batu Ampar)", 104.000, 1.170, "Kepulauan Riau"),
    ("Dumai", 101.450, 1.690, "Riau"), ("Panjang, Lampung", 105.320, -5.470, "Lampung"),
    ("Teluk Bayur, Padang", 100.370, -1.000, "Sumatera Barat"), ("Cilacap", 109.000, -7.740, "Jawa Tengah"),
    ("Benoa, Bali", 115.210, -8.745, "Bali"), ("Tenau, Kupang", 123.530, -10.200, "Nusa Tenggara Timur"),
    ("Kendari", 122.600, -3.970, "Sulawesi Tenggara"), ("Ambon (Yos Sudarso)", 128.170, -3.690, "Maluku"),
    ("Ternate (Ahmad Yani)", 127.390, 0.780, "Maluku Utara"), ("Sorong", 131.250, -0.880, "Papua Barat Daya"),
    ("Jayapura", 140.710, -2.540, "Papua"), ("Merauke", 140.390, -8.470, "Papua Selatan"),
    ("Morowali (IMIP)", 122.165, -2.825, "Sulawesi Tengah"), ("Weda Bay (IWIP)", 127.940, 0.480, "Maluku Utara"),
]
PORT_HALF_KM = 6.0  # bbox pelabuhan ±6 km dari titik pusat (mencakup kolam pelabuhan & area labuh)


def port_bbox(lon: float, lat: float, half_km: float = PORT_HALF_KM) -> list[float]:
    dlat = half_km / 110.574
    dlon = half_km / (111.320 * math.cos(math.radians(lat)))
    return [round(lon - dlon, 5), round(lat - dlat, 5), round(lon + dlon, 5), round(lat + dlat, 5)]


def seed_ports() -> int:
    if db.query("SELECT COUNT(*) AS n FROM ports")[0]["n"]:
        return 0
    now = db.now_iso()
    for name, lon, lat, prov in SEED_PORTS:
        db.insert("ports", {"name": name, "lon": lon, "lat": lat, "bbox": port_bbox(lon, lat), "province": prov,
                            "approx": 1, "active": 1, "created_at": now})
    return len(SEED_PORTS)


# --------------------------------------------------------------------------- Sentinel-1
def _utm(lon, lat):
    return (32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1


def scan_port(port: dict, lookback_days: int = 45, max_dates: int = 8) -> dict:
    """Deteksi kapal radar pada akuisisi Sentinel-1 terbaru di sebuah pelabuhan; simpan observasi & titik kapal."""
    import odc.stac
    from odc.geo.geobox import GeoBox
    from odc.geo.geom import BoundingBox
    from rasterio.warp import transform as warp_transform

    from ..ancillary import _catalog

    bbox = tuple(port["bbox"])
    catalog = _catalog()
    epsg = f"EPSG:{_utm(port['lon'], port['lat'])}"
    gbox = GeoBox.from_bbox(BoundingBox(*bbox, crs="EPSG:4326").to_crs(epsg), crs=epsg, resolution=10)
    end = datetime.now(timezone.utc)
    items = list(catalog.search(collections=["sentinel-1-rtc"], bbox=bbox,
                                datetime=f"{(end - timedelta(days=lookback_days)).date()}/{end.date()}").items())
    if not items:
        raise RuntimeError("tidak ada akuisisi Sentinel-1 dalam periode")
    by_day: dict[str, list] = {}
    for it in items:
        by_day.setdefault(it.datetime.strftime("%Y-%m-%dT%H"), []).append(it)
    keys = sorted(by_day)[-max_dates:]

    gsw = list(catalog.search(collections=["jrc-gsw"], bbox=bbox).items())
    occ = odc.stac.load(gsw, bands=["occurrence"], geobox=gbox, resampling="nearest", chunks=None)["occurrence"].values
    occ = occ.max(axis=0) if occ.ndim == 3 else occ
    sea = ndimage.binary_erosion((occ >= 50) & (occ <= 100), iterations=2)
    if sea.mean() < 0.02:
        raise RuntimeError("area laut terlalu kecil pada bbox pelabuhan (cek koordinat)")

    per_date = []
    for k in keys:
        its = by_day[k]
        ds = odc.stac.load(its, bands=["vv"], geobox=gbox, chunks=None, groupby="solar_day")
        vv = ds["vv"].values[0].astype(np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            db_ = 10 * np.log10(np.where(vv > 0, vv, np.nan))
        vals = db_[sea & np.isfinite(db_)]
        if vals.size < 1000:
            continue
        med = float(np.median(vals))
        mad = float(np.median(np.abs(vals - med)) * 1.4826)
        thr = max(med + 6 * mad, -12.0)
        det = sea & (np.nan_to_num(db_, nan=-99) > thr)
        lab, n = ndimage.label(det, structure=np.ones((3, 3)))
        objs = []
        if n:
            sizes = ndimage.sum_labels(det, lab, np.arange(1, n + 1))
            coms = ndimage.center_of_mass(det, lab, np.arange(1, n + 1))
            peaks = ndimage.maximum(np.nan_to_num(db_, nan=-99), lab, np.arange(1, n + 1))
            slices = ndimage.find_objects(lab)
            for i, (sz, (cy, cx), pk, sl) in enumerate(zip(sizes, coms, peaks, slices), start=1):
                if 3 <= sz <= 3000 and sl is not None:
                    extent = max(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) * 10
                    objs.append({"label": i, "px": int(sz), "row": float(cy), "col": float(cx), "length_m": int(extent),
                                 "peak_db": round(float(pk), 1)})
        per_date.append({"key": k, "time": min(it.datetime for it in its), "det": det, "lab": lab, "objs": objs,
                         "thr": thr, "sea_med": med, "orbit": its[0].properties.get("sat:orbit_state")})
    if not per_date:
        raise RuntimeError("tidak ada akuisisi dengan cakupan laut yang cukup")

    # Persistensi: objek di lokasi yang terdeteksi pada >= 60% tanggal = bangunan tetap.
    stack = np.stack([ndimage.binary_dilation(p["det"], iterations=2) for p in per_date]).astype(np.float32)
    persist = stack.mean(axis=0)
    static_mask = persist >= 0.6 if len(per_date) >= 3 else np.zeros_like(persist, bool)

    results = []
    for p in per_date:
        vessels, static = [], 0
        for o in p["objs"]:
            if static_mask[int(round(o["row"])), int(round(o["col"]))]:
                static += 1
            else:
                vessels.append(o)
        xs = [gbox.affine.c + (o["col"] + 0.5) * gbox.affine.a for o in vessels]
        ys = [gbox.affine.f + (o["row"] + 0.5) * gbox.affine.e for o in vessels]
        lons, lats = warp_transform(epsg, "EPSG:4326", xs, ys) if vessels else ([], [])
        t_iso = p["time"].replace(microsecond=0).isoformat()
        dark = _dark_count(lons, lats, p["time"]) if vessels else (0 if _ais_coverage(bbox, p["time"]) else None)
        for o, lo, la in zip(vessels, lons, lats):
            upsert_event("sea", "ship", "s1", f"{port['id']}|{t_iso}|{lo:.5f}|{la:.5f}", t_iso, lo, la,
                         f"Kapal radar ~{o['length_m']} m — {port['name']}", o["length_m"],
                         {"port_id": port["id"], "port": port["name"], "peak_db": o["peak_db"], "px": o["px"],
                          "orbit": p["orbit"]})
        props = {"orbit": p["orbit"], "threshold_db": round(p["thr"], 1), "sea_median_db": round(p["sea_med"], 1),
                 "length_median_m": int(np.median([o["length_m"] for o in vessels])) if vessels else 0,
                 "large_vessels": sum(1 for o in vessels if o["length_m"] >= 100)}
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO port_obs (port_id, source, obs_time, vessels, static_targets, dark_vessels, properties)"
                      " VALUES (?,?,?,?,?,?,?)", (port["id"], "s1", t_iso, len(vessels), static, dark, json.dumps(props)))
        results.append({"time": t_iso, "vessels": len(vessels), "static": static, "dark": dark, **props})
    db.update("ports", port["id"], {"last_scan_at": db.now_iso()})
    return {"port": port["name"], "dates": len(results), "observations": results}


# --------------------------------------------------------------------------- AIS
def _ais_coverage(bbox, t: datetime, minutes: int = 30) -> bool:
    lo, hi = (t - timedelta(minutes=minutes)).isoformat(), (t + timedelta(minutes=minutes)).isoformat()
    return bool(db.query("SELECT 1 FROM ais_positions WHERE ts BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? LIMIT 1",
                         (lo, hi, bbox[0], bbox[2], bbox[1], bbox[3])))


def _dark_count(lons, lats, t: datetime, minutes: int = 30, max_km: float = 1.0) -> int | None:
    """Jumlah deteksi radar tanpa pasangan AIS. None bila tidak ada data AIS pada waktu itu."""
    if not lons:
        return 0
    b = (min(lons) - 0.05, min(lats) - 0.05, max(lons) + 0.05, max(lats) + 0.05)
    lo, hi = (t - timedelta(minutes=minutes)).isoformat(), (t + timedelta(minutes=minutes)).isoformat()
    ais = db.query("SELECT lon, lat FROM ais_positions WHERE ts BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
                   (lo, hi, b[0], b[2], b[1], b[3]))
    if not ais:
        return None
    return sum(1 for x, y in zip(lons, lats) if not any(haversine_km(x, y, a["lon"], a["lat"]) <= max_km for a in ais))


def _store_ais(rows: list[tuple]) -> int:
    with db.connect() as c:
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO ais_positions (mmsi, ts, lon, lat, sog, cog, ship_type, name, source)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", rows)
        return c.total_changes - before


def import_ais_csv(content: bytes, source: str = "impor") -> int:
    """Impor AIS dari penyedia berlisensi. Kolom dikenali: mmsi, timestamp/ts/time, lat/latitude, lon/longitude, sog, cog, ship_type, name."""
    text = content.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}

    def col(*names):
        return next((cols[n] for n in names if n in cols), None)

    c_mmsi, c_ts = col("mmsi"), col("timestamp", "ts", "time", "datetime", "basedatetime", "time_utc")
    c_lat, c_lon = col("lat", "latitude"), col("lon", "lng", "longitude")
    if not all((c_mmsi, c_ts, c_lat, c_lon)):
        raise ValueError("CSV AIS harus punya kolom mmsi, timestamp, lat, lon")
    c_sog, c_cog, c_type, c_name = col("sog", "speed"), col("cog", "course"), col("ship_type", "vesseltype", "type"), col("name", "vesselname", "shipname")
    rows = []
    for r in reader:
        try:
            ts = r[c_ts].strip().replace(" ", "T")
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            rows.append((r[c_mmsi].strip(), t.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
                         float(r[c_lon]), float(r[c_lat]),
                         float(r[c_sog]) if c_sog and r.get(c_sog) else None, float(r[c_cog]) if c_cog and r.get(c_cog) else None,
                         r.get(c_type) if c_type else None, r.get(c_name) if c_name else None, source))
        except (ValueError, KeyError):
            continue
    n = _store_ais(rows)
    db.log_activity("ais_diimpor", f"Impor AIS ({source}): {n} posisi baru dari {len(rows)} baris")
    return n


async def _aisstream_collect(boxes: list[list[list[float]]], seconds: int) -> list[tuple]:
    import websockets

    rows, names = [], {}
    sub = {"APIKey": settings.aisstream_key, "BoundingBoxes": boxes, "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
    async with websockets.connect("wss://stream.aisstream.io/v0/stream", open_timeout=30) as ws:
        await ws.send(json.dumps(sub))
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, end - loop.time()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if "error" in msg:
                raise RuntimeError(f"aisstream: {msg['error']}")
            meta = msg.get("MetaData", {})
            mmsi = str(meta.get("MMSI", ""))
            if msg.get("MessageType") == "ShipStaticData":
                sd = msg["Message"]["ShipStaticData"]
                names[mmsi] = (sd.get("Name", "").strip(), str(sd.get("Type", "")))
                continue
            if msg.get("MessageType") == "PositionReport":
                pr = msg["Message"]["PositionReport"]
                t = meta.get("time_utc", "")[:19].replace(" ", "T") + "+00:00"
                nm, tp = names.get(mmsi, (meta.get("ShipName", "").strip(), None))
                rows.append((mmsi, t, float(meta["longitude"]), float(meta["latitude"]), pr.get("Sog"), pr.get("Cog"), tp, nm, "aisstream"))
    return rows


def collect_aisstream(seconds: int = 90) -> int:
    """Ambil posisi AIS realtime di bbox semua pelabuhan aktif selama `seconds` detik (perlu AISSTREAM_API_KEY)."""
    if not settings.aisstream_key:
        raise RuntimeError("AISSTREAM_API_KEY belum diisi")
    ports = db.query("SELECT bbox FROM ports WHERE active = 1")
    boxes = [[[p["bbox"][1], p["bbox"][0]], [p["bbox"][3], p["bbox"][2]]] for p in ports]  # [[lat,lon],[lat,lon]]
    rows = asyncio.run(_aisstream_collect(boxes, seconds))
    return _store_ais(rows)


def ais_daily(port: dict, days: int = 30) -> list[dict]:
    b = port["bbox"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return db.query(
        "SELECT substr(ts,1,10) AS day, COUNT(DISTINCT mmsi) AS vessels, COUNT(*) AS positions FROM ais_positions "
        "WHERE ts >= ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? GROUP BY day ORDER BY day",
        (since, b[0], b[2], b[1], b[3]))


# --------------------------------------------------------------------------- ringkasan
def port_summary(port: dict) -> dict:
    obs = db.query("SELECT obs_time, vessels, static_targets, dark_vessels, properties FROM port_obs "
                   "WHERE port_id = ? AND source = 's1' ORDER BY obs_time", (port["id"],))
    vessels = [o["vessels"] for o in obs]
    trend = None
    if len(vessels) >= 4:
        recent, base = np.mean(vessels[-2:]), np.mean(vessels[:-2])
        trend = round(100 * (recent - base) / base, 1) if base else None
    return {**port, "observations": obs, "latest": obs[-1] if obs else None, "mean_vessels": round(float(np.mean(vessels)), 1) if vessels else None,
            "trend_pct": trend, "ais_daily": ais_daily(port)}


def run_feed(source_id: str) -> int:
    try:
        if source_id == "s1_ports":
            total = 0
            # Satu putaran memindai pelabuhan yang paling lama tidak dipindai (maks 4 per putaran).
            ports = db.query("SELECT * FROM ports WHERE active = 1 ORDER BY COALESCE(last_scan_at, '') LIMIT 4")
            errors = []
            for p in ports:
                try:
                    res = scan_port(p)
                    total += sum(o["vessels"] for o in res["observations"][-1:])
                except Exception as exc:
                    errors.append(f"{p['name']}: {exc}")
                    db.update("ports", p["id"], {"last_scan_at": db.now_iso()})
            record_feed(source_id, not errors or total > 0, total,
                        f"{len(ports)} pelabuhan dipindai" + (f"; galat: {'; '.join(errors)[:200]}" if errors else ""))
            return total
        if source_id == "aisstream":
            n = collect_aisstream()
            record_feed(source_id, True, n, f"{n} posisi AIS baru")
            return n
        raise ValueError(source_id)
    except Exception as exc:
        log.warning("Feed %s gagal: %s", source_id, exc)
        record_feed(source_id, False, 0, str(exc))
        return 0


warnings.filterwarnings("ignore", message="All-NaN slice encountered")
