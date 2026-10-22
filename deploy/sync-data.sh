#!/usr/bin/env bash
# Salin arsip data/ (file export + hpp.json/ads.json lama) dari Mac ke VPS; setelah itu jalankan migrate.py di VPS.
# Pakai: deploy/sync-data.sh user@ip-vps [/path/sales-akrual di VPS, default ~/sales-akrual]
# Arah: Mac → VPS saja. File yang sudah ada di VPS dengan isi sama dilewati (rsync). .users.json & .secret di VPS TIDAK ditimpa.
set -euo pipefail
HOST="${1:?pakai: deploy/sync-data.sh user@ip-vps [path]}"
DEST="${2:-sales-akrual}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
rsync -avz --progress --exclude '.cache/' --exclude '.users.json' --exclude '.secret' --exclude '.DS_Store' --exclude 'sales.db*' \
  "$HERE/data/" "$HOST:$DEST/data/"
echo "selesai → di VPS jalankan: cd $DEST && docker compose exec app python3 migrate.py"
