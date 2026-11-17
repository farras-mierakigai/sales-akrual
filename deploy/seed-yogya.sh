#!/bin/sh
# Sekali jalan: buat channel offline "Consignment Yogya" (≤100.000 → 23%, di atas → 30%) + "Event" untuk toko Eltahj (workspace 1).
# Tidak menimpa bila Eltahj sudah punya channel offline.
cd /opt/sales-akrual && docker compose exec -T app python3 - <<'PY'
import db, app
db.set_ws(1)
cur = db.pricing_load("Eltahj")
if cur.get("channels"):
    print("Eltahj sudah punya channel offline:", [c["name"] for c in cur["channels"]], "— tidak diubah")
else:
    cfg = app.clean_pricing_cfg({"channels": [
        {"id": "yogya", "name": "Consignment Yogya", "kind": "consignment", "tiers": [{"upto": 100000, "fee": 23}, {"upto": None, "fee": 30}], "ref": "Shopee"},
        {"id": "event", "name": "Event", "kind": "event", "tiers": [{"upto": None, "fee": 0}], "ref": "Shopee"}]})
    db.pricing_save("Eltahj", cfg)
    print("OK:", [c["name"] for c in cfg["channels"]])
PY
