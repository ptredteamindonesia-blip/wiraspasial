"""INF — jalan, bangunan, kawasan urban.

- Inventaris OpenStreetMap per area pantauan: panjang jalan per kelas, jembatan, jumlah bangunan,
  luas kawasan permukiman/industri/komersial (snapshot disimpan & dipakai ulang ≤ 7 hari).
- Dari analisa satelit: jalan baru (kelas jalan_akses) & pembangunan baru per run, serta jalan baru
  yang BELUM terpetakan di OSM (sering berupa jalan tambang/pembalakan).
- Fraksi lahan terbangun menurut ESA WorldCover (dari ringkasan analisa).
"""

from __future__ import annotations

import json
import logging
import math
import urllib.parse
from datetime import datetime, timedelta, timezone

from shapely.geometry import Polygon

from .. import db
from .common import haversine_km, http_get

log = logging.getLogger(__name__)
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
ROAD_GROUPS = {
    "arteri": {"motorway", "trunk", "primary", "motorway_link", "trunk_link", "primary_link"},
    "kolektor": {"secondary", "tertiary", "secondary_link", "tertiary_link"},
    "lokal": {"unclassified", "residential", "living_street", "service"},
    "jalan tanah/track": {"track", "path"},
}


def _overpass(query: str) -> list[dict]:
    last = None
    for url in OVERPASS:
        try:
            raw = http_get(url, timeout=120, retries=1, data=urllib.parse.urlencode({"data": query}).encode())
            return json.loads(raw)["elements"]
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Overpass: {last}")


def _line_km(geom: list[dict]) -> float:
    return sum(haversine_km(a["lon"], a["lat"], b["lon"], b["lat"]) for a, b in zip(geom[:-1], geom[1:]))


def _area_ha(geom: list[dict]) -> float:
    if len(geom) < 4:
        return 0.0
    lat0 = sum(p["lat"] for p in geom) / len(geom)
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_574
    poly = Polygon([(p["lon"] * kx, p["lat"] * ky) for p in geom])
    return abs(poly.area) / 10_000


def osm_inventory(bbox) -> dict:
    w, s, e, n = bbox
    b = f"{s},{w},{n},{e}"
    roads = _overpass(f'[out:json][timeout:90];way["highway"]({b});out tags geom;')
    landuse = _overpass(f'[out:json][timeout:90];way["landuse"~"^(residential|industrial|commercial|retail|construction)$"]({b});out tags geom;')
    buildings = _overpass(f'[out:json][timeout:90];way["building"]({b});out count;')
    by_group = {g: 0.0 for g in ROAD_GROUPS}
    by_group["lainnya"] = 0.0
    bridges = 0
    for r in roads:
        hw = r.get("tags", {}).get("highway")
        km = _line_km(r.get("geometry", []))
        grp = next((g for g, s_ in ROAD_GROUPS.items() if hw in s_), "lainnya")
        by_group[grp] += km
        if r.get("tags", {}).get("bridge") in ("yes", "viaduct"):
            bridges += 1
    lu: dict[str, float] = {}
    for l in landuse:
        k = l.get("tags", {}).get("landuse")
        lu[k] = lu.get(k, 0.0) + _area_ha(l.get("geometry", []))
    n_build = int(buildings[0]["tags"].get("ways", buildings[0]["tags"].get("total", 0))) if buildings else 0
    return {
        "roads_km": {k: round(v, 1) for k, v in by_group.items()},
        "roads_total_km": round(sum(by_group.values()), 1),
        "bridges": bridges,
        "buildings": n_build,
        "landuse_ha": {k: round(v, 1) for k, v in sorted(lu.items(), key=lambda kv: -kv[1])},
        "source": "OpenStreetMap (ODbL) — kelengkapan bergantung pada kontributor",
    }


def inventory_for_aoi(aoi: dict, refresh: bool = False) -> dict:
    snap = db.query("SELECT * FROM infra_snapshots WHERE aoi_id = ? ORDER BY id DESC LIMIT 1", (aoi["id"],))
    if snap and not refresh:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(snap[0]["created_at"])
        if age < timedelta(days=7):
            return {**json.loads(snap[0]["summary"]), "snapshot_at": snap[0]["created_at"], "cached": True}
    inv = osm_inventory(aoi["bbox"])
    db.insert("infra_snapshots", {"aoi_id": aoi["id"], "bbox": aoi["bbox"], "created_at": db.now_iso(), "summary": json.dumps(inv)})
    db.log_activity("infra_inventaris", f"Inventaris infrastruktur OSM diperbarui untuk '{aoi['name']}'", aoi_id=aoi["id"])
    return {**inv, "snapshot_at": db.now_iso(), "cached": False}


def change_metrics(aoi_id: int) -> dict:
    """Jalan & pembangunan baru per run pemantauan, serta jalan baru yang belum ada di OSM (run terbaru)."""
    runs = db.query("SELECT id, after_start, after_end, summary FROM analyses WHERE aoi_id = ? AND status = 'done' ORDER BY after_end, id",
                    (aoi_id,))
    series = []
    for r in runs:
        bc = (r["summary"] or {}).get("by_class", {})
        lc = (r["summary"] or {}).get("landcover_wc") or {}
        series.append({"analysis_id": r["id"], "after_end": r["after_end"],
                       "roads_ha": bc.get("jalan_akses", {}).get("area_ha", 0), "built_ha": bc.get("pembangunan", {}).get("area_ha", 0),
                       "built_frac_wc": lc.get("terbangun")})
    unmapped = []
    if runs:
        for d in db.query("SELECT id, analysis_id, area_ha, lon, lat, features FROM detections WHERE analysis_id = ? AND class = 'jalan_akses'",
                          (runs[-1]["id"],)):
            f = d["features"] or {}
            frac = f.get("osm_road_frac")
            if frac is None or frac < 0.2:
                unmapped.append({"id": d["id"], "analysis_id": d["analysis_id"], "lon": d["lon"], "lat": d["lat"],
                                 "length_km": round((f.get("length_m") or 0) / 1000, 2), "area_ha": d["area_ha"],
                                 "osm_checked": frac is not None})
    return {"series": series, "unmapped_roads": sorted(unmapped, key=lambda x: -x["length_km"]),
            "unmapped_km": round(sum(u["length_km"] for u in unmapped if u["osm_checked"]), 2)}


def national_overview() -> list[dict]:
    out = []
    for a in db.query("SELECT id, name, bbox FROM aois ORDER BY name"):
        snap = db.query("SELECT summary, created_at FROM infra_snapshots WHERE aoi_id = ? ORDER BY id DESC LIMIT 1", (a["id"],))
        cm = change_metrics(a["id"])
        last = cm["series"][-1] if cm["series"] else {}
        inv = json.loads(snap[0]["summary"]) if snap else None
        out.append({"id": a["id"], "name": a["name"], "roads_total_km": inv and inv["roads_total_km"],
                    "buildings": inv and inv["buildings"], "snapshot_at": snap[0]["created_at"] if snap else None,
                    "new_roads_ha": last.get("roads_ha"), "new_built_ha": last.get("built_ha"),
                    "built_frac_wc": last.get("built_frac_wc"), "unmapped_km": cm["unmapped_km"]})
    return out
