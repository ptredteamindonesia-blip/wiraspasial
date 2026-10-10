"""Tes end-to-end memakai sumber data demo sintetis (skenario dengan jawaban yang diketahui)."""

import importlib
import time

import pytest

BBOX = (122.1, -2.85, 122.2, -2.75)
BEFORE = ("2023-06-01", "2023-09-30")
AFTER = ("2025-06-01", "2025-09-30")


@pytest.fixture(scope="module")
def demo_result():
    from geoint.change import detect_changes
    from geoint.classifier import classify_all
    from geoint.sources import get_source

    b, a, t = get_source("demo").load_pair(BBOX, BEFORE, AFTER)
    r = detect_changes(b, a, t)
    return r, classify_all(r.patches)


def _by_pixel(result, y, x):
    """Hasil klasifikasi untuk patch yang mencakup piksel (y, x) grid demo."""
    r, classes = result
    lbl = int(r.labels[y, x])
    assert lbl, f"Tidak ada patch di piksel {(y, x)}"
    return next(c for p, c in zip(r.patches, classes) if p.label == lbl)


@pytest.mark.parametrize(
    "pixel, cls, category",
    [
        ((70, 300), "tambang", "manusia"),
        ((270, 250), "penebangan", "manusia"),
        ((250, 300), "jalan_akses", "manusia"),
        ((320, 110), "kebakaran", "perlu_verifikasi"),
    ],
)
def test_human_and_fire_scenarios(demo_result, pixel, cls, category):
    c = _by_pixel(demo_result, *pixel)
    assert c["class"] == cls
    assert c["category"] == category
    assert c["reasons"], "setiap keputusan harus punya alasan"


def test_landslide_is_natural(demo_result):
    r, classes = demo_result
    slides = [c for c in classes if c["class"] == "longsor"]
    assert slides and all(c["category"] == "alami" for c in slides)


def test_flood_is_natural(demo_result):
    r, classes = demo_result
    floods = [c for c in classes if c["class"] == "banjir"]
    assert floods and all(c["category"] == "alami" for c in floods)


def test_settlement_expansion_is_built_up(demo_result):
    r, classes = demo_result
    built = [(p, c) for p, c in zip(r.patches, classes) if c["class"] == "pembangunan"]
    assert built and max(p.features["area_ha"] for p, _ in built) > 20
    assert all(c["category"] == "manusia" for _, c in built)


def test_unchanged_forest_not_detected(demo_result):
    r, _ = demo_result
    assert r.labels[380, 20] == 0  # pojok hutan tanpa gangguan


def test_ramp():
    from geoint.classifier import ramp

    assert ramp(0.5, 0, 1) == 0.5
    assert ramp(2, 0, 1) == 1.0
    assert ramp(0.25, 0.5, 0) == 0.5
    assert ramp(float("nan"), 0, 1) == 0.0


@pytest.fixture(scope="module")
def app_env(tmp_path_factory):
    """Server dengan direktori data terisolasi (dipakai bersama oleh tes API)."""
    data_dir = tmp_path_factory.mktemp("geoint_data")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GEOINT_DATA_DIR", str(data_dir))
        mp.setenv("GEOINT_SCHEDULER", "0")
        mp.setenv("GEOINT_FEEDS", "0")  # jangan memanggil API eksternal dari tes
        import geoint.config

        importlib.reload(geoint.config)
        import geoint.api
        import geoint.db
        import geoint.pipeline
        import geoint.storage

        import geoint.scheduler
        import geoint.domains.common
        import geoint.domains.gazetteer

        for m in (geoint.db, geoint.pipeline, geoint.api, geoint.storage, geoint.scheduler, geoint.domains.common,
                  geoint.domains.gazetteer):
            mp.setattr(m, "settings", geoint.config.settings)
        yield geoint.api.app, data_dir


def _wait(client, analysis_id, timeout=90):
    for _ in range(timeout * 2):
        a = client.get(f"/api/analyses/{analysis_id}").json()
        if a["status"] in ("done", "error"):
            return a
        time.sleep(0.5)
    raise AssertionError(f"analisa {analysis_id} tidak selesai: {a}")


