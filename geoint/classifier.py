"""Klasifikasi penyebab perubahan: alami vs aktivitas manusia.

Pendekatan berbasis pengetahuan (rule-based, fuzzy scoring) agar setiap keputusan dapat
dijelaskan. Setiap kelas punya sejumlah komponen bukti; skor kelas = jumlah berbobot
derajat keanggotaan tiap bukti. Skor dinormalisasi menjadi probabilitas (softmax),
lalu dikoreksi dengan konteks spasial antar-patch (mis. longsor di sebelah jalan tambang).

Kelas:
  tambang       — tambang terbuka (batu bara, nikel, emas): tanah terbuka terang, kolam air
                  keruh, tanah laterit kemerahan, mengelompok.
  penebangan    — penebangan / pembukaan lahan (HTI, sawit, food estate): bentuk geometris
                  teratur, sisa vegetasi, berderet.
  jalan_akses   — jalan angkut / jalan sarad: memanjang dan sempit.
  pembangunan   — perluasan permukiman / kawasan industri / smelter: permukaan heterogen
                  (atap, beton), warna abu/kebiruan (bukan tanah laterit), datar, tumbuh dari
                  lahan terbangun yang sudah ada.
  kebakaran     — area terbakar: dNBR tinggi, permukaan gelap (arang), titik panas FIRMS.
  longsor       — tanah longsor: lereng terjal, memanjang searah turunan lereng.
  banjir        — genangan/banjir: air menutupi area, dataran rendah, dekat sungai.
  alami_lain    — degradasi/stres vegetasi alami (kekeringan, fenologi, celah kanopi).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CLASSES = {
    "tambang": {"label": "Penambangan", "nature": "manusia", "color": "#d7301f"},
    "penebangan": {"label": "Penebangan / pembukaan lahan", "nature": "manusia", "color": "#f28e2b"},
    "jalan_akses": {"label": "Jalan akses / jalan angkut", "nature": "manusia", "color": "#b07aa1"},
    "pembangunan": {"label": "Pembangunan / permukiman / industri", "nature": "manusia", "color": "#76718a"},
    "kebakaran": {"label": "Kebakaran", "nature": "campuran", "color": "#5a3d2b"},
    "longsor": {"label": "Tanah longsor", "nature": "alami", "color": "#4e79a7"},
    "banjir": {"label": "Banjir / genangan", "nature": "alami", "color": "#2fb3c9"},
    "alami_lain": {"label": "Degradasi / stres vegetasi alami", "nature": "alami", "color": "#59a14f"},
    # Pesisir
    "tambak": {"label": "Tambak / kolam budidaya", "nature": "manusia", "color": "#bcbd22"},
    "reklamasi": {"label": "Reklamasi / pengurukan", "nature": "manusia", "color": "#e7298a"},
    "abrasi": {"label": "Abrasi pantai", "nature": "alami", "color": "#1f5f8b"},
    "akresi": {"label": "Akresi / sedimentasi pantai", "nature": "alami", "color": "#8c6d31"},
}
COASTAL_CLASSES = {"tambak", "reklamasi", "abrasi", "akresi"}

CATEGORIES = {
    "manusia": "Aktivitas manusia",
    "alami": "Proses alami",
    "perlu_verifikasi": "Perlu verifikasi lapangan",
}

HUMAN_CLASSES = {k for k, v in CLASSES.items() if v["nature"] == "manusia"}
# Prior: mayoritas kebakaran hutan & lahan di Indonesia terkait pembukaan lahan, namun
# tanpa bukti kontekstual kami tidak memvonis — nilai 0.5 membuat kebakaran tanpa konteks
# masuk kategori "perlu verifikasi".
FIRE_HUMAN_PRIOR = 0.5
SOFTMAX_TEMPERATURE = 0.12


def ramp(x: float, a: float, b: float) -> float:
    """Keanggotaan fuzzy linier: 0 di a, 1 di b (a > b berarti menurun). NaN -> 0."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if a == b:
        return float(x >= a)
    return float(min(1.0, max(0.0, (x - a) / (b - a))))


