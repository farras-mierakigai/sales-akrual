#!/usr/bin/env bash
# Commit + push arsip data (folder data/ = clone repo farras-mierakigai/sales-akrual-data) ke GitHub.
# Jalankan manual setelah upload, atau otomatis tiap malam lewat cron:
#   crontab -e  →  30 2 * * * /home/<user>/sales-akrual/deploy/backup-arsip.sh >> /home/<user>/backup-arsip.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/../data"
git add -A
if git diff --cached --quiet; then
  echo "$(date '+%F %T') tidak ada perubahan"
  exit 0
fi
git -c user.name="sales-akrual" -c user.email="sales-akrual@mierakigai.local" commit -q -m "arsip $(date '+%F %H:%M') ($(hostname))"
git push -q origin HEAD
echo "$(date '+%F %T') arsip di-push: $(git log -1 --format=%h)"
