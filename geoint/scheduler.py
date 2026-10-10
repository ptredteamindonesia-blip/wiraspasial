"""Analisa otomatis: pemantauan berkala area pantauan + feed domain nasional (gempa, hotspot, berita, pelabuhan).

- Sakelar global "analisa otomatis" (mulai/hentikan) disimpan di database, sehingga
  statusnya bertahan setelah server direstart.
- Setiap `tick` (default 10 menit) area aktif yang sudah jatuh tempo dianalisa; area yang
  masih punya analisa antri/berjalan dilewati agar tidak dobel.
- Menghentikan analisa otomatis dapat sekaligus membatalkan analisa otomatis yang antri/berjalan.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .config import settings
from .pipeline import cancel_analysis, run_aoi

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_tick_lock = threading.Lock()
JOB_ID = "monitor"
KEY_ENABLED = "auto_monitor_enabled"
KEY_LAST_TICK = "auto_monitor_last_tick"


def is_enabled() -> bool:
    return db.get_setting(KEY_ENABLED, "1") == "1"


def due_aois(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    busy = {r["aoi_id"] for r in db.query("SELECT DISTINCT aoi_id FROM analyses WHERE status IN ('queued','running') AND aoi_id IS NOT NULL")}
    due = []
    for aoi in db.query("SELECT * FROM aois WHERE active = 1"):
        if aoi["id"] in busy:
            continue
        last = aoi["last_run_at"]
        if last is None or datetime.fromisoformat(last) + timedelta(days=aoi["interval_days"]) <= now:
            due.append(aoi)
    return due


def tick(force: bool = False) -> list[int]:
    """Jalankan analisa untuk area yang jatuh tempo. Mengembalikan id analisa yang dibuat."""
    if not force and not is_enabled():
        return []
    if not _tick_lock.acquire(blocking=False):
        return []
    try:
        db.set_setting(KEY_LAST_TICK, db.now_iso())
        # Feed domain nasional (gempa, hotspot, berita, radar pelabuhan, AIS) yang jatuh tempo.
        try:
            from .domains import run_due_feeds

            if settings.enable_feeds:
                run_due_feeds()
        except Exception as exc:
            log.error("Feed domain gagal dijadwalkan: %s", exc)
        created = []
        for aoi in due_aois():
            try:
                created.append(run_aoi(aoi, trigger="auto"))
                log.info("Pemantauan otomatis AOI %s (%s) -> analisa #%s", aoi["id"], aoi["name"], created[-1])
            except Exception as exc:
                log.error("Pemantauan AOI %s gagal dijadwalkan: %s", aoi["id"], exc)
                db.log_activity("pantauan_gagal", f"Pemantauan '{aoi['name']}' gagal dijadwalkan: {exc}", aoi_id=aoi["id"])
        return created
    finally:
        _tick_lock.release()


def enable() -> list[int]:
    """Mulai analisa otomatis & langsung periksa area yang jatuh tempo."""
    db.set_setting(KEY_ENABLED, "1")
    db.log_activity("otomatis_mulai", "Analisa otomatis dimulai")
    return tick()


def disable(cancel_pending: bool = True) -> dict:
    """Hentikan analisa otomatis. Opsional: batalkan analisa otomatis yang masih antri/berjalan."""
    db.set_setting(KEY_ENABLED, "0")
    result = {"cancelled": [], "cancelling": []}
    if cancel_pending:
        for a in db.query("SELECT id FROM analyses WHERE trigger = 'auto' AND status IN ('queued','running')"):
            state = cancel_analysis(a["id"])
            if state in result:
                result[state].append(a["id"])
    n = len(result["cancelled"]) + len(result["cancelling"])
    db.log_activity("otomatis_berhenti", "Analisa otomatis dihentikan" + (f"; {n} analisa otomatis dibatalkan" if n else ""))
    return result


def status() -> dict:
    job = _scheduler.get_job(JOB_ID) if _scheduler else None
    active = db.query("SELECT COUNT(*) AS n FROM aois WHERE active = 1")[0]["n"]
    running = db.query(
        "SELECT id, name, aoi_id, status, progress, trigger FROM analyses WHERE status IN ('queued','running') ORDER BY id"
    )
    next_due = None
    for aoi in db.query("SELECT last_run_at, interval_days FROM aois WHERE active = 1"):
        t = (
            datetime.now(timezone.utc)
            if aoi["last_run_at"] is None
            else datetime.fromisoformat(aoi["last_run_at"]) + timedelta(days=aoi["interval_days"])
        )
        next_due = t if next_due is None or t < next_due else next_due
    from .domains import running_feeds

    return {
        "running_feeds": running_feeds(),
        "enabled": is_enabled(),
        "scheduler_running": bool(_scheduler and _scheduler.running),
        "tick_minutes": settings.tick_minutes,
        "workers": settings.workers,
        "last_tick": db.get_setting(KEY_LAST_TICK),
        "next_tick": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "next_due": next_due.isoformat() if next_due else None,
        "active_aois": active,
        "due_aois": len(due_aois()),
        "running": running,
    }


def start() -> None:
    """Jalankan thread penjadwal (dipanggil saat server start)."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        tick, "interval", minutes=settings.tick_minutes, id=JOB_ID, max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    _scheduler.start()
    log.info("Penjadwal aktif (cek tiap %s menit, analisa otomatis %s)", settings.tick_minutes,
             "AKTIF" if is_enabled() else "BERHENTI")


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
