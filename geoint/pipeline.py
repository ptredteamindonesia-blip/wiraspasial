"""Orkestrasi analisa: muat citra → deteksi perubahan → klasifikasi → simpan → alert."""

from __future__ import annotations

import json
import logging
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
from rasterio import features as rfeatures
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import box, mapping, shape

from . import db
from .change import detect_changes
from .classifier import CATEGORIES, CLASSES, classify_all, classify_coastal
from .coastal import detect_coastal
from .config import settings
from .firms import fetch_hotspots
from .products import write_products
from .sources import get_source
from . import storage
from .ancillary import SOURCE_LABELS
from .sources.sentinel import bbox_area_km2

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="analisa")


class AnalysisCancelled(Exception):
    pass
# Analisa yang terputus dilanjutkan otomatis saat server start, maksimal sebanyak ini
# (mencegah loop restart bila analisa itu sendiri yang membuat server crash, mis. kehabisan memori).
MAX_ATTEMPTS = 3


# --------------------------------------------------------------------------- job API
def create_analysis(
    geometry: dict,
    before: tuple[str, str],
    after: tuple[str, str],
    source: str = "sentinel2",
    name: str | None = None,
    aoi_id: int | None = None,
    trigger: str = "manual",
) -> int:
    geom = shape(geometry)
    if geom.is_empty or not geom.is_valid:
        raise ValueError("Geometri AOI tidak valid")
    bbox = list(geom.bounds)
    if source != "demo" and bbox_area_km2(bbox) > settings.max_aoi_km2:
        raise ValueError(
            f"AOI terlalu luas ({bbox_area_km2(bbox):.0f} km²). Maksimum {settings.max_aoi_km2:.0f} km² — perkecil area."
        )
    for s, e in (before, after):
        if date.fromisoformat(s) >= date.fromisoformat(e):
            raise ValueError(f"Rentang tanggal tidak valid: {s}..{e}")
    if date.fromisoformat(before[1]) > date.fromisoformat(after[0]):
        raise ValueError("Periode 'sebelum' harus berakhir sebelum periode 'sesudah' dimulai")
    aid = db.insert(
        "analyses",
        {
            "aoi_id": aoi_id,
            "name": name or f"Analisa {date.today().isoformat()}",
            "geometry": mapping(geom),
            "bbox": bbox,
            "source": source,
            "before_start": before[0],
            "before_end": before[1],
            "after_start": after[0],
            "after_end": after[1],
            "status": "queued",
            "progress": 0.0,
            "message": "Menunggu antrian",
            "trigger": trigger,
            "created_at": db.now_iso(),
        },
    )
    db.log_activity(
        "analisa_dibuat",
        f"Analisa #{aid} '{name or ''}' dibuat ({source}, {before[0]}..{before[1]} vs {after[0]}..{after[1]})",
        analysis_id=aid,
        aoi_id=aoi_id,
    )
    return aid


def rerun_analysis(analysis_id: int) -> None:
    """Jalankan ulang analisa (memakai data citra tersimpan bila ada)."""
    db.update("analyses", analysis_id, {"status": "queued", "progress": 0.0, "message": "Menunggu antrian (analisa ulang)",
                                        "attempts": 0, "finished_at": None, "cancel_requested": 0})
    db.log_activity("analisa_diulang", f"Analisa #{analysis_id} dijalankan ulang", analysis_id=analysis_id)
    submit(analysis_id)


def resume_pending() -> list[int]:
    """Dipanggil saat server start: lanjutkan analisa yang terputus karena server berhenti."""
    resumed = []
    for a in db.query("SELECT id, name, attempts, inputs_cached FROM analyses WHERE status IN ('queued','running') ORDER BY id"):
        if a["attempts"] >= MAX_ATTEMPTS:
            msg = f"Dihentikan: sudah {a['attempts']}x terputus/gagal saat berjalan. Klik 'Jalankan ulang' untuk mencoba lagi."
            db.update("analyses", a["id"], {"status": "error", "message": msg, "finished_at": db.now_iso()})
            db.log_activity("analisa_gagal", f"Analisa #{a['id']} tidak dilanjutkan: {msg}", analysis_id=a["id"])
            continue
        note = " (memakai data citra tersimpan)" if a["inputs_cached"] else ""
        db.update("analyses", a["id"], {"status": "queued", "progress": 0.0, "message": f"Dilanjutkan setelah server restart{note}"})
        db.log_activity("analisa_dilanjutkan", f"Analisa #{a['id']} '{a['name']}' dilanjutkan setelah restart{note}", analysis_id=a["id"])
        submit(a["id"])
        resumed.append(a["id"])
    return resumed


