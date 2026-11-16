"""Tes cache hasil & snapshot data (fastcache): hasil disimpan, ikut berubah saat data/HPP/iklan berubah, toko dihapus bersih.

Jalankan dari root repo:  python3 tests/test_cache.py   (atau pytest). Pakai SQLite sementara.
"""
import os
import sys
import glob
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if "db" in sys.modules:
    import db
    TMP = db.DATA
else:
    TMP = tempfile.mkdtemp(prefix="sa-cache-")
    os.environ.update(SA_MODE="server", SA_DATA_DIR=TMP, SA_DB_URL="sqlite:///" + os.path.join(TMP, "t.db"),
                      SA_ADMIN_USER="farras", SA_ADMIN_PASS="rahasia-tim-2026", SA_SECRET="x" * 32,
                      SA_GOOGLE_CLIENT_ID="id", SA_GOOGLE_CLIENT_SECRET="secret", SA_BASE_URL="https://apps.test")

import db, auth, analysis, fastcache, app as appmod   # noqa: E402
from test_geo import shopee_xlsx, farras               # noqa: E402

auth.bootstrap_from_env()
TOKO = "TokoCache"


def _upload(c, name, rows):
    p = os.path.join(TMP, name)
    shopee_xlsx(p, rows)
    with open(p, "rb") as fh:
        r = c.post("/api/upload", data=dict(toko=TOKO), files=[("files", (name, fh.read()))])
    assert r.status_code == 200, r.text


def test_result_cache_follows_data_version():
    c = farras()
    assert c.post("/api/toko", json=dict(name=TOKO)).status_code == 200
    _upload(c, "Order.all.c1.xlsx", [("260101AAA", "2026-01-05 10:00", "Selesai", "budi", "Kemeja", 1, 100000, "JAWA BARAT", "KOTA BEKASI"),
                                     ("260102BBB", "2026-02-06 10:00", "Selesai", "budi", "Kemeja", 1, 150000, "JAWA BARAT", "KOTA BEKASI")])
    s1 = c.get("/api/summary", params=dict(toko=TOKO)).json()
    assert s1["totals"]["bruto"] == 250000
    # grafik per channel ikut di respons yang sama (tidak perlu 3 request tambahan)
    assert s1["by_platform_months"]["shopee"] == {"2026-01": 100000, "2026-02": 150000}
    ws = db.current_ws()
    rdir = fastcache._results_dir(ws, TOKO)
    assert glob.glob(os.path.join(rdir, "*", "summary-*.json.gz")), "hasil summary tersimpan di disk"
    assert glob.glob(os.path.join(fastcache._frames_dir(ws, TOKO), "*.pkl")), "snapshot data tersimpan"
    # dari cache: hasil sama persis
    assert c.get("/api/summary", params=dict(toko=TOKO)).json() == s1
    # memori dikosongkan → tetap dari disk, hasil sama
    fastcache._results.clear(); fastcache._results_bytes = 0; fastcache._frames.clear()
    assert c.get("/api/summary", params=dict(toko=TOKO)).json() == s1
    r1 = c.get("/api/repeat", params=dict(toko=TOKO)).json()
    assert r1["totals"]["repeat_customers"] == 1 and r1["reparsed"] == 0 and r1["reparse_missing"] == []
    # filter berbeda = kunci berbeda
    s_jan = c.get("/api/summary", params=dict(toko=TOKO, d_from="2026-01-01", d_to="2026-01-31")).json()
    assert s_jan["totals"]["bruto"] == 100000
    # upload baru → versi naik → hasil baru, cache versi lama dibuang
    _upload(c, "Order.all.c2.xlsx", [("260303CCC", "2026-03-07 10:00", "Selesai", "sari", "Kemeja", 2, 200000, "JAWA TIMUR", "KOTA SURABAYA")])
    s2 = c.get("/api/summary", params=dict(toko=TOKO)).json()
    assert s2["totals"]["bruto"] == 450000
    assert len(os.listdir(rdir)) == 1, "hanya folder versi terbaru yang tersisa"
    assert len(glob.glob(os.path.join(fastcache._frames_dir(ws, TOKO), "*.pkl"))) == 1
    files = c.get("/api/files", params=dict(toko=TOKO)).json()
    assert sorted(f["file"] for f in files) == ["Order.all.c1.xlsx", "Order.all.c2.xlsx"]
    # iklan diubah → versi naik → summary ikut berubah
    ads = c.get("/api/ads", params=dict(toko=TOKO)).json()
    ads["months"] = {"2026-01": {ads["channels"][0]: 50000}}
    assert c.post("/api/ads", json=dict(toko=TOKO, channels=ads["channels"], attribution=ads["attribution"], months=ads["months"])).status_code == 200
    s3 = c.get("/api/summary", params=dict(toko=TOKO)).json()
    assert s3["totals"]["iklan"] == 50000
    assert c.get("/api/ads", params=dict(toko=TOKO)).json()["months"]["2026-01"][ads["channels"][0]] == 50000


def test_z_deleted_toko_recreated_does_not_inherit_cache():
    c = farras()
    assert c.delete("/api/toko", params=dict(toko=TOKO)).status_code == 200
    assert not os.path.exists(fastcache._results_dir(db.current_ws(), TOKO))
    assert c.post("/api/toko", json=dict(name=TOKO)).status_code == 200
    _upload(c, "Order.all.c9.xlsx", [("260909ZZZ", "2026-09-05 10:00", "Selesai", "tono", "Kaos", 1, 70000, "BALI", "KOTA DENPASAR")])
    s = c.get("/api/summary", params=dict(toko=TOKO)).json()
    assert s["totals"]["bruto"] == 70000


def test_z_warmer_fills_cache():
    import threading, time
    ws = db.current_ws()
    fastcache._results.clear(); fastcache._results_bytes = 0
    for d in glob.glob(os.path.join(fastcache._results_dir(ws, TOKO), "*")):
        import shutil
        shutil.rmtree(d)
    fastcache._warmed.clear()
    th = fastcache.start_warmer([lambda t: appmod.api_files(t), lambda t: appmod.api_summary(t)], interval=1, delay=0)
    for _ in range(100):
        if fastcache._warmed.get((ws, TOKO)):
            break
        time.sleep(0.1)
    assert fastcache._warmed.get((ws, TOKO)), "warmer memanaskan toko"
    assert glob.glob(os.path.join(fastcache._results_dir(ws, TOKO), "*", "summary-*.json.gz"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("semua tes cache lulus")