def test_api_end_to_end(app_env):
    from fastapi.testclient import TestClient

    app, _ = app_env
    with TestClient(app) as client:
        meta = client.get("/api/meta").json()
        assert "tambang" in meta["classes"] and meta["presets"]

        aoi = client.post("/api/aois", json={"name": "Uji", "bbox": list(BBOX), "source": "demo"}).json()
        job = client.post(f"/api/aois/{aoi['id']}/run").json()
        a = _wait(client, job["id"])
        assert a["status"] == "done", a["message"]
        assert a["summary"]["by_category"]["manusia"]["area_ha"] > 50
        assert a["products"]["after"].endswith("after.png")
        assert all(a["summary"]["data_sources"][k]["status"] == "ok" for k in ("s1", "worldcover", "gsw", "osm"))
        assert client.get(a["products"]["changes"]).status_code == 200

        fc = client.get(f"/api/analyses/{job['id']}/detections").json()
        assert fc["features"] and {"tambang", "longsor"} <= {f["properties"]["class"] for f in fc["features"]}
        assert client.get(f"/api/analyses/{job['id']}/export.csv").text.startswith("id,kelas")

        alerts = client.get("/api/alerts").json()
        assert any(al["severity"] == "tinggi" for al in alerts)
        assert client.post(f"/api/alerts/{alerts[0]['id']}/ack").json()["acknowledged"] == 1

        bad = client.post("/api/analyses", json={"bbox": list(BBOX), "source": "demo", "before": AFTER, "after": BEFORE})
        assert bad.status_code == 422


def test_data_survives_restart_and_interrupted_job_resumes(app_env):
    """Semua data & aktivitas tetap ada setelah restart; analisa yang terputus dilanjutkan otomatis."""
    from fastapi.testclient import TestClient

    from geoint import db, pipeline, storage

    app, data_dir = app_env
    with TestClient(app) as client:
        aoi = client.post("/api/aois", json={"name": "Restart", "bbox": list(BBOX), "source": "demo"}).json()
        done = _wait(client, client.post(f"/api/aois/{aoi['id']}/run").json()["id"])
        assert done["status"] == "done" and done["inputs"], "data citra mentah harus tersimpan"
        alert_id = client.get("/api/alerts").json()[0]["id"]
        client.post(f"/api/alerts/{alert_id}/ack")
        # Simulasikan server mati saat analisa sedang berjalan.
        interrupted = pipeline.create_analysis(
            {"type": "Polygon", "coordinates": [[[122.1, -2.85], [122.2, -2.85], [122.2, -2.75], [122.1, -2.85]]]},
            BEFORE, AFTER, source="demo", name="Terputus",
        )
        db.update("analyses", interrupted, {"status": "running", "progress": 0.4, "attempts": 1})
        n_before = {t: len(db.query(f"SELECT id FROM {t}")) for t in ("aois", "analyses", "detections", "alerts")}

    # --- server "restart"
    with TestClient(app) as client:
        n_after = {t: len(db.query(f"SELECT id FROM {t}")) for t in ("aois", "analyses", "detections", "alerts")}
        assert n_after["aois"] == n_before["aois"] and n_after["analyses"] == n_before["analyses"]
        assert n_after["alerts"] >= n_before["alerts"]
        assert client.get(f"/api/alerts?include_ack=true").json()  # alert yang sudah ditinjau tetap ada
        assert db.get("alerts", alert_id)["acknowledged"] == 1

        old = client.get(f"/api/analyses/{done['id']}").json()
        assert old["summary"] == done["summary"]
        assert client.get(old["products"]["after"]).status_code == 200
        assert client.get(old["inputs"]["before"]).status_code == 200

        resumed = _wait(client, interrupted)
        assert resumed["status"] == "done"
        assert resumed["attempts"] == 2

        # Analisa ulang memakai citra tersimpan, tanpa memuat ulang dari sumber.
        client.post(f"/api/analyses/{done['id']}/rerun")
        rerun = _wait(client, done["id"])
        assert rerun["status"] == "done" and rerun["summary"]["n_detections"] == done["summary"]["n_detections"]
        assert len(db.query("SELECT id FROM alerts WHERE analysis_id = ?", (done["id"],))) == len(
            [x for x in client.get("/api/alerts?include_ack=true").json() if x["analysis_id"] == done["id"]]
        )

        kinds = [r["kind"] for r in client.get("/api/activity?limit=1000").json()]
        for k in ("server_start", "server_stop", "pantauan_dibuat", "analisa_selesai", "alert", "alert_ditinjau",
                  "analisa_dilanjutkan", "analisa_diulang"):
            assert k in kinds, k
        assert kinds.count("server_start") >= 2

        # Menghapus AOI tidak menghapus riwayat analisa & alert; menghapus analisa menghapus filenya.
        client.delete(f"/api/aois/{aoi['id']}")
        assert db.get("analyses", done["id"]) is not None
        assert db.get("alerts", alert_id) is not None
        client.delete(f"/api/analyses/{done['id']}")
        assert not storage.analysis_dir(done["id"]).exists()
        assert "analisa_dihapus" in [r["kind"] for r in client.get("/api/activity").json()]


