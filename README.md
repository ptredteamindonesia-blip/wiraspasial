# WIRA-SPASIAL — Enam domain, satu peta nasional

**WIRA-SPASIAL** = **W**awasan **I**ntelijen & **R**uang **A**nalisis **Spasial**.

Platform pemantauan geospasial Indonesia dengan enam modul di atas satu peta nasional. Modul
dikembangkan bertahap, dan setiap domain mengikuti kualitas serta hak akses datanya.

| Domain | Fungsi | Sumber data (kualitas · akses) |
|---|---|---|
| **SAT** Satellite | Perubahan lahan & vegetasi (penyebab alami vs manusia), **pesisir**: abrasi, akresi, reklamasi, tambak | Sentinel-2, Sentinel-1, Copernicus DEM, WorldCover, JRC GSW (ilmiah · publik) |
| **DIS** Disaster | Gempa (BMKG + USGS), hotspot VIIRS, **paparan risiko**: estimasi zona guncangan → kab/kota, area pantauan, pelabuhan, deteksi longsor terdampak; prioritas analisa pasca-gempa | BMKG (resmi), USGS & NASA FIRMS (ilmiah) · publik |
| **SEA** Maritime | **Analitik pelabuhan** dari deteksi kapal radar Sentinel-1 (bangunan tetap disaring), tren aktivitas, **AIS berlisensi** (aisstream.io / impor CSV), **dark vessel** | Sentinel-1 (estimasi · publik), AIS (berlisensi) |
| **MNE** Mining | Deteksi tambang × **lapisan izin**: dalam izin / sebagian / izin tidak aktif / di luar izin / tanpa data izin; aktivitas di kawasan lindung | Lapisan impor GeoJSON/Shapefile/ArcGIS REST (sesuai pemilik · terbatas) |
| **INF** Infrastructure | Inventaris jalan, jembatan, bangunan, kawasan urban; jalan & bangunan baru dari satelit; **jalan baru yang belum terpetakan** | OpenStreetMap (komunitas · publik), WorldCover |
| **OS** OSINT Fusion | Berita → topik → **geolokasi kab/kota** → **fusi** dengan deteksi satelit, gempa, hotspot, pelabuhan, izin; skor korroborasi | RSS Antara/Tempo/CNN Indonesia, Google News RSS (OSINT · publik), gazetteer OSM |

**Hak akses.** Setiap sumber/lapisan diberi tingkat *publik*, *terbatas*, atau *berlisensi*. Bila
`GEOINT_ACCESS_TOKEN` diisi di server, data terbatas & berlisensi (lapisan izin, data AIS) hanya
tampil, dan hanya bisa diimpor, dengan token yang dimasukkan di menu *Pengaturan* (dikirim sebagai header `X-Wira-Token`; header lama `X-GeoInt-Token` tetap diterima). Tanpa token,
aplikasi berjalan dalam mode pengguna tunggal (semua terbuka). Registri sumber di *Pengaturan*
menampilkan kualitas, lisensi, status, dan sakelar feed otomatis tiap sumber.

## Modul SAT — perubahan hutan & penyebabnya

Aplikasi untuk memantau perubahan tutupan lahan di kawasan hutan Indonesia dari citra satelit,
lalu menganalisa **apakah perubahan terjadi secara alami** (longsor, banjir, stres vegetasi)
**atau karena aktivitas manusia** (penambangan, penebangan/pembukaan lahan, jalan akses, pembangunan/permukiman/industri).
Kebakaran ditangani khusus: dinilai dari konteksnya, dan bila konteksnya tidak cukup
masuk kategori *perlu verifikasi*.

