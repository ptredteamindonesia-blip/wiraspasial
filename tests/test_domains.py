"""Tes domain nasional (offline: API eksternal disimulasikan)."""

import importlib
import json
import time

import numpy as np
import pytest
from affine import Affine

BBOX = (122.1, -2.85, 122.2, -2.75)
BEFORE = ("2023-06-01", "2023-09-30")
AFTER = ("2025-06-01", "2025-09-30")

GAZ = [
    {"name": "Kabupaten Morowali", "level": "kabkota", "lon": 122.15, "lat": -2.80, "osm_id": 1},
    {"name": "Kota Bogor", "level": "kabkota", "lon": 106.80, "lat": -6.59, "osm_id": 2},
    {"name": "Kabupaten Bogor", "level": "kabkota", "lon": 106.95, "lat": -6.55, "osm_id": 3},
    {"name": "Kota Batu", "level": "kabkota", "lon": 112.52, "lat": -7.87, "osm_id": 4},
    {"name": "Sulawesi Tengah", "level": "provinsi", "lon": 121.4, "lat": -1.4, "osm_id": 5},
]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("geoint_domains")
    (data_dir / "gazetteer.json").write_text(json.dumps(GAZ), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GEOINT_DATA_DIR", str(data_dir))
        mp.setenv("GEOINT_SCHEDULER", "0")
        mp.setenv("GEOINT_FEEDS", "0")
        import geoint.config

        importlib.reload(geoint.config)
        import geoint.api
        import geoint.db
        import geoint.domains.common
        import geoint.domains.gazetteer
        import geoint.pipeline
        import geoint.scheduler
        import geoint.storage

        for m in (geoint.db, geoint.pipeline, geoint.api, geoint.storage, geoint.scheduler, geoint.domains.common,
                  geoint.domains.gazetteer):
            mp.setattr(m, "settings", geoint.config.settings)
        geoint.domains.gazetteer._index.cache_clear()
        mp.setattr(geoint.domains.gazetteer, "indonesia_shape", lambda buffer_deg=0.02: None)
        from fastapi.testclient import TestClient

        with TestClient(geoint.api.app) as client:
            yield client


def _wait(client, aid):
    for _ in range(180):
        a = client.get(f"/api/analyses/{aid}").json()
        if a["status"] in ("done", "error"):
            return a
        time.sleep(0.5)
    raise AssertionError(a)


def test_geocode_text(env):
    from geoint.domains.gazetteer import geocode_text

    assert geocode_text("Tambang ilegal di Kabupaten Morowali disegel")["name"] == "Kabupaten Morowali"
    assert geocode_text("Banjir rendam Morowali")["name"] == "Kabupaten Morowali"
    assert geocode_text("Longsor di Kabupaten Bogor")["name"] == "Kabupaten Bogor"
    assert geocode_text("Kebakaran di Sulawesi Tengah")["level"] == "provinsi"
    assert geocode_text("Batu bara naik, batang pohon tumbang") is None  # kata umum tidak dianggap nama wilayah


def test_earthquake_feed_and_exposure(env, monkeypatch):
    from geoint import db
    from geoint.domains import common, disaster

    env.post("/api/aois", json={"name": "Morowali", "bbox": list(BBOX), "source": "demo"})
    fake = {"Infogempa": {"gempa": [{"DateTime": "2026-10-09T05:11:04+00:00", "Coordinates": "-2.70,122.30", "Magnitude": "6.4",
                                     "Kedalaman": "10 km", "Wilayah": "Uji Morowali", "Potensi": "Tidak berpotensi tsunami"}]}}
    monkeypatch.setattr(disaster, "http_json", lambda url, **kw: fake)
    assert disaster.fetch_bmkg() == 1
    assert disaster.fetch_bmkg() == 0  # dedup per sumber
    ev = common.query_events(kind="earthquake")[0]
    ex = disaster.earthquake_exposure(ev)
    assert ex["radius_km"]["VI"] > 20 and any(a["name"] == "Morowali" for a in ex["aois"])
    assert any(p["name"] == "Kabupaten Morowali" for p in ex["places"])
    assert disaster.alert_earthquakes("2026-01-01") == 1
    assert db.query("SELECT domain FROM alerts ORDER BY id DESC LIMIT 1")[0]["domain"] == "dis"
    assert disaster.radius_for_mmi(5.0, 6) < disaster.radius_for_mmi(7.0, 6)


def test_osint_ingest_and_fusion(env):
    from geoint.domains import common, osint

    items = [
        {"title": "Polisi gerebek tambang emas ilegal di Kabupaten Morowali", "link": "https://x/1", "desc": "PETI",
         "time": "2026-10-09T01:00:00+00:00", "publisher": "Uji"},
        {"title": "Harga cabai naik di pasar", "link": "https://x/2", "desc": "", "time": "2026-10-09T01:00:00+00:00", "publisher": "Uji"},
    ]
    assert osint.ingest(items, "rss") == 1  # berita tanpa topik pemantauan diabaikan
    ev = common.query_events(kind="news")[0]
    assert ev["properties"]["topics"] == ["tambang_ilegal"] and ev["properties"]["location"]["name"] == "Kabupaten Morowali"
    fx = osint.fuse(ev)
    assert any(l["type"] == "aoi" for l in fx["links"])


def test_mining_permit_compliance(env):
    from geoint import db

    aid = env.post("/api/analyses", json={"bbox": list(BBOX), "source": "demo", "before": BEFORE, "after": AFTER, "name": "Izin"}).json()["id"]
    a = _wait(env, aid)
    assert a["status"] == "done"
    mines = db.query("SELECT id, geometry, area_ha FROM detections WHERE analysis_id = ? AND class = 'tambang' ORDER BY area_ha DESC", (aid,))
    assert mines
    from shapely.geometry import mapping, shape

    big = shape(mines[0]["geometry"])
    permit = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": mapping(big.buffer(0.002)),
         "properties": {"NAMA_USAHA": "PT Uji Nikel", "NO_SK": "123", "STATUS": "Operasi Produksi", "TGL_AKHIR": "2035-01-01"}},
        # Izin lain di sudut AOI agar cakupan lapisan meliputi semua tambang demo
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[a["bbox"][0], a["bbox"][1]], [a["bbox"][0] + 0.001, a["bbox"][1]],
                                                                             [a["bbox"][0] + 0.001, a["bbox"][3]], [a["bbox"][0], a["bbox"][1]]]]},
         "properties": {"NAMA_USAHA": "PT Lama", "STATUS": "Berakhir"}},
    ]}
    r = env.post("/api/mne/layers/upload?name=IUP%20uji&filename=iup.geojson&kind=izin_tambang&access=terbatas",
                 content=json.dumps(permit).encode())
    assert r.status_code == 201, r.text
    from geoint.domains.mining import apply_compliance

    res = apply_compliance(aid)
    st = res["by_status"]
    assert st["dalam_izin"]["count"] >= 1
    assert st["di_luar_izin"]["count"] >= 1  # tambang lain di demo tidak punya izin
    comp = db.query("SELECT compliance FROM detections WHERE id = ?", (mines[0]["id"],))[0]["compliance"]
    assert comp["status"] == "dalam_izin" and comp["permits"][0]["label"].startswith("PT Uji Nikel")
    dash = env.get("/api/mne/dashboard").json()
    assert dash["by_status"]["di_luar_izin"]["area_ha"] > 0 and dash["permits"]
    alerts = env.get("/api/alerts").json()
    assert any("di luar izin" in al["title"] for al in alerts)


