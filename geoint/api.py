"""REST API (FastAPI) + penyajian dashboard web."""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import box, mapping, shape

from . import __version__, db, scheduler, storage
from .classifier import CATEGORIES, CLASSES
from .config import ROOT, settings
from .ancillary import SOURCE_LABELS, enabled_sources
from .pipeline import cancel_analysis, create_analysis, rerun_analysis, resume_pending, run_aoi, submit
from .sources import SOURCES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Contoh wilayah pantauan (koordinat perkiraan, ~10x10 km).
PRESETS = [
    {"id": "morowali", "name": "Morowali, Sulawesi Tengah", "theme": "Tambang nikel", "bbox": [122.08, -2.86, 122.18, -2.76]},
    {"id": "weda", "name": "Weda, Halmahera Tengah", "theme": "Tambang nikel", "bbox": [127.88, 0.42, 127.98, 0.52]},
    {"id": "kukar", "name": "Kutai Kartanegara, Kaltim", "theme": "Tambang batu bara", "bbox": [117.05, -0.58, 117.15, -0.48]},
    {"id": "tessonilo", "name": "Tesso Nilo, Riau", "theme": "Perambahan & sawit", "bbox": [101.90, -0.25, 102.00, -0.15]},
    {"id": "pelalawan", "name": "Pelalawan, Riau", "theme": "Kebakaran gambut & HTI", "bbox": [102.00, 0.20, 102.10, 0.30]},
    {"id": "merauke", "name": "Merauke, Papua Selatan", "theme": "Pembukaan lahan skala besar", "bbox": [140.40, -7.90, 140.50, -7.80]},
    {"id": "sigi", "name": "Sigi, Sulawesi Tengah", "theme": "Longsor (alami)", "bbox": [119.88, -1.38, 119.98, -1.28]},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.products_dir.mkdir(parents=True, exist_ok=True)
    counts = {t: db.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("aois", "analyses", "alerts")}
    db.log_activity(
        "server_start",
        f"Server dimulai (data: {settings.data_dir}) — {counts['aois']} area pantauan, "
        f"{counts['analyses']} analisa, {counts['alerts']} alert dimuat",
    )
    from .api_domains import init_background

    init_background()
    # Analisa yang terputus karena server berhenti dilanjutkan otomatis.
    resumed = resume_pending()
    if resumed:
        logging.getLogger(__name__).info("Melanjutkan %d analisa yang terputus: %s", len(resumed), resumed)
    if settings.enable_scheduler:
        scheduler.start()
    yield
    scheduler.stop()
    db.log_activity("server_stop", "Server dihentikan")


app = FastAPI(title="WIRA-SPASIAL", description="Wawasan Intelijen & Ruang Analisis Spasial — enam domain, satu peta nasional",
              version=__version__, lifespan=lifespan)
# Lapisan peta nasional bisa berisi puluhan ribu titik (hotspot): kompres respons besar.
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=2048)


# --------------------------------------------------------------------------- schemas
class Geometry(BaseModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list


class AnalysisIn(BaseModel):
    name: str | None = None
    geometry: Geometry | None = None
    bbox: list[float] | None = Field(None, min_length=4, max_length=4)
    aoi_id: int | None = None
    source: Literal["sentinel2", "demo"] = "sentinel2"
    before: tuple[date, date]
    after: tuple[date, date]


class AoiIn(BaseModel):
    name: str
    geometry: Geometry | None = None
    bbox: list[float] | None = Field(None, min_length=4, max_length=4)
    source: Literal["sentinel2", "demo"] = "sentinel2"
    interval_days: int = Field(30, ge=1, le=365)
    window_days: int = Field(90, ge=15, le=365)
    baseline_days: int = Field(365, ge=30, le=3650)
    active: bool = True


class AoiPatch(BaseModel):
    name: str | None = None
    interval_days: int | None = Field(None, ge=1, le=365)
    window_days: int | None = Field(None, ge=15, le=365)
    baseline_days: int | None = Field(None, ge=30, le=3650)
    active: bool | None = None


def _geom(geometry: Geometry | None, bbox: list[float] | None) -> dict:
    if geometry is not None:
        return geometry.model_dump()
    if bbox is not None:
        return mapping(box(*bbox))
    raise HTTPException(422, "Sertakan 'geometry' atau 'bbox'")


def _not_found(what: str):
    raise HTTPException(404, f"{what} tidak ditemukan")


# --------------------------------------------------------------------------- meta
@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__, "firms": bool(settings.firms_map_key), "data_dir": str(settings.data_dir)}