def cancel_analysis(analysis_id: int) -> str:
    """Batalkan analisa: yang masih antri langsung dibatalkan, yang sedang berjalan berhenti di tahap berikutnya."""
    a = db.get("analyses", analysis_id)
    if a is None or a["status"] not in ("queued", "running"):
        return a["status"] if a else "missing"
    if a["status"] == "queued":
        db.update("analyses", analysis_id, {"status": "cancelled", "message": "Dibatalkan sebelum berjalan",
                                            "finished_at": db.now_iso(), "cancel_requested": 1})
        db.log_activity("analisa_dibatalkan", f"Analisa #{analysis_id} dibatalkan (masih antri)", analysis_id=analysis_id,
                        aoi_id=a["aoi_id"])
        return "cancelled"
    db.update("analyses", analysis_id, {"cancel_requested": 1, "message": "Membatalkan… (berhenti di tahap berikutnya)"})
    return "cancelling"


def submit(analysis_id: int):
    return _executor.submit(run_analysis, analysis_id)


def monitoring_periods(window_days: int, baseline_days: int, today: date | None = None):
    today = today or date.today()
    after = (today - timedelta(days=window_days), today)
    before = (after[0] - timedelta(days=baseline_days), after[1] - timedelta(days=baseline_days))
    return (before[0].isoformat(), before[1].isoformat()), (after[0].isoformat(), after[1].isoformat())


def run_aoi(aoi: dict, trigger: str = "manual") -> int:
    before, after = monitoring_periods(aoi["window_days"], aoi["baseline_days"])
    aid = create_analysis(
        aoi["geometry"], before, after, source=aoi["source"], name=f"Pantauan {aoi['name']} {after[1]}", aoi_id=aoi["id"],
        trigger=trigger,
    )
    db.update("aois", aoi["id"], {"last_run_at": db.now_iso()})
    how = "otomatis" if trigger == "auto" else "manual"
    db.log_activity("pantauan_dijalankan", f"Pemantauan '{aoi['name']}' dijalankan ({how}) -> analisa #{aid}", analysis_id=aid,
                    aoi_id=aoi["id"])
    submit(aid)
    return aid


# --------------------------------------------------------------------------- worker
def _aoi_mask(geometry: dict, scene) -> np.ndarray:
    geom = geometry if scene.crs == "EPSG:4326" else transform_geom("EPSG:4326", scene.crs, geometry)
    return rfeatures.geometry_mask([geom], out_shape=scene.shape, transform=scene.transform, invert=True, all_touched=True)


def _assign_hotspots(points, scene, labels, patches) -> int:
    if not points:
        return 0
    lons, lats = zip(*points)
    xs, ys = (lons, lats) if scene.crs == "EPSG:4326" else warp_transform("EPSG:4326", scene.crs, lons, lats)
    inv = ~scene.transform
    by_label = {p.label: p for p in patches}
    inside = 0
    for x, y in zip(xs, ys):
        col, row = inv @ (x, y)
        r, c = int(row), int(col)
        if 0 <= r < labels.shape[0] and 0 <= c < labels.shape[1]:
            inside += 1
            lbl = int(labels[r, c])
            if lbl in by_label:
                by_label[lbl].features["hotspots"] += 1
    return inside


