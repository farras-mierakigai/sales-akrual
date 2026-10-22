"""Isi database dari folder arsip <SA_DATA_DIR>/<toko>/ (file export, hpp.json, hpp_map.json, ads.json, .users.json).

Dipakai sekali saat pindah dari versi berbasis file ke database, atau untuk parse ulang semua file
setelah parsers.py berubah (PARSER_VERSION naik). Aman diulang: file dengan nama sama diganti.

    python3 migrate.py            # semua toko
    python3 migrate.py Eltahj     # satu toko
    python3 migrate.py --users    # hanya .users.json
"""
import os
import sys
import json
import time
import glob

import db
import hpp as hppmod
import ads as adsmod
import auth


def migrate_users():
    p = os.path.join(db.DATA, ".users.json")
    if not os.path.exists(p):
        return 0
    with open(p) as fh:
        users = json.load(fh)
    cur = db.users_load()
    cur.update(users)
    db.users_save(cur)
    return len(users)


def migrate_toko(name, reparse=True):
    d = db.archive_dir(name)
    db.create_toko(name)
    n_ok = n_bad = 0
    if reparse:
        files = sorted(p for p in glob.glob(os.path.join(d, "*")) if os.path.isfile(p)
                       and not os.path.basename(p).startswith(".") and not p.lower().endswith(".json") and not p.lower().endswith(".db"))
        for i, p in enumerate(files, 1):
            t0 = time.time()
            try:
                r = db.ingest_file(name, p, uploaded_by="migrasi")
            except Exception as e:
                print(f"  [{i}/{len(files)}] {os.path.basename(p)}: GAGAL {e}")
                n_bad += 1
                continue
            if r is None:
                print(f"  [{i}/{len(files)}] {os.path.basename(p)}: format tidak dikenali, dilewati")
                n_bad += 1
            else:
                n_ok += 1
                print(f"  [{i}/{len(files)}] {os.path.basename(p)}: {r['kind']} ({time.time() - t0:.1f}s)")
    ph, pm, pa = (os.path.join(d, n) for n in ("hpp.json", "hpp_map.json", "ads.json"))
    if os.path.exists(ph) or os.path.exists(pm):
        master = {"items": [], "extra": [], "col": "hpp", "source": "", "updated": ""}
        mapping = {}
        if os.path.exists(ph):
            with open(ph) as fh:
                master.update(json.load(fh))
        if os.path.exists(pm):
            with open(pm) as fh:
                mapping = json.load(fh)
        hppmod.save(name, master=master, mapping=mapping)
        print(f"  HPP: {len(master.get('items', []))} item sheet + {len(master.get('extra', []))} manual, {len(mapping)} pemetaan")
    if os.path.exists(pa):
        with open(pa) as fh:
            adsmod.save(name, json.load(fh))
        print("  Iklan: ads.json dimuat")
    return n_ok, n_bad


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print(f"database: {db.db_url().split('@')[-1]}\narsip   : {db.DATA}")
    if "--users" in sys.argv or not args:
        n = migrate_users()
        if n:
            print(f"pengguna: {n} dimuat dari .users.json")
        if "--users" in sys.argv:
            sys.exit(0)
    names = args or sorted(x for x in os.listdir(db.DATA) if os.path.isdir(os.path.join(db.DATA, x)) and not x.startswith("."))
    for name in names:
        print(f"\n== {name}")
        ok, bad = migrate_toko(name)
        print(f"  {ok} file masuk, {bad} dilewati")
    print("\nisi database:", db.stats())