@app.get("/api/meta")
def meta():
    today = date.today()
    return {
        "classes": CLASSES,
        "categories": CATEGORIES,
        "sources": SOURCES,
        "presets": PRESETS,
        "firms_enabled": bool(settings.firms_map_key),
        "ancillary": {k: {"label": v, "enabled": k in enabled_sources()} for k, v in SOURCE_LABELS.items()},
        "defaults": {
            "before": [(today - timedelta(days=365 + 120)).isoformat(), (today - timedelta(days=365)).isoformat()],
            "after": [(today - timedelta(days=120)).isoformat(), today.isoformat()],
        },
        "limits": {"max_aoi_km2": settings.max_aoi_km2, "min_patch_ha": settings.min_patch_ha},
    }


# --------------------------------------------------------------------------- analyses
@app.post("/api/analyses", status_code=202)
def post_analysis(body: AnalysisIn):
    if body.aoi_id is not None:
        aoi = db.get("aois", body.aoi_id) or _not_found("AOI")
        geometry, name = aoi["geometry"], body.name or aoi["name"]
    else:
        geometry, name = _geom(body.geometry, body.bbox), body.name
    try:
        aid = create_analysis(
            geometry,
            (body.before[0].isoformat(), body.before[1].isoformat()),
            (body.after[0].isoformat(), body.after[1].isoformat()),
            source=body.source,
            name=name,
            aoi_id=body.aoi_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    submit(aid)
    return db.get("analyses", aid)


@app.get("/api/analyses")
def list_analyses(aoi_id: int | None = None, limit: int = 50):
    cols = ("id, aoi_id, name, source, bbox, before_start, before_end, after_start, after_end, status, progress, message, "
            "created_at, finished_at, summary, trigger")
    if aoi_id is None:
        return db.query(f"SELECT {cols} FROM analyses ORDER BY id DESC LIMIT ?", (limit,))
    return db.query(f"SELECT {cols} FROM analyses WHERE aoi_id = ? ORDER BY id DESC LIMIT ?", (aoi_id, limit))


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int):
    a = db.get("analyses", analysis_id) or _not_found("Analisa")
    if a.get("products"):
        a["products"] = {
            **a["products"],
            **{k: f"/products/{analysis_id}/{a['products'][k]}" for k in ("before", "after", "changes")},
        }
    # Data mentah GeoTIFF (komposit sebelum/sesudah 6 band + DEM) yang bisa diunduh untuk QGIS/ArcGIS.
    a["inputs"] = (
        {k: f"/products/{analysis_id}/inputs/{k}.tif" for k in ("before", "after", "dem")}
        if storage.has_inputs(analysis_id) else None
    )
    return a


@app.post("/api/analyses/{analysis_id}/rerun", status_code=202)
def rerun(analysis_id: int):
    a = db.get("analyses", analysis_id) or _not_found("Analisa")
    if a["status"] in ("queued", "running"):
        raise HTTPException(409, "Analisa masih berjalan")
    rerun_analysis(analysis_id)
    return db.get("analyses", analysis_id)


@app.post("/api/analyses/{analysis_id}/cancel")
def cancel(analysis_id: int):
    db.get("analyses", analysis_id) or _not_found("Analisa")
    state = cancel_analysis(analysis_id)
    return {"state": state, "analysis": db.get("analyses", analysis_id)}


@app.delete("/api/analyses/{analysis_id}", status_code=204)
def delete_analysis(analysis_id: int):
    a = db.get("analyses", analysis_id)
    if a is None:
        return
    if a["status"] in ("queued", "running"):
        raise HTTPException(409, "Analisa masih berjalan; tunggu sampai selesai")
    db.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
    storage.delete_analysis_files(analysis_id)
    db.log_activity("analisa_dihapus", f"Analisa #{analysis_id} '{a['name']}' dihapus", analysis_id=analysis_id, aoi_id=a["aoi_id"])


@app.get("/api/analyses/{analysis_id}/detections")
def get_detections(analysis_id: int):
    """Deteksi sebagai GeoJSON FeatureCollection."""
    rows = db.query("SELECT * FROM detections WHERE analysis_id = ? ORDER BY area_ha DESC", (analysis_id,))
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": r["id"],
                "geometry": r.pop("geometry"),
                "properties": {**r, "class_label": CLASSES[r["class"]]["label"], "category_label": CATEGORIES[r["category"]]},
            }
            for r in rows
        ],
    }


@app.get("/api/analyses/{analysis_id}/export.geojson")
def export_geojson(analysis_id: int):
    fc = get_detections(analysis_id)
    return JSONResponse(fc, headers={"Content-Disposition": f'attachment; filename="geoint_analisa_{analysis_id}.geojson"'})


