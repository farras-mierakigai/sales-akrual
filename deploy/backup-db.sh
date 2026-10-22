#!/usr/bin/env bash
# Backup database Postgres (pg_dump → gzip) ke folder backup, simpan 14 hari terakhir.
# Jalankan manual, atau otomatis tiap malam lewat cron (lihat README):
#   15 2 * * * /opt/sales-akrual/deploy/backup-db.sh >> /var/log/sales-akrual-backup.log 2>&1
# Pulihkan:  gunzip -c /opt/backups/sales-<tanggal>.sql.gz | docker compose exec -T db psql -U sales -d sales
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="${SA_BACKUP_DIR:-/opt/backups}"
KEEP_DAYS="${SA_BACKUP_KEEP_DAYS:-14}"
mkdir -p "$DIR"
out="$DIR/sales-$(date '+%F-%H%M').sql.gz"
tmp="$out.part"
docker compose exec -T db pg_dump -U sales --no-owner --no-privileges sales | gzip -6 > "$tmp"
# dump kosong/gagal jangan menimpa yang lama
if [ "$(stat -c %s "$tmp")" -lt 1024 ] || ! gunzip -t "$tmp" 2>/dev/null; then
  rm -f "$tmp"; echo "$(date '+%F %T') GAGAL: dump kosong atau rusak"; exit 1
fi
mv "$tmp" "$out"
find "$DIR" -name 'sales-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') backup db: $out ($(du -h "$out" | cut -f1)), tersimpan $(ls "$DIR"/sales-*.sql.gz | wc -l) file"