def run_analysis(analysis_id: int) -> None:
    a = db.get("analyses", analysis_id)
    if a is None or a["status"] == "cancelled":
        return

    def progress(p: float, msg: str):
        if (db.get("analyses", analysis_id) or {}).get("cancel_requested"):
            raise AnalysisCancelled()
        db.update("analyses", analysis_id, {"progress": round(p, 3), "message": msg, "status": "running"})

    db.update("analyses", analysis_id, {"attempts": (a.get("attempts") or 0) + 1, "started_at": db.now_iso()})
    db.log_activity("analisa_mulai", f"Analisa #{analysis_id} mulai berjalan", analysis_id=analysis_id, aoi_id=a["aoi_id"])
    try:
        progress(0.02, "Memulai analisa")
        cached = storage.load_inputs(analysis_id)
        if cached is not None:
            progress(0.30, "Memuat data citra tersimpan (tanpa unduh ulang)")
            before, after, terrain = cached
        else:
            source = get_source(a["source"])
            before_p = (a["before_start"], a["before_end"])
            after_p = (a["after_start"], a["after_end"])
            before, after, terrain = source.load_pair(tuple(a["bbox"]), before_p, after_p, progress)
            progress(0.60, "Menyimpan data citra mentah (GeoTIFF)")
            storage.save_inputs(analysis_id, before, after, terrain)
            db.update("analyses", analysis_id, {"inputs_cached": 1})

        # Data pendukung (radar, tutupan lahan, riwayat air, OSM): pakai simpanan bila lengkap.
        anc = storage.load_ancillary(analysis_id)
        if anc is None or any(v.get("status") == "error" for v in anc.sources.values()):
            source = get_source(a["source"])
            anc = source.load_ancillary(before, tuple(a["bbox"]), before.period, after.period, progress)
            storage.save_ancillary(analysis_id, before, anc)
        else:
            progress(0.66, "Memuat data pendukung tersimpan")

        progress(0.65, "Mendeteksi perubahan tutupan hutan")
        synthetic = bool(before.meta.get("synthetic"))
        aoi_mask = None if synthetic else _aoi_mask(a["geometry"], before)
        result = detect_changes(before, after, terrain, aoi_mask=aoi_mask, min_patch_ha=settings.min_patch_ha, ancillary=anc)
        progress(0.70, "Mendeteksi perubahan pesisir (abrasi, akresi, reklamasi, tambak)")
        coast_patches, coast_labels = detect_coastal(before, after, terrain, result.labels, aoi_mask=aoi_mask, ancillary=anc,
                                                     min_patch_ha=settings.min_patch_ha, label_offset=int(result.labels.max()))
        labels_all = np.where(result.labels > 0, result.labels, coast_labels)

        hotspots_total = 0
        if not synthetic and settings.firms_map_key:
            progress(0.72, "Mengambil titik panas NASA FIRMS")
            pts = fetch_hotspots(a["bbox"], a["before_end"], a["after_end"])
            hotspots_total = _assign_hotspots(pts, before, labels_all, result.patches + coast_patches)

        progress(0.80, f"Mengklasifikasi {len(result.patches) + len(coast_patches)} area perubahan")
        patches = result.patches + coast_patches
        classes = classify_all(result.patches) + [classify_coastal(p.features) for p in coast_patches]

        progress(0.90, "Menyimpan hasil & membuat citra pratinjau")
        firms_status = (
            {"status": "ok", "detail": f"{hotspots_total} titik panas di area"} if settings.firms_map_key and not synthetic
            else {"status": "off", "detail": "isi FIRMS_MAP_KEY untuk mengaktifkan"}
        )
        data_sources = {
            "sentinel2": {"label": "Sentinel-2 L2A (optik)", "status": "ok",
                          "detail": "sintetis (demo)" if synthetic else f"{before.n_scenes} + {after.n_scenes} akuisisi"},
            "dem": {"label": "Copernicus DEM GLO-30", "status": "ok", "detail": "sintetis (demo)" if synthetic else "lereng & elevasi"},
            **{k: {"label": SOURCE_LABELS[k], **v} for k, v in anc.sources.items()},
            "firms": {"label": "NASA FIRMS (titik panas)", **firms_status},
        }
        products = write_products(
            storage.analysis_dir(analysis_id),
            before,
            after,
            labels_all,
            {p.label: c["class"] for p, c in zip(patches, classes)},
        )
        if synthetic:
            west, south = products["bounds"][0][1], products["bounds"][0][0]
            east, north = products["bounds"][1][1], products["bounds"][1][0]
            db.update("analyses", analysis_id, {"geometry": mapping(box(west, south, east, north))})

        with db.connect() as conn:
            conn.execute("DELETE FROM detections WHERE analysis_id = ?", (analysis_id,))
            for p, c in zip(patches, classes):
                feats = {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in p.features.items()}
                conn.execute(
                    "INSERT INTO detections (analysis_id, class, category, confidence, human_score, area_ha, lon, lat,"
                    " geometry, features, probs, reasons, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        analysis_id,
                        c["class"],
                        c["category"],
                        round(c["confidence"], 4),
                        round(c["human_score"], 4),
                        round(p.features["area_ha"], 3),
                        p.centroid[0],
                        p.centroid[1],
                        json.dumps(p.geometry),
                        json.dumps(feats),
                        json.dumps({k: round(v, 4) for k, v in c["probs"].items()}),
                        json.dumps(c["reasons"]),
                        json.dumps(c["notes"]),
                    ),
                )

        summary = _summarize(result, patches, classes, before, after, hotspots_total, synthetic)
        summary["data_sources"] = data_sources
        summary["coastal_change_ha"] = round(sum(p.features["area_ha"] for p in coast_patches), 2)
        summary["landcover_wc"] = _landcover(anc, aoi_mask, before.valid)
        # Domain MNE: uji deteksi tambang terhadap lapisan izin/kawasan, disimpan bersama ringkasan.
        from .domains.mining import apply_compliance, compliance_alerts

        summary["mining_compliance"] = apply_compliance(analysis_id, raise_alerts=False, update_summary=False)
        db.update(
            "analyses",
            analysis_id,
            {
                "status": "done",
                "progress": 1.0,
                "message": f"Selesai: {len(classes)} area perubahan terdeteksi",
                "summary": summary,
                "products": products,
                "finished_at": db.now_iso(),
            },
        )
        human = summary["by_category"]["manusia"]["area_ha"]
        db.log_activity(
            "analisa_selesai",
            f"Analisa #{analysis_id} selesai: {len(classes)} area perubahan, {human:.1f} ha aktivitas manusia",
            analysis_id=analysis_id,
            aoi_id=a["aoi_id"],
        )
        _raise_alerts(db.get("analyses", analysis_id), summary)
        compliance_alerts(db.get("analyses", analysis_id), summary["mining_compliance"])
    except AnalysisCancelled:
        db.update("analyses", analysis_id, {"status": "cancelled", "message": "Dibatalkan", "finished_at": db.now_iso()})
        db.log_activity("analisa_dibatalkan", f"Analisa #{analysis_id} dibatalkan saat berjalan", analysis_id=analysis_id,
                        aoi_id=a["aoi_id"])
    except Exception as exc:
        log.error("Analisa %s gagal:\n%s", analysis_id, traceback.format_exc())
        msg = f"{type(exc).__name__}: {exc}"
        db.update("analyses", analysis_id, {"status": "error", "message": msg, "finished_at": db.now_iso()})
        db.log_activity("analisa_gagal", f"Analisa #{analysis_id} gagal: {msg}", analysis_id=analysis_id, aoi_id=a["aoi_id"])