@app.get("/api/analyses/{analysis_id}/export.csv")
def export_csv(analysis_id: int):
    rows = db.query("SELECT * FROM detections WHERE analysis_id = ? ORDER BY area_ha DESC", (analysis_id,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "kelas", "kategori", "keyakinan", "skor_manusia", "luas_ha", "lon", "lat", "alasan", "catatan"])
    for r in rows:
        w.writerow([
            r["id"], CLASSES[r["class"]]["label"], CATEGORIES[r["category"]], f"{r['confidence']:.2f}",
            f"{r['human_score']:.2f}", f"{r['area_ha']:.2f}", f"{r['lon']:.6f}", f"{r['lat']:.6f}",
            " | ".join(r["reasons"]), " | ".join(r["notes"]),
        ])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="geoint_analisa_{analysis_id}.csv"'},
    )


# --------------------------------------------------------------------------- AOI monitoring
@app.get("/api/aois")
def list_aois():
    aois = db.query("SELECT * FROM aois ORDER BY id DESC")
    for a in aois:
        last = db.query(
            "SELECT id, status, finished_at, summary, trigger FROM analyses WHERE aoi_id = ? ORDER BY id DESC LIMIT 1", (a["id"],)
        )
        a["last_analysis"] = last[0] if last else None
        a["history"] = _aoi_history(a["id"], limit=12)
        a["runs_done"] = db.query("SELECT COUNT(*) AS n FROM analyses WHERE aoi_id = ? AND status = 'done'", (a["id"],))[0]["n"]
    return aois


def _aoi_history(aoi_id: int, limit: int = 100) -> list[dict]:
    rows = db.query(
        "SELECT id, after_start, after_end, before_start, before_end, finished_at, trigger, summary FROM analyses "
        "WHERE aoi_id = ? AND status = 'done' ORDER BY after_end DESC, id DESC LIMIT ?",
        (aoi_id, limit),
    )
    out = []
    for r in reversed(rows):
        s = r.pop("summary") or {}
        cat = s.get("by_category", {})
        r.update({
            "n_detections": s.get("n_detections", 0),
            "change_ha": s.get("change_ha", 0),
            "human_ha": cat.get("manusia", {}).get("area_ha", 0),
            "natural_ha": cat.get("alami", {}).get("area_ha", 0),
            "verify_ha": cat.get("perlu_verifikasi", {}).get("area_ha", 0),
            "by_class": {k: v["area_ha"] for k, v in s.get("by_class", {}).items() if v.get("count")},
        })
        out.append(r)
    return out


@app.get("/api/aois/{aoi_id}/history")
def aoi_history(aoi_id: int, limit: int = 100):
    """Deret waktu hasil pemantauan sebuah area (semua analisa selesai, urut periode)."""
    db.get("aois", aoi_id) or _not_found("AOI")
    return _aoi_history(aoi_id, limit)


@app.post("/api/aois", status_code=201)
def post_aoi(body: AoiIn):
    geometry = _geom(body.geometry, body.bbox)
    g = shape(geometry)
    if g.is_empty or not g.is_valid:
        raise HTTPException(422, "Geometri AOI tidak valid")
    aid = db.insert(
        "aois",
        {
            "name": body.name,
            "geometry": geometry,
            "bbox": list(g.bounds),
            "source": body.source,
            "interval_days": body.interval_days,
            "window_days": body.window_days,
            "baseline_days": body.baseline_days,
            "active": int(body.active),
            "created_at": db.now_iso(),
        },
    )
    db.log_activity("pantauan_dibuat", f"Area pantauan '{body.name}' dibuat (tiap {body.interval_days} hari)", aoi_id=aid)
    return db.get("aois", aid)


@app.patch("/api/aois/{aoi_id}")
def patch_aoi(aoi_id: int, body: AoiPatch):
    db.get("aois", aoi_id) or _not_found("AOI")
    values = body.model_dump(exclude_none=True)
    if "active" in values:
        values["active"] = int(values["active"])
    if values:
        db.update("aois", aoi_id, values)
        aoi = db.get("aois", aoi_id)
        changes = ", ".join(f"{k}={v}" for k, v in values.items())
        db.log_activity("pantauan_diubah", f"Area pantauan '{aoi['name']}' diubah: {changes}", aoi_id=aoi_id)
    return db.get("aois", aoi_id)


@app.delete("/api/aois/{aoi_id}", status_code=204)
def delete_aoi(aoi_id: int):
    aoi = db.get("aois", aoi_id)
    if aoi is None:
        return
    # Lepaskan alert dari AOI (bukan ikut terhapus oleh ON DELETE CASCADE) agar riwayat alert tetap ada.
    db.execute("UPDATE alerts SET aoi_id = NULL WHERE aoi_id = ?", (aoi_id,))
    db.execute("DELETE FROM aois WHERE id = ?", (aoi_id,))
    db.log_activity("pantauan_dihapus", f"Area pantauan '{aoi['name']}' dihapus (riwayat analisanya tetap disimpan)", aoi_id=aoi_id)


