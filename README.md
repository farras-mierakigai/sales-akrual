# Sales Akrual

Analisa penjualan **riil** Shopee · TikTok Shop · Tokopedia dari file export seller center — angka diakui di **bulan order dibuat** (akrual), sudah dipotong pesanan batal, refund, dan biaya marketplace yang dipasangkan ke order-nya. Bedanya dengan dashboard seller center: dashboard belum mengurangi pembatalan & potongan.

## Jalankan

```bash
cd "~/Documents/Github Repo Farras/sales-akrual" && python3 app.py
```

lalu buka http://127.0.0.1:8766 (butuh: fastapi, uvicorn, pandas, openpyxl, python-multipart — `pip3 install --user -r requirements.txt`). Ini **mode lokal**: tanpa login, ada fitur scan folder Downloads.

## Online untuk tim (mode server, di VPS)

Mode server = ada **login multi-user** (file `data/.users.json`, password di-hash), fitur scan folder Mac dimatikan (tim pakai upload), app dengar di `0.0.0.0` di belakang Caddy (HTTPS otomatis). Semua pengguna bisa buat toko, upload, hitung, unduh laporan/audit; **hapus toko & kelola pengguna hanya admin**.

Env yang dibaca `app.py`: `SA_MODE` (`local`/`server`), `SA_DATA_DIR`, `SA_HOST`, `SA_PORT`, `SA_SECRET` (rahasia cookie; kosong = dibuat otomatis di `data/.secret`), `SA_ADMIN_USER` + `SA_ADMIN_PASS` (admin pertama, dipakai hanya kalau belum ada pengguna).

### Deploy pertama kali (Ubuntu/Debian VPS, 2 GB RAM cukup)

```bash
# 1. di VPS: Docker
curl -fsSL https://get.docker.com | sh

# 2. ambil kode (repo private → login gh/SSH dulu, atau scp folder ini)
git clone git@github.com:farras-mierakigai/sales-akrual.git && cd sales-akrual

# 3. konfigurasi
cp .env.example .env && nano .env      # SA_DOMAIN (DNS A record → IP VPS), SA_ADMIN_USER, SA_ADMIN_PASS

# 4. jalankan (build ±2 menit)
docker compose up -d --build
docker compose logs -f app             # tunggu "Uvicorn running"
```

Buka `https://<SA_DOMAIN>` → login admin → tab **Data & file → Pengguna** untuk membuat akun tim. Tanpa domain: `SA_DOMAIN=:80` lalu buka `http://<IP-VPS>` (tanpa HTTPS — password & data toko lewat jalur polos, hanya untuk uji coba).

### Pindahkan data yang sudah ada di Mac

```bash
deploy/sync-data.sh user@ip-vps            # rsync data/ Mac → VPS (file cache & akun tidak ikut)
ssh user@ip-vps 'cd sales-akrual && docker compose restart app'
```

Hitung pertama per toko di VPS membaca ulang semua file (1–2 menit untuk toko besar), setelah itu kena cache.

### Operasional

```bash
docker compose exec app python3 app.py users                 # daftar pengguna
docker compose exec app python3 app.py adduser rina          # tambah user (tanya password)
docker compose exec app python3 app.py adduser budi --admin  # tambah admin
docker compose exec app python3 app.py passwd farras         # reset password (lupa password admin)
git pull && docker compose up -d --build                     # update kode
tar czf backup-$(date +%F).tgz data/                         # backup = folder data/ saja
```

Catatan: satu proses app (cache hasil baca di memori); kalau tim serentak klik Hitung di toko berbeda, antri sebentar — normal. Firewall VPS: buka hanya 22, 80, 443.

## Alur pakai