![alur](https://img.shields.io/badge/Sentinel--2-L2A-green) ![dem](https://img.shields.io/badge/DEM-Copernicus%20GLO--30-blue)

## Fitur

- **Data satelit nyata, gratis, tanpa API key**: Sentinel-2 L2A (10–20 m) dan Copernicus DEM GLO-30
  dari Microsoft Planetary Computer. Komposit median bebas awan dibuat per periode berdasarkan
  *Scene Classification Layer*, cocok untuk wilayah tropis yang sering berawan.
- **Deteksi perubahan**: piksel hutan (NDVI > 0,6) yang NDVI-nya turun atau dNBR-nya naik secara
  signifikan, lalu dikelompokkan menjadi poligon (patch).
- **Analisa penyebab yang bisa dijelaskan**: tiap patch diukur dengan ±25 indikator spektral,
  geometri, topografi, dan konteks, lalu diklasifikasi dengan aturan *fuzzy*. Setiap keputusan
  disertai **bukti utama**, probabilitas tiap kelas, dan **skor aktivitas manusia** (0–100%).
- **Pemantauan otomatis**: simpan area sebagai *pantauan*, dan penjadwal akan menganalisa ulang
  secara berkala serta membuat **alert** (bisa diteruskan ke webhook Slack/Teams/dll).
- **Dashboard peta**: menggambar AOI, wilayah contoh (Morowali, Weda, Kutai Kartanegara, Tesso Nilo,
  Pelalawan, Merauke, Sigi), overlay citra sebelum/sesudah, popup penjelasan, ekspor GeoJSON/CSV.
- **Fokus & perbesaran objek**: klik area perubahan di daftar (atau tombol *🔍 Fokus & perbesar* di
  popup) untuk membuka panel inset yang terhubung ke kotak penanda di peta utama. Panel bisa
  digeser/zoom sendiri, dan kotaknya ikut. Mode tampilan: citra resolusi tinggi, Sentinel-2 sebelum,
  sesudah, dan *Bandingkan* (geser pemisah sebelum|sesudah). Navigasi antar objek dengan ‹ › atau
  tombol panah, tutup dengan Esc. **Lensa pembesar ×4** mengikuti kursor (centang di panel Lapisan
  atau tekan `L`).
  Catatan: citra resolusi tinggi Esri bisa lebih tua atau lebih baru dari periode analisa. Gunakan
  mode Sebelum/Sesudah/Bandingkan untuk melihat kondisi sesuai periode.
- **Sumber data pendukung untuk akurasi** (gratis, tanpa API key, dimuat otomatis):
  | Sumber | Dipakai untuk |
  |---|---|
  | Sentinel-1 RTC (radar VH) | Menembus awan; mengonfirmasi hilangnya struktur tegakan, mendeteksi pantulan air & bangunan |
  | ESA WorldCover 2021 | Konteks: lahan terbangun / tanah terbuka di sekitar, area yang sudah berupa pertanian |
  | JRC Global Surface Water | Genangan di lokasi yang memang sering tergenang (banjir alami) vs kolam baru (galian tambang) |
  | OpenStreetMap (Overpass) | Area tambang/quarry yang terpetakan, permukiman/industri, jarak ke jalan |

  Status tiap sumber tercatat di hasil analisa. Bila satu sumber gagal (jaringan/kuota), analisa tetap
  jalan tanpa sumber itu dan akan dicoba lagi saat *Analisa ulang*. Pilih sumber dengan
  `GEOINT_ANCILLARY=s1,worldcover,gsw,osm`. Uji di Morowali: dengan sumber pendukung, area *perlu
  verifikasi* turun dari 9 ha ke 1 ha dan keyakinan rata-rata naik dari 0,78 ke 0,80. Catatan
  kalibrasi: radar C-band hampir tidak berubah (median ~0 dB) pada perluasan permukiman yang masih
  bertanaman, jadi "radar tidak berubah" hanya dipakai sebagai bukti alami bila tajuk masih hijau.
- **Opsional — NASA FIRMS**: titik panas VIIRS memperkuat deteksi kebakaran.
- **Mode demo offline**: citra sintetis dengan skenario yang jawabannya sudah diketahui, untuk
  demonstrasi dan pengujian tanpa internet.

## Menjalankan

```bash
pip install -r requirements.txt
python run.py                    # http://127.0.0.1:8000
python run.py --port 8080        # bila port 8000 sudah dipakai
python -m pytest -q              # tes otomatis
```

Disarankan memakai virtualenv (`python -m venv .venv`) agar dependensi tidak bentrok dengan
paket lain di sistem.

Di dashboard: pilih **Wilayah contoh** atau gambar area di peta → atur periode **sebelum** &
**sesudah** → pilih sumber data → **Jalankan analisa**. Analisa Sentinel-2 untuk area 5×5 km
memakan waktu sekitar 2–5 menit (sebagian besar untuk mengunduh citra).

### Berjalan di latar belakang & analisa otomatis

```bash
python run.py start      # jalankan di latar belakang; tetap jalan setelah terminal ditutup
python run.py status     # status server + analisa otomatis
python run.py logs -n 100
python run.py stop       # analisa yang terputus dilanjutkan saat start berikutnya
```

Agar ikut menyala sejak boot dan dinyalakan ulang bila crash, pakai layanan systemd
([deploy/geoint.service](deploy/geoint.service)) di Linux/WSL, atau Task Scheduler di Windows
(aksi: `pythonw run.py`, pemicu: saat login/startup).

**Analisa otomatis** (tab *Pantauan*):
- **Mulai / Hentikan analisa otomatis**: sakelar global yang tersimpan di database dan bertahan
  setelah restart. Saat dihentikan, analisa otomatis yang antri/berjalan bisa ikut dibatalkan.
- Per area: **Mulai / Hentikan pantau otomatis** dan **Jalankan sekarang**.
- Penjadwal memeriksa tiap `GEOINT_TICK_MINUTES` (default 10) dan menganalisa setiap area yang
  sudah jatuh tempo (`interval_days`). Area yang masih punya analisa berjalan dilewati, dan paling
  banyak `GEOINT_WORKERS` (default 2) analisa berjalan bersamaan.
- Setiap run tersimpan, jadi hasil pemantauan terus bertambah: grafik tren per area, ringkasan
  akumulasi (`GET /api/stats`), dan deret waktu per area (`GET /api/aois/{id}/history`).
- Analisa apa pun bisa **dibatalkan** (`POST /api/analyses/{id}/cancel`). Yang masih antri langsung
  batal, yang sedang berjalan berhenti di tahap berikutnya (saat unduhan citra besar bisa butuh
  beberapa menit).

### Penyimpanan data & restart server

Semua data disimpan di folder `data/` (atau `GEOINT_DATA_DIR`) dan dimuat kembali saat server
dinyalakan ulang:

| Data | Lokasi |
|---|---|
| Area pantauan, analisa, deteksi, alert, log aktivitas | `data/geoint.db` (SQLite) |
| Citra mentah per analisa — komposit sebelum/sesudah (6 band) + DEM | `data/products/<id>/inputs/*.tif` (GeoTIFF, bisa dibuka di QGIS) |
| Citra pratinjau & peta kelas | `data/products/<id>/*.png` |

- Analisa yang **terputus karena server berhenti dilanjutkan otomatis** saat start (maks. 3 kali
  percobaan). Bila citra sudah sempat terunduh, analisa langsung memakai data tersimpan.
- **Analisa ulang** (tombol di hasil/riwayat) memakai citra tersimpan tanpa mengunduh lagi, berguna
  setelah aturan klasifikasi diperbarui. Alert yang sama tidak digandakan.
- **Log aktivitas** (tab *Aktivitas*, `GET /api/activity`) mencatat start/stop server, pembuatan,
  perubahan, dan penghapusan area pantauan, mulai/selesai/gagal/dilanjutkannya analisa, alert, dan
  peninjauan alert. Log tetap ada walaupun objeknya sudah dihapus.
- Menghapus area pantauan **tidak** menghapus riwayat analisa & alert-nya. Menghapus analisa ikut
  menghapus file citranya.
- Dashboard mengingat analisa terakhir yang dibuka, isian formulir, dan tab aktif, lalu otomatis
  memantau analisa yang masih berjalan setelah halaman dimuat ulang.
- Untuk backup, cukup salin folder `data/` saat server berhenti.

### Hasil uji dengan data asli (Oktober 2026)

| Uji | Hasil |
|---|---|
| Feed BMKG / USGS | 27 dan 34 gempa; duplikat USGS–BMKG (±2 menit, ≤100 km) ditandai |
| Hotspot FIRMS 24 jam | ±28.700 titik (keyakinan rendah dibuang, disaring ke daratan Indonesia) dalam 20 detik; teratas Ogan Komering Ilir, Kotawaringin Barat/Timur, Pulang Pisau |
| Gempa dalam M6,2 (145 km) | Zona IV (dirasakan) ±185 km → 4 kabupaten di Halmahera/Morotai |
| Berita RSS + Google News | 587 berita relevan, 264 tergeolokasi (mis. karhutla → Palangka Raya, tambang ilegal → Bontang) |
| Radar Tanjung Priok | 5 akuisisi, 116–174 kapal per akuisisi setelah ±160 target tetap (bagan/platform) disaring |
| Inventaris OSM Morowali | 144 km jalan, 880 bangunan, 1.627 ha kawasan industri |
| Pesisir PIK 2 / Tangerang (2019 vs 2025) | 281 ha reklamasi/pengurukan tambak, 20 ha akresi menghadap laut, 15,6 ha tambak, 4,6 ha abrasi. Versi awal salah menyebut pengurukan sebagai "akresi" alami; diperbaiki dengan indikator kontak laut terbuka (akresi alami harus menghadap laut) |

### Konfigurasi (environment variable)

| Variabel | Default | Keterangan |
|---|---|---|
| `GEOINT_DATA_DIR` | `./data` | Lokasi database SQLite & citra hasil |
| `GEOINT_RESOLUTION_M` | `20` | Resolusi analisa (m). Otomatis dinaikkan untuk AOI besar |
| `GEOINT_MAX_AOI_KM2` | `1500` | Batas luas AOI |
| `GEOINT_MAX_CLOUD` | `70` | Batas tutupan awan per citra (%) |
| `GEOINT_MAX_SCENES` | `8` | Jumlah tanggal akuisisi per komposit |
| `GEOINT_MIN_PATCH_HA` | `0.5` | Luas minimum patch yang dilaporkan |
| `GEOINT_ALERT_MIN_HA` | `2` | Luas minimum aktivitas manusia yang memicu alert |
| `GEOINT_ALERT_WEBHOOK` | – | URL webhook untuk notifikasi alert (POST JSON, field `text`) |
| `FIRMS_MAP_KEY` | – | MAP_KEY NASA FIRMS ([daftar gratis](https://firms.modaps.eosdis.nasa.gov/api/map_key/)) |
| `GEOINT_SCHEDULER` | `1` | `0` untuk mematikan thread penjadwal (mis. untuk tes) |
| `GEOINT_TICK_MINUTES` | `10` | Interval pemeriksaan area yang jatuh tempo |
| `GEOINT_WORKERS` | `2` | Jumlah analisa paralel |
| `GEOINT_ANCILLARY` | `s1,worldcover,gsw,osm` | Sumber data pendukung yang dipakai |
| `GEOINT_FEEDS` | `1` | `0` untuk mematikan feed domain (gempa, hotspot, berita, radar pelabuhan, AIS) |
| `GEOINT_ACCESS_TOKEN` | – | Token untuk data *terbatas* & *berlisensi*. Kosong = mode pengguna tunggal |
| `AISSTREAM_API_KEY` | – | API key [aisstream.io](https://aisstream.io) untuk AIS realtime di area pelabuhan |
| `GEOINT_RSS_FEEDS` | Antara, Tempo, CNN Indonesia | Daftar RSS berita (dipisah koma) |
| `GEOINT_GOOGLE_NEWS` | `1` | `0` untuk mematikan Google News RSS (lisensi hanya untuk penggunaan pribadi non-komersial) |

## Cara kerja analisa

```
Sentinel-2 (sebelum) ─┐                         ┌─ indikator spektral (NDVI, NBR, BSI, NDWI, NDTI, rasio Fe)
Sentinel-2 (sesudah) ─┼─► deteksi perubahan ─► patch ─┼─ geometri (rektangularitas, elongasi, lebar, konveksitas)
Copernicus DEM ───────┘                         ├─ topografi (lereng, elevasi relatif, keselarasan arah lereng)
NASA FIRMS (opsional) ──────────────────────────┴─ konteks (patch tetangga, lahan non-hutan di sekeliling, jarak sungai, titik panas)
                                                          │
                                       klasifikasi fuzzy + koreksi konteks
                                                          │
                                    kelas · kategori · skor manusia · bukti
```

| Kelas | Kategori | Bukti utama yang dipakai |
|---|---|---|
| Penambangan | manusia | Tanah terbuka terang (BSI, albedo), kolam air keruh (NDTI), tanah laterit (rasio merah/biru), area luas/mengelompok |
| Penebangan / pembukaan lahan | manusia | Bentuk geometris bersudut, sisa semak (NDVI sedang), blok berderet |
| Jalan akses / jalan angkut | manusia | Sempit (< 60 m) dan memanjang |
| Pembangunan / permukiman / industri | manusia | Permukaan heterogen (atap/beton), warna abu-kebiruan (bukan laterit), datar, tumbuh dari lahan terbangun yang sudah ada |
| Kebakaran | konteks* | dNBR tinggi, NBR negatif, permukaan gelap, titik panas FIRMS |
| Tanah longsor | alami | Lereng terjal, memanjang searah turunan lereng, terisolasi |
| Banjir / genangan | alami | Fraksi air tinggi, dataran rendah, dekat sungai |
| Degradasi / stres vegetasi | alami | Penurunan NDVI ringan, vegetasi masih ada, bentuk tak beraturan |

\* Kebakaran yang berbatasan dengan pembukaan lahan/tambang, atau batasnya lurus, dinilai
**manusia**. Tanpa konteks itu: **perlu verifikasi**. Koreksi konteks lain: longsor di sebelah
jalan/galian ditandai *mungkin dipicu pemotongan lereng*.

Kategori ditentukan dari skor manusia: ≥ 60% → manusia, ≤ 40% → alami, di antaranya atau bila
bukti lemah → perlu verifikasi lapangan.

## Batasan yang perlu diketahui

**Domain nasional**
- *Paparan gempa* memakai relasi atenuasi kasar (I ≈ 1,5M − 3,5·log10 R + 3), sekadar untuk penapisan
  cepat dan **bukan** pengganti ShakeMap resmi BMKG/USGS. Untuk gempa dalam, estimasinya cenderung rendah.
- *Wilayah hotspot* dihitung dari kab/kota dengan titik pusat terdekat, jadi perkiraan dan bukan batas
  administratif. Titik yang berulang di lokasi sama ≥3 hari ditandai sebagai kemungkinan sumber industri (flare/smelter).
- *Deteksi kapal radar* mencakup semua benda terang di laut yang bergerak (kapal, tongkang, kapal ikan).
  Kapal kecil di bawah ±15 m sering terlewat. Koordinat pelabuhan awal bersifat perkiraan dan bisa diedit.
- *Dark vessel* hanya dihitung bila ada data AIS pada waktu akuisisi radar. Tanpa AIS nilainya "n/a",
  dan aplikasi tidak menebak.
- *Status izin* hanya sebaik lapisan yang diimpor. "Di luar izin" berarti tidak ada di lapisan itu
  (indikasi, wajib verifikasi), dan area tanpa lapisan izin berstatus "tidak ada data izin", bukan ilegal.
- *Geolokasi berita* = pusat kab/kota/provinsi yang disebut (±40/±150 km). Nama yang juga kata umum
  (Batu, Buru, Batang, …) hanya dikenali dengan prefix "Kabupaten/Kota". Berita adalah petunjuk, bukan bukti.
- *Pesisir*: perbedaan pasang surut antar komposit bisa menimbulkan perubahan semu. Zona pasang surut
  disaring dengan JRC GSW (darat→air hanya di lokasi yang jarang tergenang, air→darat hanya di lokasi
  yang hampir selalu air).
- Server Overpass publik (OSM) kadang sibuk atau menolak permintaan. Aplikasi mencoba server cermin dan
  menampilkan galatnya bila semuanya gagal.

**Modul SAT**

- Klasifikasi berbasis aturan dengan ambang yang dikalibrasi dari pengetahuan domain, **belum
  dilatih/divalidasi dengan data lapangan**. Gunakan hasil sebagai *indikasi* dan prioritas
  verifikasi, bukan bukti hukum. Langkah berikutnya yang disarankan: kumpulkan titik validasi
  (mis. dari data izin IUP/PBPH, laporan KLHK, verifikasi lapangan) lalu hitung akurasi per kelas
  atau latih model ML dengan indikator yang sama.
- Resolusi 20 m: tebang pilih di bawah kanopi dan jalan sarad yang sangat sempit bisa terlewat.
- Wilayah yang terus berawan (mis. Papua pada musim hujan) bisa menghasilkan cakupan valid rendah;
  perpanjang rentang tanggal atau gunakan musim kemarau. Untuk tutupan awan persisten, integrasi
  radar Sentinel-1 adalah pengembangan berikutnya yang paling bernilai.
- Perkebunan sawit/HTI yang sudah tumbuh bisa memiliki NDVI tinggi, sehingga tidak masuk masker
  "hutan sebelum" dan konversinya tidak terdeteksi bila periode sebelum sudah berupa kebun.
- Membedakan permukiman/kawasan industri dari tambang atau tebangan hanya dari spektrum 20 m
  terbatas; patch yang ambigu sengaja diberi keyakinan rendah. Uji di Morowali (2019 vs 2025)
  mendeteksi ±890 ha perubahan, mayoritas kawasan industri & permukiman yang tumbuh di pesisir.
- Koordinat wilayah contoh bersifat perkiraan.

## Struktur kode

```
geoint/
  sources/sentinel.py   Sentinel-2 + DEM (Planetary Computer), komposit bebas awan
  sources/synthetic.py  data demo dengan skenario yang diketahui
  indices.py            indeks spektral
  change.py             deteksi perubahan + ekstraksi indikator per patch
  classifier.py         klasifikasi alami vs manusia (aturan fuzzy, bisa dijelaskan)
  pipeline.py           orkestrasi analisa, ringkasan, alert, webhook
  firms.py              titik panas NASA FIRMS (opsional)
  ancillary.py          sumber pendukung: Sentinel-1, WorldCover, JRC GSW, OpenStreetMap
  coastal.py            perubahan pesisir: abrasi, akresi, reklamasi, tambak
  domains/              modul nasional: common (registri sumber, hak akses, peristiwa), gazetteer,
                        disaster, maritime, mining, infrastructure, osint
  api_domains.py        REST API enam domain + peta nasional
  products.py           PNG true-color sebelum/sesudah & peta kelas
  scheduler.py          pemantauan otomatis berkala
  db.py                 SQLite + migrasi skema + log aktivitas
  storage.py            penyimpanan citra mentah (GeoTIFF) untuk resume & analisa ulang
  api.py                REST API FastAPI + static web
web/                    dashboard: app.js (SAT), focus.js (fokus & lensa), domains.js (rail, beranda, DIS/SEA/MNE/INF/OS)
tests/                  tes end-to-end dengan data demo
```

### REST API ringkas

| Method | Endpoint | Fungsi |
|---|---|---|
| GET | `/api/meta` | kelas, kategori, sumber data, wilayah contoh |
| POST | `/api/analyses` | jalankan analisa (`geometry`/`bbox`/`aoi_id`, `before`, `after`, `source`) |
| GET | `/api/analyses/{id}` | status, progres, ringkasan, URL citra |
| GET | `/api/analyses/{id}/detections` | GeoJSON deteksi + indikator + alasan |
| GET | `/api/analyses/{id}/export.csv` · `.geojson` | ekspor |
| GET/POST/PATCH/DELETE | `/api/aois` | area pantauan otomatis |
| POST | `/api/aois/{id}/run` | jalankan pemantauan sekarang |
| GET | `/api/alerts` · POST `/api/alerts/{id}/ack` | alert |
| POST | `/api/analyses/{id}/rerun` | analisa ulang dari citra tersimpan |
| GET | `/api/activity` | log aktivitas (`limit`, `before_id`, `analysis_id`, `aoi_id`) |
| GET | `/api/monitor` · POST `/api/monitor/start` · `/stop?cancel_pending=true` · `/check` | analisa otomatis |
| POST | `/api/analyses/{id}/cancel` | batalkan analisa |
| GET | `/api/aois/{id}/history` · `/api/stats` | riwayat pemantauan & akumulasi |
| GET | `/api/domains` · `/api/sources` · `/api/access` | ringkasan enam domain, registri sumber, tingkat akses |
| GET | `/api/map/layers?layers=earthquakes,hotspots,ports,ships,news,aois,detections,permits` | lapisan peta nasional (GeoJSON) |
| POST | `/api/sources/{id}/run` · `/enable?on=` | jalankan feed sekarang / aktifkan feed otomatis |
| GET/POST | `/api/dis/earthquakes` · `/{id}/exposure` · `/{id}/prioritize` · `/api/dis/hotspots/summary` | DIS |
| GET/POST | `/api/sea/ports` · `/{id}` · `/{id}/scan` · `/api/sea/ais/import` · `/api/sea/ais/status` | SEA |
| GET/POST | `/api/mne/layers` · `/layers/upload` · `/layers/arcgis` · `/dashboard` · `/recompute` | MNE |
| GET | `/api/inf/overview` · `/api/inf/aois/{id}?refresh=` | INF |
| GET | `/api/os/news` · `/api/os/news/{id}/fusion` · `/api/os/topics` | OS |

Dokumentasi interaktif: `http://127.0.0.1:8000/docs`.