@app.post("/api/aois/{aoi_id}/run", status_code=202)
def run_aoi_now(aoi_id: int):
    aoi = db.get("aois", aoi_id) or _not_found("AOI")
    if db.query("SELECT 1 FROM analyses WHERE aoi_id = ? AND status IN ('queued','running')", (aoi_id,)):
        raise HTTPException(409, "Area ini masih punya analisa yang antri/berjalan")
    try:
        aid = run_aoi(aoi)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return db.get("analyses", aid)


# --------------------------------------------------------------------------- alerts
@app.get("/api/alerts")
def list_alerts(include_ack: bool = False, limit: int = 100):
    where = "" if include_ack else "WHERE acknowledged = 0"
    return db.query(f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    al = db.get("alerts", alert_id) or _not_found("Alert")
    db.update("alerts", alert_id, {"acknowledged": 1})
    db.log_activity("alert_ditinjau", f"Alert ditandai sudah ditinjau: {al['title']}", analysis_id=al["analysis_id"],
                    aoi_id=al["aoi_id"], alert_id=alert_id)
    return db.get("alerts", alert_id)


# --------------------------------------------------------------------------- analisa otomatis
@app.get("/api/monitor")
def monitor_status():
    return scheduler.status()


@app.post("/api/monitor/start")
def monitor_start():
    """Mulai analisa otomatis (status tersimpan & bertahan setelah restart)."""
    created = scheduler.enable()
    return {**scheduler.status(), "started_analyses": created}


@app.post("/api/monitor/stop")
def monitor_stop(cancel_pending: bool = True):
    """Hentikan analisa otomatis; secara default juga membatalkan analisa otomatis yang antri/berjalan."""
    result = scheduler.disable(cancel_pending=cancel_pending)
    return {**scheduler.status(), **result}


@app.post("/api/monitor/check")
def monitor_check():
    """Periksa sekarang area yang jatuh tempo (hanya bila analisa otomatis aktif)."""
    if not scheduler.is_enabled():
        raise HTTPException(409, "Analisa otomatis sedang berhenti")
    return {"started_analyses": scheduler.tick()}


@app.get("/api/stats")
def stats():
    """Akumulasi hasil pemantauan yang tersimpan di database."""
    q = lambda sql, p=(): db.query(sql, p)[0]["n"]  # noqa: E731
    by_class = db.query(
        "SELECT d.class AS class, COUNT(*) AS n, ROUND(SUM(d.area_ha), 2) AS area_ha FROM detections d "
        "JOIN analyses a ON a.id = d.analysis_id WHERE a.status = 'done' GROUP BY d.class ORDER BY area_ha DESC"
    )
    per_aoi = db.query(
        "SELECT o.id, o.name, COUNT(a.id) AS runs, MIN(a.after_end) AS first_period, MAX(a.after_end) AS last_period "
        "FROM aois o LEFT JOIN analyses a ON a.aoi_id = o.id AND a.status = 'done' GROUP BY o.id ORDER BY o.name"
    )
    return {
        "analyses_done": q("SELECT COUNT(*) AS n FROM analyses WHERE status = 'done'"),
        "analyses_auto": q("SELECT COUNT(*) AS n FROM analyses WHERE status = 'done' AND trigger = 'auto'"),
        "analyses_failed": q("SELECT COUNT(*) AS n FROM analyses WHERE status = 'error'"),
        "detections": q("SELECT COUNT(*) AS n FROM detections"),
        "aois": q("SELECT COUNT(*) AS n FROM aois"),
        "alerts": q("SELECT COUNT(*) AS n FROM alerts"),
        "first_analysis": db.query("SELECT MIN(created_at) AS t FROM analyses")[0]["t"],
        "detections_by_class": by_class,
        "per_aoi": per_aoi,
    }


# --------------------------------------------------------------------------- activity log
@app.get("/api/activity")
def list_activity(limit: int = 200, before_id: int | None = None, analysis_id: int | None = None, aoi_id: int | None = None):
    where, params = [], []
    if before_id is not None:
        where.append("id < ?"), params.append(before_id)
    if analysis_id is not None:
        where.append("analysis_id = ?"), params.append(analysis_id)
    if aoi_id is not None:
        where.append("aoi_id = ?"), params.append(aoi_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return db.query(f"SELECT * FROM activity {clause} ORDER BY id DESC LIMIT ?", (*params, min(limit, 1000)))


# --------------------------------------------------------------------------- domain nasional
from .api_domains import router as domains_router  # noqa: E402

app.include_router(domains_router)


# --------------------------------------------------------------------------- static
@app.get("/products/{path:path}")
def products(path: str):
    """Citra & GeoTIFF hasil analisa. Folder dibaca dari pengaturan aktif (bukan dipatok saat impor)."""
    base = settings.products_dir.resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():  # tolak path traversal (../)
        raise HTTPException(404, "File tidak ditemukan")
    return FileResponse(target)

app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