def test_access_token_hides_restricted(env, monkeypatch):
    import dataclasses

    import geoint.config
    import geoint.domains.common as common

    locked = dataclasses.replace(geoint.config.settings, access_token="rahasia")
    monkeypatch.setattr(common, "settings", locked)
    monkeypatch.setattr(geoint.config, "settings", locked)
    assert env.get("/api/access").json()["level"] == "publik"
    assert env.get("/api/mne/layers/geojson").json()["features"] == []
    assert env.post("/api/sea/ais/import", content=b"mmsi,timestamp,lat,lon\n").status_code == 403
    assert env.get("/api/mne/layers/geojson", headers={"X-Wira-Token": "rahasia"}).json()["features"]


def test_ais_import_and_dark_vessels(env):
    from datetime import datetime, timezone

    from geoint.domains import maritime

    csv = ("mmsi,timestamp,lat,lon,sog\n"
           "525000001,2026-10-04T11:10:00Z,-6.05,106.88,0.1\n"
           "525000002,2026-10-04T11:20:00Z,-6.06,106.90,5.0\n").encode()
    r = env.post("/api/sea/ais/import?source=uji", content=csv)
    assert r.json()["inserted"] == 2
    t = datetime(2026, 10, 4, 11, 14, tzinfo=timezone.utc)
    # Dua deteksi radar: satu cocok dengan AIS (<1 km), satu tidak -> 1 dark vessel.
    assert maritime._dark_count([106.881, 106.95], [-6.051, -6.02], t) == 1
    assert maritime._dark_count([106.881], [-6.051], datetime(2026, 1, 1, tzinfo=timezone.utc)) is None  # tanpa data AIS


