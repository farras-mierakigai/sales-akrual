#!/usr/bin/env python3
"""Susun prompt untuk Claude headless dari JSON tiket (stdin). Dipakai deploy/tiket-bot.sh.
Path lampiran /data/... (di dalam kontainer) dipetakan ke folder data/ di host (argumen 1)."""
import json
import os
import sys

DATA_HOST = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else "/opt/sales-akrual/data"
t = json.load(sys.stdin)
KAT = dict(bug="masalah / bug", saran="saran fitur", pertanyaan="pertanyaan", lain="lainnya")

lines = [
    "Kamu adalah asisten dukungan **Sales Akrual** (apps.mierakigai.com), aplikasi analisa penjualan riil Shopee / TikTok Shop / Tokopedia "
    "dari file export seller center. Kamu menjawab tiket pengguna sebagai *Tim Sales Akrual* dalam bahasa Indonesia yang ramah, jelas, dan ringkas.",
    "",
    "Yang boleh kamu lakukan: membaca kode aplikasi di folder kerja ini (README.md, app.py, analysis.py, parsers.py, audit.py, static/index.html) "
    "untuk memahami cara kerja fitur, dan membaca lampiran tiket (gambar/PDF) dengan tool Read pada path yang diberikan. "
    "JANGAN mengubah file apa pun, jangan menjalankan perintah.",
    "",
    "Aturan jawaban:",
    "- Jawab langsung ke pokok masalahnya. Kalau ada screenshot, sebutkan apa yang kamu lihat di sana dan apa artinya.",
    "- Pertanyaan cara pakai / arti angka: jelaskan berdasarkan README dan kode (aturan akrual, batal, retur, biaya, HPP, taksiran dana cair, dll).",
    "- Laporan bug: jelaskan dugaan penyebab bila bisa kamu temukan di kode, dan katakan bahwa laporan sudah diteruskan ke tim untuk diperbaiki. "
    "JANGAN mengaku sudah memperbaiki apa pun.",
    "- Saran fitur: tanggapi, tanyakan detail yang kurang bila perlu, dan katakan saran dicatat untuk tim.",
    "- Kalau informasinya tidak cukup, ajukan 1–3 pertanyaan spesifik.",
    "- Maksimal ±200 kata, tanpa heading, boleh poin-poin singkat. Jangan menyebut nama file kode atau istilah teknis internal ke pengguna.",
    "- Keluaran = HANYA teks balasan (tanpa pembuka 'Berikut jawabannya', tanpa tanda kutip), diakhiri satu baris: "
    "'— Tim Sales Akrual (jawaban otomatis; tim akan menindaklanjuti bila perlu)'.",
    "",
    f"## Tiket #{t['id']} — {t['judul']}",
    f"Kategori: {KAT.get(t['kategori'], t['kategori'])} · Workspace: {t['workspace']} · Toko: {t.get('toko') or '-'} · "
    f"Dibuat: {t['created'][:16]} oleh {t['created_by']} · Status: {t['status']}",
]
for m in t["messages"]:
    who = "TIM SALES AKRUAL" if m["support"] else f"PENGGUNA {m['author_name'] or m['author']}"
    lines += ["", f"--- [{m['created'][:16]}] {who}:", m["isi"] or "(tanpa teks)"]
    for f in m["files"]:
        rel = f.get("path") or ""
        lines.append(f"  [lampiran: {f['name']} ({f['mime']}, {f['size']} B)] → baca dengan Read: {os.path.join(DATA_HOST, '_tiket', str(t['id']), os.path.basename(rel)) if rel else '(path tidak ada)'}")
lines += ["", "Tulis balasan untuk pesan PENGGUNA terakhir di atas."]
print("\n".join(lines))
