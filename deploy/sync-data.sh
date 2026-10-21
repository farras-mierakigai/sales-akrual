#!/usr/bin/env bash
# Salin folder data/ (file export + HPP + iklan) dari Mac ke VPS, sekali di awal atau kapan pun mau sinkron.
# Pakai: deploy/sync-data.sh user@ip-vps [/path/sales-akrual di VPS, default ~/sales-akrual]
# Arah: Mac → VPS saja. File yang sudah ada di VPS dengan isi sama dilewati (rsync). .users.json & .secret di VPS TIDAK ditimpa.
set -euo pipefail
HOST="${1:?pakai: deploy/sync-data.sh user@ip-vps [path]}"
DEST="${2:-sales-akrual}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
rsync -avz --progress --exclude '.cache/' --exclude '.users.json' --exclude '.secret' --exclude '.DS_Store' \
  "$HERE/data/" "$HOST:$DEST/data/"
echo "selesai → di VPS jalankan: cd $DEST && docker compose restart app"
