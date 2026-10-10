"""MNE — perubahan lahan tambang + lapisan izin.

Lapisan izin/kawasan diimpor pengguna (GeoJSON, Shapefile ZIP, atau ArcGIS REST FeatureServer)
karena data izin (IUP/WIUP/PKP2B, kawasan hutan, kawasan lindung) memiliki pemilik & hak akses
masing-masing. Setiap deteksi tambang dari analisa satelit diuji terhadap lapisan tersebut:

  dalam_izin        ≥ 50% luas berada di izin yang berstatus aktif/tidak diketahui
  izin_tidak_aktif  berada di izin yang tercatat berakhir/dicabut
  sebagian          10–50% di dalam izin
  di_luar_izin      < 10% di dalam izin manapun (indikasi PETI — wajib verifikasi lapangan)
  tanpa_data_izin   tidak ada lapisan izin yang mencakup lokasi ini (BUKAN berarti ilegal)
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import zipfile
from datetime import date, datetime, timezone

from shapely.geometry import Point, box, mapping, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .. import db
from .common import ACCESS_LEVELS, http_json, make_alert

log = logging.getLogger(__name__)

LAYER_KINDS = {
    "izin_tambang": "Izin usaha pertambangan (IUP/WIUP/PKP2B/IPR)",
    "kawasan_hutan": "Kawasan hutan (produksi/lindung/konservasi)",
    "kawasan_lindung": "Kawasan lindung / konservasi",
    "lainnya": "Lapisan lain",
}
MINING_CLASSES = {"tambang", "jalan_akses"}
STATUS_LABELS = {
    "dalam_izin": "Dalam izin",
    "sebagian": "Sebagian dalam izin",
    "izin_tidak_aktif": "Izin tidak aktif",
    "di_luar_izin": "Di luar izin terdaftar",
    "tanpa_data_izin": "Tidak ada data izin",
}
INACTIVE_RE = re.compile(r"tidak\s*aktif|non\s*aktif|berakhir|habis|dicabut|expired|inactive|terminated|cabut", re.I)
ACTIVE_RE = re.compile(r"\baktif\b|berlaku|active|operasi produksi|eksplorasi|produksi", re.I)
NAME_KEYS = ("nama_usaha", "nama_perusahaan", "nm_perusahaan", "perusahaan", "pemegang", "company", "nama", "name", "namobj")
NUMBER_KEYS = ("no_sk", "nomor_sk", "sk", "no_izin", "nomor", "kode_wiup", "id_izin")
END_KEYS = ("akhir", "berakhir", "exp", "end", "sampai", "tgl_akhir", "valid_to")

_cache_lock = threading.Lock()
_cache: dict | None = None  # {"tree": STRtree, "items": [...]}


# --------------------------------------------------------------------------- utilitas atribut
def _get(props: dict, keys) -> str | None:
    low = {k.lower(): v for k, v in props.items()}
    for k in keys:
        if low.get(k) not in (None, ""):
            return str(low[k])
    return None


def feature_label(props: dict) -> str:
    name = _get(props, NAME_KEYS) or "Tanpa nama"
    num = _get(props, NUMBER_KEYS)
    return f"{name} ({num})" if num else name


def _parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and v > 10**11:  # epoch ms (ArcGIS)
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date()
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def permit_state(props: dict) -> str:
    """aktif | tidak_aktif | tidak_diketahui — dari kolom status atau tanggal berakhir."""
    for k, v in props.items():
        kl = k.lower()
        if any(e in kl for e in END_KEYS):
            d = _parse_date(v)
            if d:
                return "tidak_aktif" if d < date.today() else "aktif"
    for k, v in props.items():
        if k.lower() in ("status", "sts", "keterangan", "ket", "tahap", "tahapan", "status_izin") and isinstance(v, str):
            if INACTIVE_RE.search(v):
                return "tidak_aktif"
            if ACTIVE_RE.search(v):
                return "aktif"
    return "tidak_diketahui"


# --------------------------------------------------------------------------- impor
def _read_shapefile_zip(content: bytes) -> list[dict]:
    import shapefile
    from rasterio.crs import CRS
    from rasterio.warp import transform_geom

    zf = zipfile.ZipFile(io.BytesIO(content))
    shp = next((n for n in zf.namelist() if n.lower().endswith(".shp")), None)
    if not shp:
        raise ValueError("ZIP tidak berisi file .shp")
    base = shp[:-4]
    names = {n.lower(): n for n in zf.namelist()}

    def part(ext):
        n = names.get((base + ext).lower())
        return io.BytesIO(zf.read(n)) if n else None

    reader = shapefile.Reader(shp=part(".shp"), shx=part(".shx"), dbf=part(".dbf"), encoding="latin-1")
    prj = part(".prj")
    src_crs = CRS.from_wkt(prj.read().decode("latin-1")) if prj else None
    feats = []
    for sr in reader.iterShapeRecords():
        geom = sr.shape.__geo_interface__
        if src_crs and not src_crs.to_epsg() == 4326:
            geom = transform_geom(src_crs, "EPSG:4326", geom)
        feats.append({"type": "Feature", "geometry": geom, "properties": sr.record.as_dict()})
    return feats


def parse_upload(filename: str, content: bytes) -> list[dict]:
    fn = filename.lower()
    if fn.endswith(".zip"):
        return _read_shapefile_zip(content)
    if fn.endswith((".geojson", ".json")):
        data = json.loads(content.decode("utf-8-sig"))
        if data.get("type") == "FeatureCollection":
            return data["features"]
        if data.get("type") == "Feature":
            return [data]
        return [{"type": "Feature", "geometry": data, "properties": {}}]
    raise ValueError("Format tidak didukung: gunakan .geojson/.json atau Shapefile dalam .zip")


def fetch_arcgis(url: str, bbox=None, max_features: int = 20000) -> list[dict]:
    """Ambil fitur dari ArcGIS REST FeatureServer/MapServer layer (…/FeatureServer/0) dengan paginasi."""
    base = url.rstrip("/")
    if not base.endswith("/query"):
        base += "/query"
    feats, offset = [], 0
    while len(feats) < max_features:
        params = f"where=1%3D1&outFields=*&outSR=4326&f=geojson&resultOffset={offset}&resultRecordCount=1000"
        if bbox:
            params += (f"&geometry={bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}&geometryType=esriGeometryEnvelope"
                       f"&inSR=4326&spatialRel=esriSpatialRelIntersects")
        data = http_json(f"{base}?{params}", timeout=120)
        if "error" in data:
            raise RuntimeError(f"ArcGIS: {data['error'].get('message')}")
        batch = data.get("features", [])
        feats.extend(batch)
        if len(batch) < 1000 or not data.get("exceededTransferLimit", len(batch) == 1000):
            break
        offset += len(batch)
    return feats


def save_layer(name: str, kind: str, access: str, features: list[dict], source: str, license: str = "",
               quality: str = "sesuai sumber", layer_id: int | None = None) -> int:
    if kind not in LAYER_KINDS:
        raise ValueError(f"Jenis lapisan tidak dikenal: {kind}")
    if access not in ACCESS_LEVELS:
        raise ValueError(f"Tingkat akses tidak dikenal: {access}")
    rows, bounds = [], None
    for f in features:
        if not f.get("geometry"):
            continue
        g = shape(f["geometry"])
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        b = g.bounds
        bounds = b if bounds is None else (min(bounds[0], b[0]), min(bounds[1], b[1]), max(bounds[2], b[2]), max(bounds[3], b[3]))
        props = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in (f.get("properties") or {}).items()}
        rows.append((json.dumps(mapping(g)), json.dumps(props, ensure_ascii=False, default=str), *b))
    if not rows:
        raise ValueError("Tidak ada fitur geometri yang valid")
    now = db.now_iso()
    if layer_id is None:
        layer_id = db.insert("layers", {"name": name, "kind": kind, "domain": "mne", "access": access, "source": source,
                                        "license": license, "quality": quality, "feature_count": len(rows),
                                        "bbox": list(bounds), "created_at": now, "updated_at": now})
    else:
        db.execute("DELETE FROM layer_features WHERE layer_id = ?", (layer_id,))
        db.update("layers", layer_id, {"feature_count": len(rows), "bbox": list(bounds), "updated_at": now})
    with db.connect() as c:
        c.executemany("INSERT INTO layer_features (layer_id, geometry, properties, minx, miny, maxx, maxy) VALUES (?,?,?,?,?,?,?)",
                      [(layer_id, *r) for r in rows])
    invalidate_cache()
    db.log_activity("lapisan_diimpor", f"Lapisan '{name}' ({LAYER_KINDS[kind]}, akses {access}) diimpor: {len(rows)} fitur")
    return layer_id


def delete_layer(layer_id: int) -> None:
    lay = db.get("layers", layer_id)
    db.execute("DELETE FROM layers WHERE id = ?", (layer_id,))
    invalidate_cache()
    if lay:
        db.log_activity("lapisan_dihapus", f"Lapisan '{lay['name']}' dihapus")


# --------------------------------------------------------------------------- indeks spasial
def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _index():
    global _cache
    with _cache_lock:
        if _cache is None:
            items = []
            for lay in db.query("SELECT id, name, kind, access FROM layers"):
                for f in db.query("SELECT id, geometry, properties FROM layer_features WHERE layer_id = ?", (lay["id"],)):
                    g = shape(f["geometry"])
                    items.append({"geom": g, "feature_id": f["id"], "layer_id": lay["id"], "layer_name": lay["name"],
                                  "kind": lay["kind"], "access": lay["access"], "props": f["properties"],
                                  "label": feature_label(f["properties"]), "state": permit_state(f["properties"])})
            _cache = {"items": items, "tree": STRtree([i["geom"] for i in items]) if items else None}
        return _cache


def features_containing(lon: float, lat: float, kinds=None) -> list[dict]:
    idx = _index()
    if not idx["tree"]:
        return []
    pt = Point(lon, lat)
    out = []
    for i in idx["tree"].query(pt, predicate="intersects"):
        it = idx["items"][int(i)]
        if kinds is None or it["kind"] in kinds:
            out.append(it)
    return out


def features_intersecting(geom, kinds=None) -> list[dict]:
    idx = _index()
    if not idx["tree"]:
        return []
    return [idx["items"][int(i)] for i in idx["tree"].query(geom, predicate="intersects")
            if kinds is None or idx["items"][int(i)]["kind"] in kinds]


def layer_covers(geom, kind: str = "izin_tambang") -> bool:
    """Apakah ada lapisan berjenis `kind` yang cakupannya (bbox) meliputi lokasi ini."""
    for lay in db.query("SELECT bbox FROM layers WHERE kind = ?", (kind,)):
        if lay["bbox"] and box(*lay["bbox"]).intersects(geom):
            return True
    return False


# --------------------------------------------------------------------------- kepatuhan
def evaluate_detection(det: dict) -> dict:
    g = shape(det["geometry"])
    if not g.is_valid:
        g = g.buffer(0)
    area = g.area or 1e-12
    hits = features_intersecting(g)
    permits, zones = [], []
    for h in hits:
        ov = g.intersection(h["geom"]).area / area
        if ov <= 0.001:
            continue
        rec = {"layer": h["layer_name"], "layer_id": h["layer_id"], "feature_id": h["feature_id"], "label": h["label"],
               "overlap": round(ov, 3), "access": h["access"]}
        if h["kind"] == "izin_tambang":
            permits.append({**rec, "state": h["state"]})
        else:
            zones.append({**rec, "kind": h["kind"]})
    out = {"permits": sorted(permits, key=lambda x: -x["overlap"]), "zones": sorted(zones, key=lambda x: -x["overlap"])}
    if det["class"] in MINING_CLASSES:
        covered = layer_covers(g)
        active = [p for p in permits if p["state"] != "tidak_aktif"]
        inside_active = min(1.0, unary_union([h["geom"] for h in hits if h["kind"] == "izin_tambang"
                                               and h["state"] != "tidak_aktif"]).intersection(g).area / area) if active else 0.0
        inside_any = sum(p["overlap"] for p in permits)
        if not covered:
            status = "tanpa_data_izin"
        elif inside_active >= 0.5:
            status = "dalam_izin"
        elif permits and inside_any >= 0.5:
            status = "izin_tidak_aktif"
        elif inside_any >= 0.1:
            status = "sebagian"
        else:
            status = "di_luar_izin"
        out.update({"status": status, "status_label": STATUS_LABELS[status], "inside_active": round(inside_active, 3)})
    return out


def apply_compliance(analysis_id: int, raise_alerts: bool = True, update_summary: bool = True) -> dict:
    """Hitung status izin untuk semua deteksi sebuah analisa, simpan, perbarui ringkasan & alert."""
    dets = db.query("SELECT id, class, area_ha, geometry FROM detections WHERE analysis_id = ?", (analysis_id,))
    totals = {k: {"label": v, "count": 0, "area_ha": 0.0} for k, v in STATUS_LABELS.items()}
    zone_ha: dict[str, float] = {}
    with db.connect() as c:
        for d in dets:
            comp = evaluate_detection(d)
            c.execute("UPDATE detections SET compliance = ? WHERE id = ?", (json.dumps(comp, ensure_ascii=False), d["id"]))
            if "status" in comp:
                totals[comp["status"]]["count"] += 1
                totals[comp["status"]]["area_ha"] += d["area_ha"]
            for z in comp["zones"]:
                if d["class"] in ("tambang", "penebangan", "jalan_akses", "pembangunan", "kebakaran"):
                    key = f"{LAYER_KINDS.get(z['kind'], z['kind'])}: {z['layer']}"
                    zone_ha[key] = zone_ha.get(key, 0) + d["area_ha"] * z["overlap"]
    for v in totals.values():
        v["area_ha"] = round(v["area_ha"], 2)
    result = {"by_status": totals, "human_in_zones_ha": {k: round(v, 2) for k, v in zone_ha.items()},
              "layers": db.query("SELECT COUNT(*) AS n FROM layers")[0]["n"], "computed_at": db.now_iso()}
    a = db.get("analyses", analysis_id)
    # Pipeline memanggil dengan update_summary=False lalu menyimpan ringkasan sekali bersama status "done",
    # agar pembaca tidak pernah melihat analisa selesai dengan ringkasan setengah jadi.
    if a and update_summary and a.get("summary") is not None:
        summary = a["summary"]
        summary["mining_compliance"] = result
        db.update("analyses", analysis_id, {"summary": summary})
    if a and raise_alerts:
        compliance_alerts(a, result)
    return result


def compliance_alerts(a: dict, result: dict) -> None:
    analysis_id, totals = a["id"], result["by_status"]
    out_ha = totals["di_luar_izin"]["area_ha"]
    if out_ha >= 1:
        make_alert("tinggi", f"[{a['name']}] {out_ha:.1f} ha tambang di luar izin terdaftar",
                   f"{totals['di_luar_izin']['count']} area tambang/jalan tambang tidak berada di izin pada lapisan yang diimpor. "
                   "Indikasi PETI — wajib verifikasi lapangan.", "mne", aoi_id=a["aoi_id"], analysis_id=analysis_id,
                   dedup_key=f"mne_out:{analysis_id}:{round(out_ha)}")
    inactive = totals["izin_tidak_aktif"]["area_ha"]
    if inactive >= 1:
        make_alert("sedang", f"[{a['name']}] {inactive:.1f} ha aktivitas tambang di izin tidak aktif",
                   "Izin tercatat berakhir/dicabut pada lapisan izin.", "mne", aoi_id=a["aoi_id"],
                   analysis_id=analysis_id, dedup_key=f"mne_inactive:{analysis_id}:{round(inactive)}")
    for k, ha in result["human_in_zones_ha"].items():
        if ha >= 1 and "lindung" in k.lower():
            make_alert("tinggi", f"[{a['name']}] {ha:.1f} ha aktivitas manusia di {k}", "Periksa izin & lakukan verifikasi.",
                       "mne", aoi_id=a["aoi_id"], analysis_id=analysis_id, dedup_key=f"zone:{analysis_id}:{k}")


def recompute_all() -> int:
    ids = [r["id"] for r in db.query("SELECT id FROM analyses WHERE status = 'done'")]
    for i in ids:
        apply_compliance(i, raise_alerts=False)
    return len(ids)


def dashboard(level: int) -> dict:
    """Ringkasan nasional dari analisa selesai terbaru per area (plus analisa tanpa area)."""
    latest = db.query(
        "SELECT a.id, a.name, a.aoi_id FROM analyses a WHERE a.status='done' AND (a.aoi_id IS NULL OR a.id = "
        "(SELECT MAX(b.id) FROM analyses b WHERE b.aoi_id = a.aoi_id AND b.status='done'))"
    )
    ids = [a["id"] for a in latest]
    names = {a["id"]: a["name"] for a in latest}
    totals = {k: {"label": v, "count": 0, "area_ha": 0.0} for k, v in STATUS_LABELS.items()}
    outside, per_permit = [], {}
    if ids:
        rows = db.query(
            f"SELECT id, analysis_id, class, area_ha, lon, lat, compliance, confidence FROM detections "
            f"WHERE analysis_id IN ({','.join('?' * len(ids))}) AND class IN ('tambang','jalan_akses')", tuple(ids))
        for r in rows:
            comp = r["compliance"] or {}
            st = comp.get("status", "tanpa_data_izin")
            totals[st]["count"] += 1
            totals[st]["area_ha"] += r["area_ha"]
            if st in ("di_luar_izin", "izin_tidak_aktif"):
                outside.append({**{k: r[k] for k in ("id", "analysis_id", "class", "area_ha", "lon", "lat", "confidence")},
                                "status": st, "analysis": names.get(r["analysis_id"])})
            for p in comp.get("permits", []):
                if ACCESS_LEVELS.get(p.get("access", "terbatas"), 2) > level:
                    continue
                key = (p["layer"], p["label"])
                rec = per_permit.setdefault(key, {"layer": p["layer"], "label": p["label"], "state": p["state"], "area_ha": 0.0})
                rec["area_ha"] += r["area_ha"] * p["overlap"]
    for v in totals.values():
        v["area_ha"] = round(v["area_ha"], 2)
    return {
        "analyses": len(ids), "by_status": totals,
        "outside": sorted(outside, key=lambda x: -x["area_ha"])[:100],
        "permits": sorted(({**v, "area_ha": round(v["area_ha"], 2)} for v in per_permit.values()), key=lambda x: -x["area_ha"])[:50],
    }


def layers_geojson(level: int, bbox=None, layer_id: int | None = None, limit: int = 5000) -> dict:
    lays = {l["id"]: l for l in db.query("SELECT * FROM layers") if ACCESS_LEVELS.get(l["access"], 2) <= level}
    if layer_id is not None:
        lays = {k: v for k, v in lays.items() if k == layer_id}
    if not lays:
        return {"type": "FeatureCollection", "features": []}
    where = f"layer_id IN ({','.join('?' * len(lays))})"
    params: list = list(lays)
    if bbox:
        where += " AND maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?"
        params += [bbox[0], bbox[2], bbox[1], bbox[3]]
    rows = db.query(f"SELECT id, layer_id, geometry, properties FROM layer_features WHERE {where} LIMIT ?", (*params, limit))
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": r["id"], "geometry": r["geometry"],
         "properties": {"layer_id": r["layer_id"], "layer": lays[r["layer_id"]]["name"], "kind": lays[r["layer_id"]]["kind"],
                        "label": feature_label(r["properties"]), "state": permit_state(r["properties"]), "attrs": r["properties"]}}
        for r in rows]}
