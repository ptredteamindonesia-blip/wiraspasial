"""REST API enam domain nasional + peta nasional + registri sumber & hak akses."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db
from .domains import common, submit_feed
from .domains.common import ACCESS_LEVELS, DOMAINS, caller_level, iso_days_ago, query_events

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- akses
def access_level(x_wira_token: str | None = Header(None), x_geoint_token: str | None = Header(None),
                 token: str | None = Query(None)) -> int:
    # X-Wira-Token (nama baru); X-GeoInt-Token tetap diterima untuk klien lama.
    return caller_level(x_wira_token or x_geoint_token or token)


def require(level_name: str):
    def dep(level: int = Depends(access_level)) -> int:
        if level < ACCESS_LEVELS[level_name]:
            raise HTTPException(403, f"Butuh akses '{level_name}': isi token akses di Pengaturan")
        return level
    return dep


LEVEL_NAMES = {v: k for k, v in ACCESS_LEVELS.items()}


@router.get("/access")
def access_info(level: int = Depends(access_level)):
    from .config import settings

    return {"token_required": bool(settings.access_token), "level": LEVEL_NAMES[level],
            "levels": list(ACCESS_LEVELS), "ais_key": bool(settings.aisstream_key), "firms_key": bool(settings.firms_map_key)}


# --------------------------------------------------------------------------- domain & sumber
@router.get("/domains")
def domains_overview(level: int = Depends(access_level)):
    q = lambda sql, p=(): db.query(sql, p)[0]["n"]  # noqa: E731
    d7, d1 = iso_days_ago(7), iso_days_ago(1)
    from .domains.infrastructure import national_overview

    inf = national_overview()
    mne_out = 0.0
    try:
        from .domains.mining import dashboard

        mne_out = dashboard(level)["by_status"]["di_luar_izin"]["area_ha"]
    except Exception:
        pass
    latest_ports = db.query("SELECT SUM(vessels) AS v, COUNT(*) AS n FROM port_obs o WHERE source='s1' AND obs_time = "
                            "(SELECT MAX(obs_time) FROM port_obs WHERE port_id = o.port_id AND source='s1')")[0]
    n_done = q("SELECT COUNT(*) AS n FROM analyses WHERE status = 'done'")
    n_aoi = q("SELECT COUNT(*) AS n FROM aois WHERE active = 1")
    n_eq = q("SELECT COUNT(*) AS n FROM events WHERE kind = 'earthquake' AND source = 'bmkg' AND event_time >= ?", (d7,))
    n_hs = q("SELECT COUNT(*) AS n FROM events WHERE kind = 'hotspot' AND event_time >= ?", (d1,))
    n_ports = q("SELECT COUNT(*) AS n FROM ports WHERE active = 1")
    n_layers = q("SELECT COUNT(*) AS n FROM layers")
    n_news = q("SELECT COUNT(*) AS n FROM events WHERE kind = 'news' AND event_time >= ?", (d7,))
    n_news_geo = q("SELECT COUNT(*) AS n FROM events WHERE kind = 'news' AND lon IS NOT NULL AND event_time >= ?", (d7,))
    n_inv = sum(1 for a in inf if a["roads_total_km"] is not None)
    unmapped = sum(a["unmapped_km"] or 0 for a in inf)
    stats = {
        "sat": [f"{n_done} analisa selesai", f"{n_aoi} area dipantau"],
        "dis": [f"{n_eq} gempa BMKG (7 hari)", f"{n_hs} hotspot (24 jam)"],
        "sea": [f"{n_ports} pelabuhan", f"{latest_ports['v'] or 0} kapal radar (pindaian terakhir)"],
        "mne": [f"{n_layers} lapisan izin/kawasan", f"{mne_out:.1f} ha tambang di luar izin"],
        "inf": [f"{n_inv} area berinventaris", f"{unmapped:.1f} km jalan baru belum di OSM"],
        "os": [f"{n_news} berita relevan (7 hari)", f"{n_news_geo} tergeolokasi"],
    }
    reg = common.registry()
    out = []
    for key, d in DOMAINS.items():
        srcs = [s for s in reg if s["domain"] == key]
        access = sorted({s["access"] for s in srcs}, key=lambda a: ACCESS_LEVELS[a])
        out.append({**d, "id": key, "stats": stats[key], "sources": len(srcs), "access": access,
                    "needs_setup": [s["label"] for s in srcs if not s["available"]]})
    return out


@router.get("/sources")
def sources():
    return common.registry()


@router.post("/sources/{source_id}/run", status_code=202)
def run_source(source_id: str):
    try:
        ok = submit_feed(source_id, force=True)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if not ok:
        src = common.SOURCE_BY_ID[source_id]
        avail, why = common.source_available(src)
        raise HTTPException(409, why or "Feed sedang berjalan")
    db.log_activity("feed_manual", f"Feed '{common.SOURCE_BY_ID[source_id]['label']}' dijalankan manual")
    return {"started": source_id}


@router.post("/sources/{source_id}/enable")
def enable_source(source_id: str, on: bool = True):
    if source_id not in common.SOURCE_BY_ID:
        raise HTTPException(404, "Sumber tidak dikenal")
    common.set_feed_enabled(source_id, on)
    db.log_activity("feed_" + ("mulai" if on else "berhenti"),
                    f"Feed '{common.SOURCE_BY_ID[source_id]['label']}' {'diaktifkan' if on else 'dihentikan'}")
    return {"id": source_id, "enabled": on}


# --------------------------------------------------------------------------- peta nasional
def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _pt(lon, lat, props, fid=None):
    return {"type": "Feature", "id": fid, "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


@router.get("/map/layers")
def map_layers(layers: str = "earthquakes,hotspots,news,ports,aois", days: float = 7, level: int = Depends(access_level)):
    want = set(layers.split(","))
    since = iso_days_ago(days)
    out = {}
    if "earthquakes" in want:
        out["earthquakes"] = _fc([_pt(e["lon"], e["lat"], {"id": e["id"], "title": e["title"], "mag": e["value"], "time": e["event_time"],
                                                          "source": e["source"], "depth": e["properties"].get("kedalaman_km"),
                                                          "potensi": e["properties"].get("potensi")}, e["id"])
                                  for e in query_events(kind="earthquake", since=since, level=level, limit=3000)
                                  if not e["properties"].get("duplicate_of")])
    if "hotspots" in want:
        out["hotspots"] = _fc([_pt(e["lon"], e["lat"], {"frp": e["value"], "time": e["event_time"], "conf": e["properties"].get("confidence")})
                               for e in query_events(kind="hotspot", since=iso_days_ago(min(days, 3)), level=level, limit=20000)])
    if "news" in want:
        out["news"] = _fc([_pt(e["lon"], e["lat"], {"id": e["id"], "title": e["title"], "time": e["event_time"],
                                                    "topics": e["properties"].get("topic_labels"), "publisher": e["properties"].get("publisher"),
                                                    "place": (e["properties"].get("location") or {}).get("name"),
                                                    "precision_km": (e["properties"].get("location") or {}).get("precision_km"),
                                                    "score": e["properties"].get("fusion_score", 0)}, e["id"])
                           for e in query_events(kind="news", since=since, level=level, limit=2000, extra_where="lon IS NOT NULL")])
    if "ships" in want:
        out["ships"] = _fc([_pt(e["lon"], e["lat"], {"length_m": e["value"], "time": e["event_time"], "port": e["properties"].get("port")})
                            for e in query_events(kind="ship", since=since, level=level, limit=10000)])
    if "ports" in want:
        rows = db.query("SELECT p.*, (SELECT vessels FROM port_obs WHERE port_id=p.id AND source='s1' ORDER BY obs_time DESC LIMIT 1) AS vessels "
                        "FROM ports p")
        out["ports"] = _fc([_pt(p["lon"], p["lat"], {"id": p["id"], "name": p["name"], "vessels": p["vessels"], "approx": p["approx"],
                                                     "active": p["active"]}, p["id"]) for p in rows])
    if "aois" in want:
        out["aois"] = _fc([{"type": "Feature", "id": a["id"], "geometry": a["geometry"],
                            "properties": {"id": a["id"], "name": a["name"], "active": a["active"]}}
                           for a in db.query("SELECT id, name, geometry, active FROM aois")])
    if "detections" in want:
        rows = db.query(
            "SELECT d.id, d.class, d.category, d.area_ha, d.lon, d.lat, d.analysis_id, d.compliance FROM detections d "
            "JOIN analyses a ON a.id = d.analysis_id WHERE a.status='done' AND (a.aoi_id IS NULL OR a.id = "
            "(SELECT MAX(b.id) FROM analyses b WHERE b.aoi_id = a.aoi_id AND b.status='done')) ORDER BY d.area_ha DESC LIMIT 5000")
        out["detections"] = _fc([_pt(r["lon"], r["lat"], {"id": r["id"], "class": r["class"], "category": r["category"], "area_ha": r["area_ha"],
                                                         "analysis_id": r["analysis_id"],
                                                         "compliance": (r["compliance"] or {}).get("status")}, r["id"]) for r in rows])
    if "permits" in want:
        from .domains.mining import layers_geojson

        out["permits"] = layers_geojson(level, limit=8000)
    return out


# --------------------------------------------------------------------------- DIS
@router.get("/dis/earthquakes")
def earthquakes(days: float = 7, min_mag: float = 0, include_duplicates: bool = False, level: int = Depends(access_level)):
    rows = query_events(kind="earthquake", since=iso_days_ago(days), level=level, limit=2000, extra_where="value >= ?", params=(min_mag,))
    return [r for r in rows if include_duplicates or not r["properties"].get("duplicate_of")]


@router.get("/dis/earthquakes/{event_id}/exposure")
def eq_exposure(event_id: int):
    from .domains.disaster import earthquake_exposure

    ev = db.get("events", event_id)
    if not ev or ev["kind"] != "earthquake":
        raise HTTPException(404, "Gempa tidak ditemukan")
    return earthquake_exposure(ev)


@router.post("/dis/earthquakes/{event_id}/prioritize")
def eq_prioritize(event_id: int):
    """Tandai area pantauan di zona guncangan VI+ agar dianalisa pada pemeriksaan otomatis berikutnya."""
    ex = eq_exposure(event_id)
    ids = [a["id"] for a in ex["aois"] if a["zona"] in ("VI", "VII+")]
    for i in ids:
        db.update("aois", i, {"last_run_at": None})
    db.log_activity("pasca_gempa", f"{len(ids)} area pantauan diprioritaskan untuk analisa pasca-gempa (event #{event_id})")
    return {"aois": ids}


@router.get("/dis/hotspots/summary")
def hotspots_summary(days: float = 1):
    from .domains.disaster import hotspot_exposure

    return hotspot_exposure(days)


# --------------------------------------------------------------------------- SEA
class PortIn(BaseModel):
    name: str
    lon: float = Field(ge=90, le=145)
    lat: float = Field(ge=-15, le=10)
    half_km: float = Field(6.0, ge=1, le=25)
    province: str | None = None


@router.get("/sea/ports")
def ports():
    from .domains.maritime import port_summary

    out = []
    for p in db.query("SELECT * FROM ports ORDER BY name"):
        s = port_summary(p)
        s["observations"] = s["observations"][-12:]
        out.append(s)
    return out


@router.get("/sea/ports/{port_id}")
def port_detail(port_id: int, level: int = Depends(access_level)):
    from .domains.maritime import port_summary

    p = db.get("ports", port_id)
    if not p:
        raise HTTPException(404, "Pelabuhan tidak ditemukan")
    s = port_summary(p)
    if not common.allowed("berlisensi", level):
        s["ais_daily"] = None  # data AIS berlisensi disembunyikan
    latest = s["latest"]["obs_time"] if s["latest"] else None
    s["ships"] = query_events(kind="ship", level=level, limit=2000, extra_where="event_time = ? AND properties LIKE ?",
                              params=(latest, f'%"port_id": {port_id}%')) if latest else []
    return s


@router.post("/sea/ports", status_code=201)
def add_port(body: PortIn):
    from .domains.maritime import port_bbox

    pid = db.insert("ports", {"name": body.name, "lon": body.lon, "lat": body.lat, "bbox": port_bbox(body.lon, body.lat, body.half_km),
                              "province": body.province, "approx": 0, "active": 1, "created_at": db.now_iso()})
    db.log_activity("pelabuhan_ditambah", f"Pelabuhan '{body.name}' ditambahkan")
    return db.get("ports", pid)


@router.delete("/sea/ports/{port_id}", status_code=204)
def delete_port(port_id: int):
    db.execute("DELETE FROM ports WHERE id = ?", (port_id,))


@router.post("/sea/ports/{port_id}/active")
def port_active(port_id: int, on: bool = True):
    db.update("ports", port_id, {"active": int(on)})
    return db.get("ports", port_id)


_scans: set[int] = set()


@router.post("/sea/ports/{port_id}/scan", status_code=202)
def scan_now(port_id: int):
    from .domains.maritime import scan_port

    p = db.get("ports", port_id)
    if not p:
        raise HTTPException(404, "Pelabuhan tidak ditemukan")
    if port_id in _scans:
        raise HTTPException(409, "Pemindaian sedang berjalan")
    _scans.add(port_id)

    def job():
        try:
            res = scan_port(p)
            db.log_activity("pelabuhan_dipindai", f"Pelabuhan '{p['name']}' dipindai: {len(res['observations'])} akuisisi radar")
        except Exception as exc:
            db.log_activity("pelabuhan_gagal", f"Pemindaian '{p['name']}' gagal: {exc}")
        finally:
            _scans.discard(port_id)

    threading.Thread(target=job, daemon=True).start()
    return {"scanning": port_id}


@router.get("/sea/scans")
def scans_running():
    return sorted(_scans)


@router.get("/sea/ais/status")
def ais_status(level: int = Depends(access_level)):
    from .config import settings

    st = db.query("SELECT COUNT(*) AS n, COUNT(DISTINCT mmsi) AS vessels, MIN(ts) AS first, MAX(ts) AS last FROM ais_positions")[0]
    return {"key_configured": bool(settings.aisstream_key), "access": "berlisensi",
            "visible": common.allowed("berlisensi", level), **(st if common.allowed("berlisensi", level) else {})}


@router.post("/sea/ais/import")
async def ais_import(request: Request, source: str = "impor", level: int = Depends(require("berlisensi"))):
    from .domains.maritime import import_ais_csv

    try:
        n = import_ais_csv(await request.body(), source=source)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"inserted": n}


# --------------------------------------------------------------------------- MNE
class ArcgisIn(BaseModel):
    name: str
    kind: str = "izin_tambang"
    access: str = "terbatas"
    url: str
    bbox: list[float] | None = None
    license: str = ""


@router.get("/mne/layers")
def mne_layers(level: int = Depends(access_level)):
    from .domains.mining import LAYER_KINDS

    rows = db.query("SELECT id, name, kind, access, source, license, quality, feature_count, bbox, created_at, updated_at FROM layers ORDER BY id DESC")
    return {"kinds": LAYER_KINDS, "layers": [{**r, "visible": common.allowed(r["access"], level)} for r in rows]}


@router.post("/mne/layers/upload", status_code=201)
async def mne_upload(request: Request, name: str, filename: str, kind: str = "izin_tambang", access: str = "terbatas",
                     license: str = "", level: int = Depends(require("terbatas"))):
    from .domains.mining import apply_compliance, parse_upload, save_layer

    try:
        feats = parse_upload(filename, await request.body())
        lid = save_layer(name, kind, access, feats, source=f"upload:{filename}", license=license)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc))
    _recompute_async()
    return db.get("layers", lid)


@router.post("/mne/layers/arcgis", status_code=201)
def mne_arcgis(body: ArcgisIn, level: int = Depends(require("terbatas"))):
    from .domains.mining import fetch_arcgis, save_layer

    try:
        feats = fetch_arcgis(body.url, body.bbox)
        lid = save_layer(body.name, body.kind, body.access, feats, source=f"arcgis:{body.url}", license=body.license)
    except Exception as exc:
        raise HTTPException(422, f"Gagal mengambil ArcGIS REST: {exc}")
    _recompute_async()
    return db.get("layers", lid)


@router.delete("/mne/layers/{layer_id}", status_code=204)
def mne_delete(layer_id: int, level: int = Depends(require("terbatas"))):
    from .domains.mining import delete_layer

    delete_layer(layer_id)
    _recompute_async()


@router.get("/mne/layers/geojson")
def mne_geojson(bbox: str | None = None, layer_id: int | None = None, level: int = Depends(access_level)):
    from .domains.mining import layers_geojson

    b = [float(v) for v in bbox.split(",")] if bbox else None
    return layers_geojson(level, b, layer_id)


def _recompute_async():
    from .domains.mining import recompute_all

    threading.Thread(target=recompute_all, daemon=True).start()


@router.post("/mne/recompute")
def mne_recompute():
    from .domains.mining import recompute_all

    return {"analyses": recompute_all()}


@router.get("/mne/dashboard")
def mne_dashboard(level: int = Depends(access_level)):
    from .domains.mining import dashboard

    return dashboard(level)


# --------------------------------------------------------------------------- INF
@router.get("/inf/overview")
def inf_overview():
    from .domains.infrastructure import national_overview

    return national_overview()


@router.get("/inf/aois/{aoi_id}")
def inf_aoi(aoi_id: int, refresh: bool = False, inventory: bool = True):
    from .domains.infrastructure import change_metrics, inventory_for_aoi

    aoi = db.get("aois", aoi_id)
    if not aoi:
        raise HTTPException(404, "AOI tidak ditemukan")
    inv, err = None, None
    if inventory:
        try:
            inv = inventory_for_aoi(aoi, refresh=refresh)
        except Exception as exc:
            err = f"Inventaris OSM gagal: {exc}"
    return {"aoi": {"id": aoi["id"], "name": aoi["name"], "bbox": aoi["bbox"]}, "inventory": inv, "inventory_error": err,
            "changes": change_metrics(aoi_id)}


# --------------------------------------------------------------------------- OS
@router.get("/os/topics")
def os_topics():
    from .domains.osint import TOPICS

    return {k: {"domain": v[0], "label": v[1]} for k, v in TOPICS.items()}


@router.get("/os/news")
def os_news(days: float = 7, topic: str | None = None, q: str | None = None, geolocated: bool = False,
            limit: int = 300, level: int = Depends(access_level)):
    extra, params = [], []
    if topic:
        extra.append("properties LIKE ?"), params.append(f'%"{topic}"%')
    if q:
        extra.append("title LIKE ?"), params.append(f"%{q}%")
    if geolocated:
        extra.append("lon IS NOT NULL")
    return query_events(kind="news", since=iso_days_ago(days), level=level, limit=min(limit, 1000),
                        extra_where=" AND ".join(extra), params=tuple(params))


@router.get("/os/news/{event_id}/fusion")
def os_fusion(event_id: int):
    from .domains.osint import fuse

    ev = db.get("events", event_id)
    if not ev or ev["kind"] != "news":
        raise HTTPException(404, "Berita tidak ditemukan")
    return fuse(ev)


def init_background() -> None:
    """Saat start: data referensi (pelabuhan) & unduh gazetteer di latar belakang."""
    from .domains import init
    from .domains.gazetteer import ensure

    init()
    from .config import settings

    if not settings.enable_feeds:
        return

    def gz():
        try:
            ensure()
        except Exception as exc:
            db.log_activity("gazetteer_gagal", f"Gazetteer belum bisa diunduh: {exc}")

    threading.Thread(target=gz, daemon=True).start()