@dataclass
class Evidence:
    weight: float
    value: float
    text: str


def _fmt(v: float, nd: int = 2) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}"


def _evidence(f: dict) -> dict[str, list[Evidence]]:
    g = f.get
    water_frac = g("water_frac", 0.0)
    n_nb = g("n_neighbors_1500m", 0)
    rect = g("rectangularity", 0.0)

    ev: dict[str, list[Evidence]] = {}
    ev["tambang"] = [
        Evidence(0.25, ramp(g("bsi_after"), 0.05, 0.20), f"Tanah terbuka kuat (BSI {_fmt(g('bsi_after'))})"),
        Evidence(0.15, ramp(g("albedo_after"), 0.07, 0.11), f"Permukaan terang khas galian (albedo {_fmt(g('albedo_after'), 3)})"),
        Evidence(0.15, ramp(g("ndvi_after"), 0.35, 0.15), f"Vegetasi hilang hampir total (NDVI sesudah {_fmt(g('ndvi_after'))})"),
        Evidence(
            0.20,
            ramp(water_frac, 0.02, 0.10) * ramp(g("ndti_water"), -0.20, 0.0),
            f"Kolam air keruh bersedimen {water_frac * 100:.0f}% area (NDTI {_fmt(g('ndti_water'))}) — ciri void/settling pond tambang",
        ),
        Evidence(0.10, ramp(g("ferric_after"), 1.9, 2.4), f"Tanah laterit kaya besi (rasio merah/biru {_fmt(g('ferric_after'))})"),
        Evidence(
            0.15,
            max(ramp(n_nb, 0, 3), ramp(g("area_ha", 0), 5, 30)),
            f"Bukaan luas/mengelompok ({g('area_ha', 0):.1f} ha, {n_nb} patch lain dalam 1,5 km)",
        ),
    ]
    ring_nf = g("ring_nonforest", 0.0)
    ev["penebangan"] = [
        Evidence(0.30, ramp(rect, 0.80, 0.93), f"Bentuk geometris teratur/bersudut (rektangularitas {_fmt(rect)})"),
        Evidence(
            0.20,
            ramp(g("ndvi_after"), 0.15, 0.30) * ramp(g("ndvi_after"), 0.68, 0.52),
            f"Sisa vegetasi/semak setelah tebang (NDVI sesudah {_fmt(g('ndvi_after'))})",
        ),
        Evidence(0.15, ramp(n_nb, 1, 4), f"Pola blok berderet ({n_nb} patch lain dalam 1,5 km)"),
        Evidence(0.15, ramp(ring_nf, 0.5, 0.2), f"Berada di dalam kawasan hutan ({(1 - ring_nf) * 100:.0f}% sekeliling masih hutan)"),
        Evidence(0.10, ramp(g("bsi_after"), 0.20, 0.05), "Bukan galian tanah dalam (BSI rendah-sedang)"),
        Evidence(0.10, ramp(g("albedo_after"), 0.04, 0.06), "Tidak ada jejak arang/bakar"),
    ]
    ev["pembangunan"] = [
        Evidence(0.25, ramp(ring_nf, 0.25, 0.6), f"Tumbuh dari lahan terbangun/non-hutan yang sudah ada ({ring_nf * 100:.0f}% sekeliling)"),
        Evidence(0.20, ramp(g("albedo_cv"), 0.15, 0.30), f"Permukaan heterogen khas atap/beton/jalan (CV albedo {_fmt(g('albedo_cv'))})"),
        Evidence(0.20, ramp(g("ferric_after"), 1.6, 1.3), f"Warna abu/kebiruan material bangunan, bukan tanah laterit (rasio merah/biru {_fmt(g('ferric_after'))})"),
        Evidence(0.15, ramp(g("slope_mean"), 10, 4), f"Topografi datar ({_fmt(g('slope_mean'), 1)}°)"),
        Evidence(0.10, ramp(g("albedo_after"), 0.08, 0.12), "Permukaan terang"),
        Evidence(0.10, ramp(g("bsi_after"), 0.15, 0.0), "Bukan galian tanah"),
    ]
    thin = ramp(g("width_m", 999), 60, 30)
    ev["jalan_akses"] = [
        Evidence(0.55, thin, f"Sempit memanjang (lebar efektif ~{g('width_m', 0):.0f} m)"),
        Evidence(0.30, thin * ramp(g("length_m", 0), 200, 600), f"Panjang ~{g('length_m', 0) / 1000:.1f} km"),
        Evidence(0.15, thin * ramp(n_nb, 0, 3), "Menghubungkan area bukaan lain"),
    ]
    ev["kebakaran"] = [
        Evidence(0.30, ramp(g("dnbr"), 0.25, 0.60), f"dNBR tinggi ({_fmt(g('dnbr'))}) — indikasi kuat terbakar"),
        Evidence(0.20, ramp(g("nbr_after"), 0.05, -0.12), f"NBR sesudah negatif ({_fmt(g('nbr_after'))})"),
        Evidence(0.30, ramp(g("albedo_after"), 0.08, 0.05), f"Permukaan gelap/arang (albedo {_fmt(g('albedo_after'), 3)})"),
        Evidence(
            0.20,
            max(ramp(g("hotspots", 0), 0, 2), 0.5 * ramp(g("ndvi_after"), 0.15, 0.35)),
            f"{g('hotspots', 0)} titik panas FIRMS di dalam area" if g("hotspots", 0) else "Sisa batang/vegetasi hangus",
        ),
    ]
    ev["longsor"] = [
        Evidence(0.35, ramp(g("slope_mean"), 12, 25), f"Lereng terjal (rata-rata {_fmt(g('slope_mean'), 1)}°)"),
        Evidence(0.20, ramp(g("downslope_alignment"), 0.5, 0.9), f"Memanjang searah turunan lereng (keselarasan {_fmt(g('downslope_alignment'))})"),
        Evidence(0.15, ramp(g("elongation"), 1.8, 4.0), f"Bentuk memanjang (elongasi {_fmt(g('elongation'), 1)})"),
        Evidence(0.20, ramp(g("bsi_after"), 0.0, 0.15), "Material tanah/batuan tersingkap"),
        Evidence(0.10, ramp(n_nb, 3, 0), "Terisolasi, bukan pola blok"),
    ]
    flood_gate = ramp(water_frac, 0.05, 0.25)
    ev["banjir"] = [
        Evidence(0.45, ramp(water_frac, 0.25, 0.60), f"Tergenang air {water_frac * 100:.0f}% area"),
        Evidence(0.15, flood_gate * ramp(g("rel_elev"), 0.40, 0.15), f"Dataran rendah (persentil elevasi {g('rel_elev', 0) * 100:.0f}%)"),
        Evidence(0.20, flood_gate * ramp(g("dist_water_m"), 400, 60), f"Dekat sungai/badan air ({g('dist_water_m', 0):.0f} m)"),
        Evidence(0.10, flood_gate * ramp(g("slope_mean"), 8, 3), "Topografi datar"),
        Evidence(0.10, flood_gate * ramp(g("bsi_after"), 0.10, -0.10), "Tanpa galian tanah"),
    ]
    ev["alami_lain"] = [
        Evidence(0.35, ramp(g("ndvi_after"), 0.45, 0.60), f"Vegetasi masih ada (NDVI sesudah {_fmt(g('ndvi_after'))})"),
        Evidence(0.25, ramp(g("dndvi"), -0.45, -0.25), f"Penurunan NDVI ringan ({_fmt(g('dndvi'))})"),
        Evidence(0.25, ramp(rect, 0.85, 0.60), "Bentuk tidak beraturan (tidak geometris)"),
        Evidence(0.15, ramp(g("bsi_after"), 0.05, -0.05), "Tidak ada tanah terbuka"),
    ]
    ev["tambak"] = [
        Evidence(0.30, ramp(water_frac, 0.30, 0.70), f"Berubah menjadi kolam air ({water_frac * 100:.0f}% area)"),
        Evidence(0.25, ramp(rect, 0.70, 0.90), f"Petak geometris (rektangularitas {_fmt(rect)})"),
        Evidence(0.15, ramp(g("elev_mean"), 15, 3), f"Dataran pesisir rendah ({_fmt(g('elev_mean'), 0)} m)"),
        Evidence(0.15, ramp(g("wc_mangrove_frac"), 0.2, 0.6), "Bekas mangrove (ESA WorldCover)"),
        Evidence(0.15, ramp(g("new_water_frac"), 0.2, 0.6), "Genangan baru di lokasi yang tak pernah tergenang (JRC)"),
    ]
    _add_ancillary_evidence(ev, f)
    return ev


