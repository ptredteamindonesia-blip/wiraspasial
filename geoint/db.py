"""Penyimpanan SQLite: AOI pantauan, analisa (sekaligus status job), deteksi, dan alert."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS aois (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    geometry TEXT NOT NULL,          -- GeoJSON Polygon (EPSG:4326)
    bbox TEXT NOT NULL,              -- [w, s, e, n]
    source TEXT NOT NULL DEFAULT 'sentinel2',
    interval_days INTEGER NOT NULL DEFAULT 30,   -- frekuensi pemantauan otomatis
    window_days INTEGER NOT NULL DEFAULT 90,     -- panjang periode komposit
    baseline_days INTEGER NOT NULL DEFAULT 365,  -- jarak periode pembanding
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aoi_id INTEGER REFERENCES aois(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    geometry TEXT NOT NULL,
    bbox TEXT NOT NULL,
    source TEXT NOT NULL,
    before_start TEXT NOT NULL, before_end TEXT NOT NULL,
    after_start TEXT NOT NULL, after_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',      -- queued | running | done | error
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    summary TEXT,
    products TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    class TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    human_score REAL NOT NULL,
    area_ha REAL NOT NULL,
    lon REAL NOT NULL, lat REAL NOT NULL,
    geometry TEXT NOT NULL,
    features TEXT NOT NULL,
    probs TEXT NOT NULL,
    reasons TEXT NOT NULL,
    notes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_det_analysis ON detections(analysis_id);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER REFERENCES analyses(id) ON DELETE CASCADE,   -- NULL untuk alert domain (gempa, berita, ...)
    aoi_id INTEGER REFERENCES aois(id) ON DELETE SET NULL,
    domain TEXT NOT NULL DEFAULT 'sat',
    severity TEXT NOT NULL,          -- tinggi | sedang | rendah
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
-- Log aktivitas: sengaja tanpa foreign key agar riwayat tetap ada walau objeknya dihapus.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    analysis_id INTEGER,
    aoi_id INTEGER,
    alert_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_activity_created ON activity(created_at);
-- ===================== Domain nasional =====================
-- Peristiwa titik dari semua domain (gempa, hotspot, berita, kapal, ...). Terakumulasi; dedup per sumber.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,            -- sat | dis | sea | mne | inf | os
    kind TEXT NOT NULL,              -- earthquake | hotspot | news | ship | ...
    source TEXT NOT NULL,            -- bmkg | usgs | firms | rss | s1 | ...
    external_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    lon REAL, lat REAL,
    title TEXT,
    value REAL,                      -- magnitudo / FRP / panjang kapal / relevansi
    properties TEXT NOT NULL DEFAULT '{}',
    access TEXT NOT NULL DEFAULT 'publik',
    created_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_events_kind_time ON events(kind, event_time);
CREATE INDEX IF NOT EXISTS ix_events_domain_time ON events(domain, event_time);
-- Lapisan vektor impor (izin tambang, kawasan hutan, kawasan lindung, ...), dengan hak akses.
CREATE TABLE IF NOT EXISTS layers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- izin_tambang | kawasan_hutan | kawasan_lindung | lainnya
    domain TEXT NOT NULL DEFAULT 'mne',
    access TEXT NOT NULL DEFAULT 'terbatas',
    source TEXT,
    license TEXT,
    quality TEXT,
    feature_count INTEGER NOT NULL DEFAULT 0,
    bbox TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS layer_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer_id INTEGER NOT NULL REFERENCES layers(id) ON DELETE CASCADE,
    geometry TEXT NOT NULL,
    properties TEXT NOT NULL,
    minx REAL, miny REAL, maxx REAL, maxy REAL
);
CREATE INDEX IF NOT EXISTS ix_lf_layer ON layer_features(layer_id);
CREATE INDEX IF NOT EXISTS ix_lf_bbox ON layer_features(minx, maxx, miny, maxy);
CREATE TABLE IF NOT EXISTS ports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lon REAL NOT NULL, lat REAL NOT NULL,
    bbox TEXT NOT NULL,
    province TEXT,
    approx INTEGER NOT NULL DEFAULT 0,  -- 1 = koordinat perkiraan
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_scan_at TEXT
);
-- Observasi aktivitas pelabuhan per waktu (jumlah kapal dari radar / AIS).
CREATE TABLE IF NOT EXISTS port_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
    source TEXT NOT NULL,            -- s1 | ais
    obs_time TEXT NOT NULL,
    vessels INTEGER NOT NULL,
    static_targets INTEGER,
    dark_vessels INTEGER,
    properties TEXT NOT NULL DEFAULT '{}',
    UNIQUE(port_id, source, obs_time)
);
CREATE TABLE IF NOT EXISTS ais_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi TEXT NOT NULL,
    ts TEXT NOT NULL,
    lon REAL NOT NULL, lat REAL NOT NULL,
    sog REAL, cog REAL,
    ship_type TEXT, name TEXT,
    source TEXT NOT NULL,
    UNIQUE(mmsi, ts)
);
CREATE INDEX IF NOT EXISTS ix_ais_ts ON ais_positions(ts);
CREATE TABLE IF NOT EXISTS infra_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aoi_id INTEGER,
    bbox TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL
);

-- Pengaturan aplikasi yang harus bertahan setelah restart (mis. status analisa otomatis).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Kolom yang ditambahkan setelah rilis awal: (tabel, kolom, definisi). Diterapkan otomatis
# pada database lama agar data yang sudah ada tetap terbaca.
MIGRATIONS = [
    ("analyses", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("analyses", "started_at", "TEXT"),
    ("analyses", "inputs_cached", "INTEGER NOT NULL DEFAULT 0"),
    ("analyses", "trigger", "TEXT NOT NULL DEFAULT 'manual'"),       # manual | auto
    ("analyses", "cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
    ("detections", "compliance", "TEXT"),                           # status izin tambang & lapisan regulasi
]

JSON_FIELDS = {"geometry", "bbox", "summary", "products", "features", "probs", "reasons", "notes", "properties", "compliance"}

_lock = threading.Lock()
_initialized: set[Path] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect():
    path = settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    with _lock:
        if path not in _initialized:
            _migrate_alerts(conn)
            conn.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            conn.commit()
            _initialized.add(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_alerts(conn) -> None:
    """Database lama: alerts.analysis_id NOT NULL & tanpa kolom domain -> bangun ulang tabel (data dipertahankan)."""
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(alerts)")}
    if not cols or ("domain" in cols and not cols["analysis_id"][3]):
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript('''
        CREATE TABLE alerts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER REFERENCES analyses(id) ON DELETE CASCADE,
            aoi_id INTEGER REFERENCES aois(id) ON DELETE SET NULL,
            domain TEXT NOT NULL DEFAULT 'sat',
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO alerts_new (id, analysis_id, aoi_id, severity, title, message, created_at, acknowledged)
            SELECT id, analysis_id, aoi_id, severity, title, message, created_at, acknowledged FROM alerts;
        DROP TABLE alerts;
        ALTER TABLE alerts_new RENAME TO alerts;
    ''')
    conn.execute("PRAGMA foreign_keys = ON")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


def insert(table: str, values: dict) -> int:
    cols = list(values)
    data = [json.dumps(v) if k in JSON_FIELDS and v is not None else v for k, v in values.items()]
    with connect() as c:
        cur = c.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", data)
        return int(cur.lastrowid)


def update(table: str, row_id: int, values: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in values)
    data = [json.dumps(v) if k in JSON_FIELDS and v is not None else v for k, v in values.items()]
    with connect() as c:
        c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*data, row_id])


def get(table: str, row_id: int) -> dict | None:
    with connect() as c:
        return row_to_dict(c.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as c:
        return [row_to_dict(r) for r in c.execute(sql, params).fetchall()]


def log_activity(kind: str, message: str, analysis_id: int | None = None, aoi_id: int | None = None,
                 alert_id: int | None = None) -> None:
    """Catat aktivitas (pembuatan/penghapusan, mulai/selesai analisa, alert, restart server, dll)."""
    insert("activity", {"created_at": now_iso(), "kind": kind, "message": message,
                        "analysis_id": analysis_id, "aoi_id": aoi_id, "alert_id": alert_id})


def get_setting(key: str, default: str | None = None) -> str | None:
    rows = query("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_iso()),
        )


def execute(sql: str, params: tuple = ()) -> int:
    with connect() as c:
        return c.execute(sql, params).rowcount