WC_GROUPS = {"hutan": (10,), "mangrove": (95,), "semak/rumput": (20, 30), "pertanian": (40,), "terbangun": (50,),
             "terbuka": (60,), "air": (80,), "lahan basah": (90,)}


def _landcover(anc, aoi_mask, valid) -> dict | None:
    """Fraksi tutupan lahan ESA WorldCover 2021 di area analisa (konteks urban/infrastruktur)."""
    wc = anc.layers.get("worldcover") if anc is not None else None
    if wc is None or wc.shape != valid.shape:
        return None
    m = valid & np.isfinite(wc) & (wc > 0)
    if aoi_mask is not None:
        m &= aoi_mask
    if not m.any():
        return None
    vals = wc[m]
    return {k: round(float(np.isin(vals, codes).mean()), 4) for k, codes in WC_GROUPS.items()}


def _summarize(result, patches, classes, before, after, hotspots_total: int, synthetic: bool) -> dict:
    by_class = {k: {"label": v["label"], "count": 0, "area_ha": 0.0} for k, v in CLASSES.items()}
    by_cat = {k: {"label": v, "count": 0, "area_ha": 0.0} for k, v in CATEGORIES.items()}
    for p, c in zip(patches, classes):
        area = p.features["area_ha"]
        by_class[c["class"]]["count"] += 1
        by_class[c["class"]]["area_ha"] += area
        by_cat[c["category"]]["count"] += 1
        by_cat[c["category"]]["area_ha"] += area
    for d in (*by_class.values(), *by_cat.values()):
        d["area_ha"] = round(d["area_ha"], 2)
    forest = result.forest_before_ha
    return {
        "n_detections": len(classes),
        "forest_before_ha": round(forest, 1),
        "change_ha": round(result.loss_ha, 2),
        "change_pct_of_forest": round(100 * result.loss_ha / forest, 3) if forest else 0.0,
        "valid_fraction": round(result.valid_fraction, 3),
        "by_class": by_class,
        "by_category": by_cat,
        "hotspots": hotspots_total,
        "synthetic": synthetic,
        "scenes": {
            "before": {"n": before.n_scenes, **{k: v for k, v in before.meta.items() if k != "synthetic"}},
            "after": {"n": after.n_scenes, **{k: v for k, v in after.meta.items() if k != "synthetic"}},
        },
    }


