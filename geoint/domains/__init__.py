"""Modul domain nasional: SAT (di pipeline utama), DIS, SEA, MNE, INF, OS.

Feed otomatis tiap domain dijalankan penjadwal di thread terpisah agar feed yang lambat
(mis. pemindaian radar pelabuhan) tidak menahan analisa area pantauan.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import db
from .common import SOURCE_BY_ID, feed_due, feed_enabled, source_available

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feed")
_running: set[str] = set()
_lock = threading.Lock()


def _runner(source_id: str):
    if source_id in ("bmkg", "usgs", "firms"):
        from .disaster import run_feed
    elif source_id in ("s1_ports", "aisstream"):
        from .maritime import run_feed
    elif source_id in ("rss_media", "gnews"):
        from .osint import run_feed
    else:
        raise ValueError(f"Feed tidak dikenal: {source_id}")
    return run_feed


def _run(source_id: str) -> None:
    try:
        n = _runner(source_id)(source_id)
        log.info("Feed %s selesai: %s data baru", source_id, n)
    finally:
        with _lock:
            _running.discard(source_id)


def submit_feed(source_id: str, force: bool = False) -> bool:
    """Jadwalkan satu feed. False bila sedang berjalan, nonaktif, atau sumber belum tersedia (mis. tanpa API key)."""
    src = SOURCE_BY_ID.get(source_id)
    if not src or src["kind"] != "feed":
        raise ValueError(f"Bukan feed: {source_id}")
    avail, _ = source_available(src)
    if not avail or (not force and not feed_enabled(source_id)):
        return False
    with _lock:
        if source_id in _running:
            return False
        _running.add(source_id)
    _executor.submit(_run, source_id)
    return True


def run_due_feeds() -> list[str]:
    started = []
    for sid, src in SOURCE_BY_ID.items():
        if src["kind"] == "feed" and feed_due(sid) and submit_feed(sid):
            started.append(sid)
    return started


def running_feeds() -> list[str]:
    with _lock:
        return sorted(_running)


def init() -> None:
    """Dipanggil saat server start: data referensi awal (pelabuhan)."""
    from .maritime import seed_ports

    n = seed_ports()
    if n:
        db.log_activity("pelabuhan_awal", f"{n} pelabuhan utama Indonesia ditambahkan (koordinat perkiraan, bisa diedit)")