def _abs_small(x, lo: float, hi: float) -> float:
    """1 bila |x| <= lo, 0 bila |x| >= hi (x None -> 0)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return ramp(abs(x), hi, lo)


def _add_ancillary_evidence(ev: dict[str, list[Evidence]], f: dict) -> None:
    """Bukti dari sumber data pendukung (Sentinel-1, WorldCover, JRC GSW, OSM).

    Bila sumber tidak tersedia, nilainya None -> keanggotaan 0 untuk semua kelas, sehingga
    perbandingan antar kelas tetap adil.
    """
    g = f.get
    dvh = g("dvh_db")
    water_gate = ramp(g("water_frac", 0.0), 0.05, 0.25)
    thin = ramp(g("width_m", 999), 60, 30)
    road = g("road_dist_m")
    radar_drop = f"Radar Sentinel-1: struktur tegakan hilang (VH {_fmt(dvh, 1)} dB)"

    ev["tambang"] += [
        Evidence(0.20, ramp(g("osm_mining_frac"), 0.05, 0.40), "Berada di area tambang/quarry yang terpetakan di OpenStreetMap"),
        Evidence(
            0.10,
            max(ramp(g("osm_mining_ring"), 0.03, 0.25), ramp(g("wc_bare_ring"), 0.05, 0.30)),
            "Bersebelahan dengan area galian/tanah terbuka yang sudah ada (OSM/WorldCover) — perluasan tambang",
        ),
        Evidence(
            0.10,
            ramp(g("new_water_frac"), 0.02, 0.10),
            f"Genangan baru {(g('new_water_frac') or 0) * 100:.0f}% area di lokasi yang hampir tak pernah tergenang (JRC 1984–2021) — ciri kolam galian",
        ),
        Evidence(0.05, ramp(dvh, -1.5, -3.5), radar_drop),
    ]
    ev["penebangan"] += [
        Evidence(0.10, ramp(dvh, -1.0, -2.5), radar_drop),
        Evidence(0.05, ramp(road, 1500, 300), f"Dekat jalan ({road or 0:.0f} m, OSM) — akses angkut kayu"),
    ]
    ev["pembangunan"] += [
        Evidence(
            0.15,
            max(ramp(g("osm_built_frac"), 0.05, 0.40), ramp(g("osm_built_ring"), 0.05, 0.30), ramp(g("wc_built_ring"), 0.05, 0.30)),
            "Di dalam/bersebelahan kawasan permukiman/industri yang terpetakan (OSM/WorldCover)",
        ),
        Evidence(0.10, ramp(dvh, 0.0, 2.0), f"Radar Sentinel-1: pantulan meningkat (VH +{max(dvh or 0, 0):.1f} dB) — efek pantulan ganda bangunan"),
        Evidence(0.05, ramp(road, 1000, 100), f"Terhubung jaringan jalan ({road or 0:.0f} m, OSM)"),
    ]
    ev["jalan_akses"] += [
        Evidence(0.15, thin * ramp(g("osm_road_frac"), 0.10, 0.50), "Bertepatan dengan jalan yang terpetakan di OpenStreetMap"),
    ]
    ev["banjir"] += [
        Evidence(
            0.20,
            water_gate * ramp(g("gsw_water_occ"), 3, 20),
            f"Lokasi memang sering tergenang (JRC: air {_fmt(g('gsw_water_occ'), 0)}% dari waktu 1984–2021)",
        ),
        Evidence(0.10, water_gate * ramp(dvh, -3.0, -6.0), f"Radar Sentinel-1: pantulan khas air (VH {_fmt(dvh, 1)} dB)"),
    ]
    ev["longsor"] += [
        Evidence(0.10, ramp(dvh, -1.5, -3.5), radar_drop),
    ]
    ev["alami_lain"] += [
        # Radar C-band kurang peka pada pembukaan parsial/permukiman bertanaman (teruji di Morowali:
        # median perubahan VH ~0 dB pada perluasan permukiman). Karena itu "radar tidak berubah" hanya
        # dihitung sebagai bukti alami bila tajuk masih hijau secara optik.
        Evidence(
            0.15,
            _abs_small(dvh, 0.7, 2.0) * ramp(g("ndvi_after"), 0.45, 0.60),
            f"Radar Sentinel-1 tidak menunjukkan perubahan struktur tegakan (VH {_fmt(dvh, 1)} dB) dan tajuk masih hijau",
        ),
        Evidence(0.05, ramp(road, 500, 2000), f"Jauh dari jalan ({road or 0:.0f} m, OSM)"),
    ]


def classify(features: dict) -> dict:
    """Klasifikasi satu patch. Mengembalikan kelas, probabilitas, skor manusia, & alasan."""
    ev = _evidence(features)
    raw = {c: sum(e.weight * e.value for e in items) for c, items in ev.items()}
    # Gating: banjir mustahil tanpa air, jalan mustahil bila lebar.
    raw["banjir"] *= ramp(features.get("water_frac", 0.0), 0.05, 0.25)
    raw["tambak"] *= ramp(features.get("water_frac", 0.0), 0.2, 0.4)
    raw["tambang"] *= 1 - 0.6 * ramp(features.get("water_frac", 0.0), 0.5, 0.8)
    raw["penebangan"] *= 1 - ramp(features.get("water_frac", 0.0), 0.2, 0.5)
    raw["pembangunan"] *= 1 - ramp(features.get("water_frac", 0.0), 0.2, 0.5)

    mx = max(raw.values())
    exps = {c: math.exp((s - mx) / SOFTMAX_TEMPERATURE) for c, s in raw.items()}
    tot = sum(exps.values())
    probs = {c: e / tot for c, e in exps.items()}
    top = max(probs, key=probs.get)

    reasons = [
        e.text
        for e in sorted(ev[top], key=lambda e: e.weight * e.value, reverse=True)
        if e.value >= 0.5
    ][:4]

    return {
        "class": top,
        "label": CLASSES[top]["label"],
        "confidence": probs[top],
        "score": raw[top],
        "probs": probs,
        "raw_scores": raw,
        "reasons": reasons,
        "notes": [],
    }


def _human_score(probs: dict, fire_prior: float) -> float:
    return sum(p for c, p in probs.items() if c in HUMAN_CLASSES) + probs.get("kebakaran", 0) * fire_prior


def _category(human: float, top_score: float) -> str:
    if top_score < 0.35:
        return "perlu_verifikasi"
    if human >= 0.6:
        return "manusia"
    if human <= 0.4:
        return "alami"
    return "perlu_verifikasi"


def classify_all(patches: list) -> list[dict]:
    """Klasifikasi seluruh patch lalu terapkan koreksi konteks spasial antar-patch."""
    results = {p.label: classify(p.features) for p in patches}

    for p in patches:
        r = results[p.label]
        f = p.features
        neighbors = [results[l] for l in f.get("adjacent_labels", []) if l in results]
        human_nb = [n for n in neighbors if n["class"] in HUMAN_CLASSES]
        fire_prior = FIRE_HUMAN_PRIOR

        if r["class"] == "kebakaran":
            if human_nb:
                fire_prior = 0.85
                r["notes"].append("Berbatasan dengan area pembukaan lahan/tambang — pola pembakaran untuk pembukaan lahan")
            if f.get("rectangularity", 0) > 0.85:
                fire_prior = max(fire_prior, 0.8)
                r["notes"].append("Batas area terbakar lurus/geometris — indikasi pembakaran terencana")
            if f.get("hotspots", 0) == 0 and not human_nb:
                r["notes"].append("Tidak ada konteks pembukaan lahan; cek data hotspot & musim kemarau (El Niño)")

        human = _human_score(r["probs"], fire_prior)

        if r["class"] == "longsor" and human_nb:
            human = max(human, 0.5)
            r["notes"].append("Bersebelahan dengan jalan/galian — longsor mungkin dipicu pemotongan lereng")
        if r["class"] == "jalan_akses" and human_nb:
            r["notes"].append(f"Terhubung ke {len(human_nb)} area aktivitas manusia lain")
        if r["class"] in ("alami_lain", "banjir") and len(human_nb) >= 2:
            human = max(human, 0.45)
            r["notes"].append("Dikelilingi aktivitas manusia — dampak tidak langsung mungkin terjadi")

        _ancillary_notes(r, f)
        r["human_score"] = human
        r["category"] = _category(human, r["score"])
        r["category_label"] = CATEGORIES[r["category"]]
    return [results[p.label] for p in patches]


def _ancillary_notes(r: dict, f: dict) -> None:
    dvh = f.get("dvh_db")
    if dvh is not None:
        if dvh <= -1.5 and r["class"] != "banjir":
            r["notes"].append(f"Dikonfirmasi radar Sentinel-1: VH turun {abs(dvh):.1f} dB (perubahan struktur nyata, bukan efek awan/musim)")
        elif abs(dvh) < 0.7 and r["class"] in ("tambang", "penebangan") and (f.get("ndvi_after") or 1) < 0.3:
            r["notes"].append("Vegetasi hilang secara optik tetapi radar Sentinel-1 tidak berubah — verifikasi (artefak awan/asap?)")
    if (f.get("wc_agri_frac") or 0) > 0.5 and r["class"] in ("penebangan", "alami_lain"):
        r["notes"].append("WorldCover 2021 mencatat area ini sudah berupa semak/pertanian — mungkin panen/peremajaan kebun, bukan deforestasi baru")
    if (f.get("osm_mining_frac") or 0) > 0.3 and r["class"] != "tambang":
        r["notes"].append("Area ini terpetakan sebagai tambang/quarry di OpenStreetMap")


# --------------------------------------------------------------------------- pesisir
def _coastal_evidence(f: dict) -> dict[str, list[Evidence]]:
    g = f.get
    rect, conv = g("rectangularity", 0), g("convexity", 0)
    built = max(g("wc_built_ring") or 0, g("osm_built_ring") or 0)
    if g("direction") == "ke_air":
        return {
            "tambak": [
                Evidence(0.35, ramp(rect, 0.65, 0.90), f"Petak kolam geometris (rektangularitas {_fmt(rect)})"),
                Evidence(0.20, ramp(conv, 0.80, 0.95), "Tepi lurus/bersudut"),
                Evidence(0.25, ramp(g("n_neighbors_1500m", 0), 0, 3), f"Petak berderet ({g('n_neighbors_1500m', 0)} petak lain dalam 1,5 km)"),
                Evidence(0.20, ramp(g("dist_sea_m"), 50, 400), "Terpisah dari laut oleh pematang (tidak langsung terbuka ke laut)"),
            ],
            "abrasi": [
                Evidence(0.40, ramp(g("dist_sea_m"), 100, 0), "Bersambung langsung dengan laut — darat tergerus"),
                Evidence(0.25, ramp(g("elongation"), 2, 5), f"Memanjang sejajar garis pantai (elongasi {_fmt(g('elongation'), 1)})"),
                Evidence(0.20, ramp(rect, 0.75, 0.45), "Bentuk tidak beraturan"),
                Evidence(0.15, ramp(g("n_neighbors_1500m", 0), 3, 0), "Tidak berpola petak"),
            ],
        }
    return {
        "reklamasi": [
            Evidence(0.25, ramp(g("albedo_after"), 0.08, 0.14), f"Material urugan terang (albedo {_fmt(g('albedo_after'), 3)})"),
            Evidence(0.20, ramp(g("ndvi_after"), 0.3, 0.1), "Tanpa vegetasi"),
            Evidence(0.25, max(ramp(rect, 0.60, 0.85), ramp(conv, 0.85, 0.97)), f"Tepi lurus/geometris (rektangularitas {_fmt(rect)})"),
            Evidence(0.20, ramp(built, 0.05, 0.30), "Bersebelahan kawasan terbangun/pelabuhan (WorldCover/OSM)"),
            Evidence(0.10, ramp(g("area_ha", 0), 2, 20), f"Luas {g('area_ha', 0):.1f} ha"),
            Evidence(0.30, ramp(g("open_sea_contact", 0), 0.20, 0.05),
                     f"Pengurukan kolam/tambak di darat — tidak menghadap laut terbuka (kontak laut {g('open_sea_contact', 0) * 100:.0f}%)"),
        ],
        "akresi": [
            Evidence(0.30, ramp(g("open_sea_contact", 0), 0.10, 0.35),
                     f"Daratan tumbuh menghadap laut terbuka (kontak laut {g('open_sea_contact', 0) * 100:.0f}%)"),
            Evidence(0.35, max(ramp(g("ndvi_after"), 0.2, 0.45), ramp(g("albedo_after"), 0.09, 0.06)),
                     "Daratan baru bervegetasi (kolonisasi mangrove) atau lumpur endapan"),
            Evidence(0.30, ramp(rect, 0.70, 0.40), "Bentuk tidak beraturan, mengikuti arus/endapan"),
            Evidence(0.20, ramp(built, 0.20, 0.0), "Jauh dari kawasan terbangun"),
            Evidence(0.15, ramp(conv, 0.90, 0.70), "Tepi tidak lurus"),
        ],
    }


def classify_coastal(features: dict) -> dict:
    ev = _coastal_evidence(features)
    raw = {c: sum(e.weight * e.value for e in items) for c, items in ev.items()}
    if "akresi" in raw:
        # Akresi alami selalu berbatasan dengan laut; air->darat yang terkurung daratan = pengurukan (manusia).
        raw["akresi"] *= ramp(features.get("open_sea_contact", 0), 0.05, 0.20)
    mx = max(raw.values())
    exps = {c: math.exp((s - mx) / SOFTMAX_TEMPERATURE) for c, s in raw.items()}
    tot = sum(exps.values())
    probs = {c: e / tot for c, e in exps.items()}
    top = max(probs, key=probs.get)
    reasons = [e.text for e in sorted(ev[top], key=lambda e: e.weight * e.value, reverse=True) if e.value >= 0.5][:4]
    human = sum(p for c, p in probs.items() if CLASSES[c]["nature"] == "manusia")
    notes = []
    if features.get("gsw_occ") is None:
        notes.append("Tanpa data JRC: perubahan di zona pasang surut bisa akibat beda kondisi pasang antar komposit")
    if top == "abrasi":
        notes.append("Abrasi bisa dipicu aktivitas manusia (penambangan pasir, hilangnya mangrove, struktur pantai) — periksa konteks")
    return {"class": top, "label": CLASSES[top]["label"], "confidence": probs[top], "score": raw[top], "probs": probs,
            "raw_scores": raw, "reasons": reasons, "notes": notes, "human_score": human,
            "category": _category(human, raw[top]), "category_label": CATEGORIES[_category(human, raw[top])]}