def test_ancillary_keeps_scenarios_and_raises_confidence():
    from geoint.change import detect_changes
    from geoint.classifier import classify_all
    from geoint.sources import get_source

    src = get_source("demo")
    b, a, t = src.load_pair(BBOX, BEFORE, AFTER)
    anc = src.load_ancillary(b, BBOX, BEFORE, AFTER)
    base = detect_changes(b, a, t)
    rich = detect_changes(b, a, t, ancillary=anc)
    c0, c1 = classify_all(base.patches), classify_all(rich.patches)
    assert [c["class"] for c in c0] == [c["class"] for c in c1]
    mean = lambda cs, ps: sum(c["confidence"] * p.features["area_ha"] for c, p in zip(cs, ps)) / sum(p.features["area_ha"] for p in ps)
    assert mean(c1, rich.patches) > mean(c0, base.patches)
    mine = next(c for p, c in zip(rich.patches, c1) if c["class"] == "tambang" and (p.features["osm_mining_frac"] or 0) > 0.3)
    assert any("OpenStreetMap" in r for r in mine["reasons"])
    assert any("radar" in n.lower() for n in mine["notes"])


def test_auto_monitoring_start_stop_and_cancel(app_env):
    from fastapi.testclient import TestClient

    from geoint import db, pipeline, scheduler

    app, _ = app_env
    with TestClient(app) as client:
        client.post("/api/monitor/stop")
        assert client.get("/api/monitor").json()["enabled"] is False
        aoi = client.post("/api/aois", json={"name": "Otomatis", "bbox": list(BBOX), "source": "demo", "interval_days": 7}).json()
        assert scheduler.tick() == []  # berhenti -> tidak ada analisa otomatis

        started = client.post("/api/monitor/start").json()
        assert started["enabled"] is True
        auto_ids = [i for i in started["started_analyses"] if db.get("analyses", i)["aoi_id"] == aoi["id"]]
        assert len(auto_ids) == 1 and db.get("analyses", auto_ids[0])["trigger"] == "auto"
        assert _wait(client, auto_ids[0])["status"] == "done"
        assert aoi["id"] not in [db.get("analyses", i)["aoi_id"] for i in scheduler.tick()]  # belum jatuh tempo lagi

        hist = client.get(f"/api/aois/{aoi['id']}/history").json()
        assert len(hist) == 1 and hist[0]["human_ha"] > 0
        stats = client.get("/api/stats").json()
        assert stats["analyses_auto"] >= 1 and stats["detections"] > 0

        # Analisa otomatis yang masih antri dibatalkan saat analisa otomatis dihentikan.
        queued = pipeline.create_analysis(
            {"type": "Polygon", "coordinates": [[[122.1, -2.85], [122.2, -2.85], [122.2, -2.75], [122.1, -2.85]]]},
            BEFORE, AFTER, source="demo", aoi_id=aoi["id"], trigger="auto",
        )
        stopped = client.post("/api/monitor/stop").json()
        assert queued in stopped["cancelled"] and db.get("analyses", queued)["status"] == "cancelled"
        pipeline.run_analysis(queued)  # worker yang mengambilnya harus melewatinya
        assert db.get("analyses", queued)["status"] == "cancelled"

        # Pembatalan analisa yang sedang berjalan berhenti di tahap berikutnya.
        running = pipeline.create_analysis(
            {"type": "Polygon", "coordinates": [[[122.1, -2.85], [122.2, -2.85], [122.2, -2.75], [122.1, -2.85]]]},
            BEFORE, AFTER, source="demo",
        )
        db.update("analyses", running, {"status": "running"})
        assert client.post(f"/api/analyses/{running}/cancel").json()["state"] == "cancelling"
        pipeline.run_analysis(running)
        assert db.get("analyses", running)["status"] == "cancelled"

    # Status "berhenti" bertahan setelah restart.
    with TestClient(app) as client:
        assert client.get("/api/monitor").json()["enabled"] is False
        kinds = [r["kind"] for r in client.get("/api/activity?limit=1000").json()]
        assert {"otomatis_mulai", "otomatis_berhenti", "analisa_dibatalkan"} <= set(kinds)
        client.post("/api/monitor/start")
