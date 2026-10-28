#!/usr/bin/env bash
# Bot tiket otomatis: tiket berstatus "open" yang pesan terakhirnya dari pengguna dijawab oleh Claude (headless),
# yang membaca isi tiket, lampiran (screenshot/PDF), dan kode aplikasi, lalu balasan diposting sebagai Tim Sales Akrual.
# Jadwalkan tiap 5 menit:  */5 * * * * /opt/sales-akrual/deploy/tiket-bot.sh >> /var/log/sales-akrual-tiket-bot.log 2>&1
# Pakai manual:            deploy/tiket-bot.sh [--dry-run] [--id N]   (dry-run = cetak jawaban, tidak diposting)
# Env: SA_BOT_MODEL (default sonnet), SA_BOT_OFF=1 untuk mematikan sementara.
set -uo pipefail
cd "$(dirname "$0")/.."
[ "${SA_BOT_OFF:-}" = "1" ] && exit 0
exec 9>/tmp/sales-akrual-tiket-bot.lock; flock -n 9 || exit 0
DRY=0; ONLY=""
while [ $# -gt 0 ]; do case "$1" in --dry-run) DRY=1;; --id) ONLY="$2"; shift;; esac; shift; done
MODEL="${SA_BOT_MODEL:-sonnet}"
export HOME="${HOME:-/root}"
app() { docker compose exec -T app python3 app.py "$@"; }
ts() { date '+%F %T'; }

if [ -n "$ONLY" ]; then ids="$ONLY"; else
  ids=$(app tiket --json | python3 -c 'import json,sys; print(" ".join(str(t["id"]) for t in json.load(sys.stdin) if t["status"]=="open" and not t["last_support"]))')
fi
[ -z "$ids" ] && exit 0

for id in $ids; do
  detail=$(app tiket "$id" --json) || { echo "$(ts) #$id: gagal membaca tiket"; continue; }
  last=$(echo "$detail" | python3 -c 'import json,sys; t=json.load(sys.stdin); print(t["messages"][-1]["id"] if t.get("messages") else "")')
  mark="data/_tiket/$id/.bot-last-msg"
  if [ "$DRY" = 0 ] && [ -f "$mark" ] && [ "$(cat "$mark")" = "$last" ]; then continue; fi   # pesan ini sudah pernah dicoba
  mkdir -p "data/_tiket/$id"; [ "$DRY" = 0 ] && echo "$last" > "$mark"
  prompt=$(echo "$detail" | python3 deploy/tiket-prompt.py "$PWD/data")
  reply=$(env -u CLAUDECODE timeout 600 claude -p "$prompt" --model "$MODEL" --permission-mode default \
            --allowedTools "Read,Grep,Glob" --disallowedTools "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent" \
            --output-format text </dev/null 2>>/tmp/sales-akrual-tiket-bot.err) || { echo "$(ts) #$id: claude gagal (lihat /tmp/sales-akrual-tiket-bot.err)"; continue; }
  reply=$(printf '%s' "$reply" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  if [ -z "$reply" ] || [ ${#reply} -gt 6000 ]; then echo "$(ts) #$id: jawaban kosong/terlalu panjang, dilewati"; continue; fi
  if [ "$DRY" = 1 ]; then printf '%s #%s (dry-run) jawaban:\n%s\n\n' "$(ts)" "$id" "$reply"; continue; fi
  docker compose exec -T -e SA_REPLY_NAME="Tim Sales Akrual (asisten otomatis)" app python3 app.py tiket-balas "$id" "$reply" >/dev/null \
    && echo "$(ts) #$id: dijawab (${#reply} karakter)" || echo "$(ts) #$id: gagal memposting balasan"
done
