"""Konfigurasi aplikasi, dibaca dari environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("GEOINT_DATA_DIR", ROOT / "data")))
    # Resolusi analisa (meter). 20 m = resolusi native band SWIR Sentinel-2.
    resolution_m: float = field(default_factory=lambda: _env_float("GEOINT_RESOLUTION_M", 20.0))
    # Batas luas AOI agar analisa tetap ringan di mesin lokal.
    max_aoi_km2: float = field(default_factory=lambda: _env_float("GEOINT_MAX_AOI_KM2", 1500.0))
    max_cloud_pct: float = field(default_factory=lambda: _env_float("GEOINT_MAX_CLOUD", 70.0))
    max_scenes_per_period: int = field(default_factory=lambda: int(_env_float("GEOINT_MAX_SCENES", 8)))
    # Luas minimum patch perubahan yang dilaporkan (hektar).
    min_patch_ha: float = field(default_factory=lambda: _env_float("GEOINT_MIN_PATCH_HA", 0.5))
    # Luas minimum deteksi aktivitas manusia yang memicu alert (hektar).
    alert_min_ha: float = field(default_factory=lambda: _env_float("GEOINT_ALERT_MIN_HA", 2.0))
    # Opsional: NASA FIRMS MAP_KEY untuk data titik panas (hotspot) kebakaran.
    firms_map_key: str | None = field(default_factory=lambda: os.environ.get("FIRMS_MAP_KEY") or None)
    # Opsional: URL webhook (Slack/Teams/Telegram-bridge/dll) untuk notifikasi alert.
    alert_webhook_url: str | None = field(default_factory=lambda: os.environ.get("GEOINT_ALERT_WEBHOOK") or None)
    # Sumber data pendukung yang dipakai (pisahkan dengan koma): s1, worldcover, gsw, osm.
    ancillary: str = field(default_factory=lambda: os.environ.get("GEOINT_ANCILLARY", "s1,worldcover,gsw,osm"))
    # Interval penjadwal memeriksa area pantauan yang jatuh tempo (menit).
    tick_minutes: float = field(default_factory=lambda: _env_float("GEOINT_TICK_MINUTES", 10.0))
    # Jumlah analisa yang boleh berjalan bersamaan.
    workers: int = field(default_factory=lambda: max(1, int(_env_float("GEOINT_WORKERS", 2))))
    # Token akses untuk data "terbatas" & "berlisensi". Kosong = mode pengguna tunggal (semua terbuka).
    access_token: str | None = field(default_factory=lambda: os.environ.get("GEOINT_ACCESS_TOKEN") or None)
    # API key aisstream.io untuk AIS (data berlisensi/terdaftar).
    aisstream_key: str | None = field(default_factory=lambda: os.environ.get("AISSTREAM_API_KEY") or None)
    # Daftar RSS media tambahan untuk OSINT (pisahkan dengan koma).
    rss_feeds: str = field(default_factory=lambda: os.environ.get(
        "GEOINT_RSS_FEEDS",
        "https://www.antaranews.com/rss/terkini.xml,https://rss.tempo.co/nasional,https://www.cnnindonesia.com/nasional/rss",
    ))
    # Google News RSS: lisensi hanya untuk penggunaan pribadi non-komersial -> bisa dimatikan.
    google_news: bool = field(default_factory=lambda: os.environ.get("GEOINT_GOOGLE_NEWS", "1") != "0")
    # Feed domain nasional (gempa, hotspot, berita, radar pelabuhan, AIS). 0 = matikan (mis. untuk tes/offline).
    enable_feeds: bool = field(default_factory=lambda: os.environ.get("GEOINT_FEEDS", "1") != "0")
    enable_scheduler: bool = field(default_factory=lambda: os.environ.get("GEOINT_SCHEDULER", "1") != "0")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "geoint.db"

    @property
    def products_dir(self) -> Path:
        return self.data_dir / "products"


settings = Settings()