1. Tab **Data & file** → buat toko (mis. `Eltahj`). Satu toko = satu folder `data/<toko>/`. Export Shopee tidak mencantumkan nama toko, jadi pisahkan sendiri.
2. Masukkan file: drag-drop, atau **Scan folder** (default `~/Downloads`) → centang → salin ke toko. Kolom *petunjuk toko* = brand terbanyak di nama produk / username penjual.
3. Klik **Hitung**. Filter: platform (gabungan / Shopee / TikTok / Tokopedia), rentang bulan.
4. **⬇ Excel laporan** → file `Laporan Penjualan AKRUAL <toko>.xlsx` (sheet Gabungan / Shopee / TikTok / Tokopedia, format sama dengan laporan Jan–Jun 2026).

## File yang dikenali (deteksi dari isi, bukan nama)

| Platform | File | Dipakai untuk |
|---|---|---|
| Shopee | `Order.all.*.xlsx` (Pesanan → Export) | pesanan, status, batal, bruto (kolom *Subtotal Pesanan*; export lama ≤ Apr 2026 pakai *Total Harga Produk*) |
| Shopee | `Income.*.xlsx` (Keuangan → Saldo → Laporan Penghasilan) | potongan per order, refund, dana diterima; format baru (sheet *Penghasilan*) maupun lama (sheet *Income*, 1 baris per order) |
| TikTok/Tokopedia | `Semua pesanan-*.csv` atau `.xlsx` | pesanan, status, batal, bruto; kolom *Purchase Channel* memisahkan Tokopedia. CSV yang sebagian rusak (byte NUL) tetap dibaca baris utuhnya; yang rusak total ditandai **FILE RUSAK** |
| TikTok/Tokopedia | `income_*.xlsx` (Keuangan → Laporan) | potongan per order, refund, settlement; header Inggris 2025 (*TikTok Shop commission fee*, *Flat fee*, *Dynamic Commission*), Inggris 2026, maupun Indonesia |
| TikTok | `Onhold-unsettled-*.xlsx` (Keuangan → Belum dibayar) | opsional: estimasi resmi dana per order yang belum cair |

Periode file boleh tumpang-tindih: baris yang sama diambil dari file yang paling baru (status paling mutakhir). File besar di-cache di `data/<toko>/.cache/` setelah dibaca pertama kali.

## Definisi angka

- **Nilai pesanan masuk** = semua order (termasuk batal) ≈ angka dashboard.
- **Peredaran bruto** = nilai produk setelah diskon penjual, order non-batal dan non-retur, per bulan order. **Retur penuh** = order yang di laporan penghasilan refund-nya ≥ 99% harga produk → dikeluarkan dari bruto walau status pesanan bukan batal (aturan Farras 2026-10-08); refund-nya tidak masuk baris refund (sudah keluar dari bruto). Shopee = `Subtotal Pesanan` (aturan tim, jangan kurangi *Diskon Dari Penjual* lagi). TikTok = `SKU Subtotal Before Discount − SKU Seller Discount` (= subtotal setelah diskon penjual di laporan penghasilan; diskon platform ditanggung TikTok, bukan pengurang). Status *Belum bayar* tidak dihitung.
- **Potongan marketplace** = kolom biaya di laporan penghasilan, dijumlah per order lalu diakui di bulan order (bukan bulan cair). Shopee: admin, proses pesanan, gratis ongkir XTRA, layanan, komisi AMS, promosi lain, lainnya, PPh 22 (+ baris ongkir net & voucher penjual sebagai penambah/pengurang dana). TikTok: komisi platform, komisi dinamis, pemrosesan, afiliasi, ongkir net, PPh 22, lainnya.
- **Neto / dana diterima** = *Total Penghasilan* (Shopee) / *Jumlah penyelesaian pembayaran* (TikTok) per order.
- **Settle %** = porsi bruto yang order-nya sudah muncul di laporan penghasilan. Di bawah 100% berarti potongan/refund/neto bulan itu masih parsial → upload laporan penghasilan periode berikutnya.
- **Taksiran dana akan cair** = untuk order terjual yang belum ada di laporan penghasilan: bruto × rasio neto/bruto-settle order yang sudah cair (per platform & bulan order; fallback rasio semua bulan bila < 30 order cair). TikTok memakai estimasi resmi per order dari file Onhold-unsettled bila ada. **Neto proyeksi** = neto + taksiran. Tabel "Order belum cair per status" memecahnya per status pesanan dan menandai status berisiko.
- **Penyesuaian non-order TikTok** (pembayaran GMV untuk iklan, penggantian logistik) ditampilkan terpisah per bulan cair, tidak masuk akrual penjualan.
- **Cek silang**: bruto per order dari file pesanan dibandingkan dengan *harga produk* di laporan penghasilan — harus cocok persis.