def _raise_alerts(analysis: dict, summary: dict) -> None:
    human = summary["by_category"]["manusia"]
    verify = summary["by_category"]["perlu_verifikasi"]
    alerts = []
    if human["area_ha"] >= settings.alert_min_ha:
        top = sorted(
            ((k, v) for k, v in summary["by_class"].items() if CLASSES[k]["nature"] == "manusia" and v["count"]),
            key=lambda kv: -kv[1]["area_ha"],
        )
        parts = ", ".join(f"{v['label']} {v['area_ha']:.1f} ha" for _, v in top)
        severity = "tinggi" if human["area_ha"] >= 50 or summary["by_class"]["tambang"]["count"] else (
            "sedang" if human["area_ha"] >= 10 else "rendah"
        )
        alerts.append((severity, f"Aktivitas manusia terdeteksi: {human['area_ha']:.1f} ha", parts))
    if verify["area_ha"] >= max(settings.alert_min_ha, 10):
        alerts.append(
            ("sedang", f"{verify['area_ha']:.1f} ha perubahan perlu verifikasi lapangan", "Termasuk area terbakar tanpa konteks jelas")
        )
    for severity, title, msg in alerts:
        full_title = f"[{analysis['name']}] {title}"
        # Analisa ulang tidak menggandakan alert yang sama.
        if db.query("SELECT 1 FROM alerts WHERE analysis_id = ? AND title = ?", (analysis["id"], full_title)):
            continue
        alert = {
            "analysis_id": analysis["id"],
            "aoi_id": analysis["aoi_id"],
            "severity": severity,
            "title": full_title,
            "message": msg,
            "created_at": db.now_iso(),
        }
        alert_id = db.insert("alerts", alert)
        db.log_activity("alert", f"Alert {severity}: {full_title}", analysis_id=analysis["id"], aoi_id=analysis["aoi_id"],
                        alert_id=alert_id)
        _post_webhook(alert)


def _post_webhook(alert: dict) -> None:
    if not settings.alert_webhook_url:
        return
    body = json.dumps({"text": f"🛰 WIRA-SPASIAL alert ({alert['severity']}): {alert['title']} — {alert['message']}", **alert}).encode()
    req = urllib.request.Request(settings.alert_webhook_url, data=body, headers={"Content-Type": "application/json"})
    try:
        from .domains.common import SSL_CONTEXT

        urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT).close()
    except Exception as exc:
        log.warning("Webhook alert gagal: %s", exc)