def test_coastal_detection_synthetic():
    from geoint.classifier import classify_coastal
    from geoint.coastal import detect_coastal
    from geoint.sources.base import BANDS, Scene, terrain_from_dem

    n = 300
    land = {"blue": 0.03, "green": 0.06, "red": 0.04, "nir": 0.30, "swir1": 0.18, "swir2": 0.09}
    sea = {"blue": 0.06, "green": 0.07, "red": 0.04, "nir": 0.02, "swir1": 0.01, "swir2": 0.005}
    sand = {"blue": 0.18, "green": 0.20, "red": 0.22, "nir": 0.26, "swir1": 0.30, "swir2": 0.25}
    yy, xx = np.mgrid[0:n, 0:n]
    is_sea = xx >= 150

    def scene(mods):
        b = {k: np.where(is_sea, sea[k], land[k]).astype(np.float32) for k in BANDS}
        for mask, spec in mods:
            for k in BANDS:
                b[k] = np.where(mask, spec[k], b[k]).astype(np.float32)
        return Scene(b, Affine(20, 0, 500000, 0, -20, 9700000), "EPSG:32751", ("2020-01-01", "2020-06-01"))

    recl = (xx >= 150) & (xx < 175) & (yy > 40) & (yy < 80)              # urugan pasir ke laut, kotak
    ponds = np.zeros((n, n), bool)
    for i in range(3):
        for j in range(2):
            ponds |= (xx >= 100 + i * 14) & (xx < 110 + i * 14) & (yy >= 180 + j * 14) & (yy < 190 + j * 14)
    b, a = scene([]), scene([(recl, sand), (ponds, sea)])
    t = terrain_from_dem(np.full((n, n), 2.0, np.float32), (20, 20))
    patches, labels = detect_coastal(b, a, t, np.zeros((n, n), np.int32))
    classes = {classify_coastal(p.features)["class"] for p in patches}
    assert "reklamasi" in classes and "tambak" in classes


def test_domain_overview_and_map(env):
    doms = env.get("/api/domains").json()
    assert [d["id"] for d in doms] == ["sat", "dis", "sea", "mne", "inf", "os"]
    assert all(d["stats"] for d in doms)
    layers = env.get("/api/map/layers?layers=earthquakes,news,ports,aois,detections,permits").json()
    assert layers["ports"]["features"] and layers["earthquakes"]["features"] and layers["news"]["features"]
    src = env.get("/api/sources").json()
    ais = next(s for s in src if s["id"] == "aisstream")
    assert ais["access"] == "berlisensi" and not ais["available"]
    assert env.post("/api/sources/aisstream/run").status_code == 409