## HPP & laba (tab HPP & Laba)

- Master HPP per toko: impor dari link Google Sheet (export xlsx; link harus bisa dibuka siapa saja) atau upload xlsx/csv dengan kolom **Nama Produk** + **HPP** (opsional Pembulatan, Harga Jual). Disimpan di `data/<toko>/hpp.json`; pilih kolom HPP presisi atau Pembulatan.
- Pemetaan listing marketplace → item HPP: pertama lewat **kode SKU** listing = kolom *Kode* master (alias dipisah `/`, bundle `A+B` = jumlah komponen), lalu override manual per listing/SKU, lalu otomatis lewat nama model (token inti pertama, mis. Runa/Rumi/Nami); nama model yang unik di sheet dicocokkan longgar, yang dipakai beberapa item (mis. Aeri …) butuh kecocokan ≥ 75% token. Override manual disimpan di `data/<toko>/hpp_map.json`.
- Angka: **HPP produk terjual** = Σ qty × HPP (order non-batal non-retur, hanya qty yang punya HPP; cakupan ditampilkan), **Gross profit** = bruto − HPP, **GPM** = GP / bruto, **Laba setelah marketplace** = neto proyeksi − HPP (sebelum iklan & opex). Ada di tile, tab Per bulan, tab Produk (per listing), dan Excel.

## Audit (tab Audit)

1. **Kelengkapan per bulan** — hari tanpa order (export tidak menutup bulan), bulan yang hanya punya laporan penghasilan tanpa file pesanan, settle %.
2. **Rekonsiliasi vs total resmi** — per file laporan penghasilan: angka di sheet ringkasan resmi platform (Shopee *Summary*: harga asli produk, refund, biaya admin/proses/XTRA/AMS, PPh 22, total dilepas; TikTok *Laporan/Reports*: settlement, subtotal, refund, total biaya, komisi, dinamis, proses, ongkir, PPh) dibandingkan jumlah rincian per order yang dibaca app. Selisih kecil bisa berasal dari platform sendiri (order ada di ringkasan tapi tidak di rincian).
3. **Temuan per order** — ada di laporan penghasilan tapi tidak di file pesanan; terjual >30 hari belum settle; retur penuh (dana dikembalikan penuh, otomatis keluar dari bruto); neto negatif; potongan >50%/negatif; batal tapi ada dana; status berubah antar export; bruto ≠ harga produk; terjual Rp 0; tanggal kosong; order ID lintas platform.
4. **Jejak order** — cari satu order ID: semua baris sumber di tiap file + cara dihitungnya.

Semua bisa diunduh sebagai `Audit Data Penjualan <toko>.xlsx` (sheet per temuan).

## Struktur

- `parsers.py` — deteksi & parsing 4 format → DataFrame kanonis
- `analysis.py` — dedup, agregasi per bulan, breakdown produk/status/bayar, export Excel
- `hpp.py` — master HPP per toko, impor sheet, pencocokan listing → item, gross profit
- `audit.py` — kelengkapan, rekonsiliasi vs ringkasan resmi, temuan per order, jejak order, export audit
- `app.py` — FastAPI (upload, scan folder [lokal], summary, export, login & pengguna, CLI `adduser`/`passwd`/`users`)
- `auth.py` — akun & sesi mode server (PBKDF2 + cookie HMAC, tanpa database)
- `static/index.html` — UI (palet website Mierakigai); `static/login.html` — halaman masuk
- `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`, `.env.example`, `deploy/sync-data.sh` — deploy ke VPS
