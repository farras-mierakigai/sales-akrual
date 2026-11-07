"""Tes demografi wilayah pembeli: normalisasi provinsi/kota di parser, analisa per wilayah, API /api/geo + Excel, parse ulang file lama.

Jalankan dari root repo:  python3 tests/test_geo.py   (atau pytest). Pakai SQLite sementara.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "db" in sys.modules:
    import db
    TMP = db.DATA
else:
    TMP = tempfile.mkdtemp(prefix="sa-geo-")
    os.environ.update(SA_MODE="server", SA_DATA_DIR=TMP, SA_DB_URL="sqlite:///" + os.path.join(TMP, "t.db"),
                      SA_ADMIN_USER="farras", SA_ADMIN_PASS="rahasia-tim-2026", SA_SECRET="x" * 32,
                      SA_GOOGLE_CLIENT_ID="id", SA_GOOGLE_CLIENT_SECRET="secret", SA_BASE_URL="https://apps.test")

import pandas as pd                      # noqa: E402
import sqlalchemy as sa                  # noqa: E402
import db, auth, parsers, analysis, app as appmod   # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

auth.bootstrap_from_env()
TOKO = "TokoGeo"


def farras():
    c = TestClient(appmod.app, base_url="https://apps.test")
    assert c.post("/api/login", json=dict(username="farras", password="rahasia-tim-2026")).status_code == 200
    return c


def test_norm_province_and_city():
    np_ = parsers.norm_province
    assert np_("JAWA BARAT") == np_("West Java Province") == np_("West+Java") == np_("Jawa Kulon") == "Jawa Barat"
    assert np_("DKI JAKARTA") == np_("Jakarta Raya") == np_("Daerah Khusus Ibukota Jakarta") == np_("Jakarta") == "DKI Jakarta"
    assert np_("DI YOGYAKARTA") == np_("D.I. Yogyakarta") == np_("Special Region of Yogyakarta") == np_("Yogyakarta") == "DI Yogyakarta"
    assert np_("NANGGROE ACEH DARUSSALAM (NAD)") == np_("D.I. Aceh") == "Aceh"
    assert np_("NUSA TENGGARA BARAT (NTB)") == np_("West Nusa Tenggara") == "Nusa Tenggara Barat"
    assert np_("Riau islands") == np_("KEPULAUAN RIAU") == "Kepulauan Riau"
    assert np_("BANGKA BELITUNG") == np_("Bangka Belitung Islands") == "Kepulauan Bangka Belitung"
    assert np_("North Sumatra") == np_("Sumatra Utara") == "Sumatera Utara"
    assert np_("") == np_(None) == ""
    assert np_("Negeri Antah") == "Negeri Antah"          # tidak dikenali → apa adanya (title case)
    nc = parsers.norm_city
    assert nc("KOTA JAKARTA TIMUR") == nc("Kota Administrasi Jakarta Timur") == nc("East Jakarta") == nc("Jakarta Timur") == "Kota Jakarta Timur"
    assert nc("Central Jakarta") == "Kota Jakarta Pusat"
    assert nc("KAB. BOGOR") == nc("Kabupaten Bogor") == nc("Bogor Regency") == nc("Kab.Bogor") == "Kab. Bogor"
    assert nc("Bekasi City") == nc("Kabupaten Kota Bekasi") == "Kota Bekasi"
    assert nc("South Tangerang") == "Tangerang Selatan" and nc("Kota Tangerang Selatan") == "Kota Tangerang Selatan"
    assert nc("Depok") == "Depok" and nc("Kecamatan Garut Kota") == "Kecamatan Garut Kota"
    assert nc("") == ""
    sd = parsers.shopee_district
    addr = "Jalan Marzuki I No 2A (samping klinik), KOTA JAKARTA TIMUR, CAKUNG, DKI JAKARTA, ID, 13940"
    assert sd(addr, "KOTA JAKARTA TIMUR", "DKI JAKARTA") == "Cakung"
    assert sd("Ja******", "KOTA BANDUNG", "JAWA BARAT") == ""                         # alamat disamarkan di export lama
    assert sd(addr, "KOTA BEKASI", "JAWA BARAT") == ""                                  # segmen tidak cocok kolom → jangan tebak
    assert parsers.norm_district("*************") == "" and parsers.norm_district("Kec. Medan Satria") == "Medan Satria"


def shopee_xlsx(path, rows):
    """rows: (order_id, tanggal, status, username, produk, qty, subtotal, provinsi, kota)"""
    df = pd.DataFrame([dict({"No. Pesanan": o, "Waktu Pesanan Dibuat": d, "Status Pesanan": st, "Status Pembatalan/ Pengembalian": "",
                             "Nomor Referensi SKU": "SKU1", "Nama Produk": p, "Nama Variasi": "", "Jumlah": q, "Subtotal Pesanan": sub,
                             "Harga Awal": sub, "Metode Pembayaran": "COD", "Alasan Pembatalan": "", "Username (Pembeli)": u,
                             "Nama Penerima": "X", "No. Telepon": "****", "Alamat Pengiriman": f"jl 1, {kota}, KEC {o[-3:]}, {prov}, ID, 10000",
                             "Kota/Kabupaten": kota, "Provinsi": prov})
                       for o, d, st, u, p, q, sub, prov, kota in rows])
    df.to_excel(path, index=False)


def tiktok_csv(path, rows, geo=True):
    """rows: (order_id, created, status, buyer, product, qty, subtotal, province, city). geo=False = format lama tanpa kolom alamat."""
    cols = ["Order ID", "Order Status", "Created Time", "SKU ID", "Seller SKU", "Product Name", "Variation", "Quantity",
            "SKU Subtotal Before Discount", "SKU Seller Discount", "SKU Subtotal After Discount", "Payment Method", "Cancel Reason",
            "Cancelation/Return Type", "Purchase Channel", "Buyer Username", "Recipient", "Phone #"] + (["Country", "Province", "Regency and City", "Districts"] if geo else [])
    df = pd.DataFrame([[o, st, d, "9001", "SKU1", p, "", q, sub, 0, sub, "COD", "", "", "", u, "R", "(+62)8**"] + ([("Indonesia"), prov, city, "***"] if geo else [])
                       for o, d, st, u, p, q, sub, prov, city in rows], columns=cols)
    df.to_csv(path, index=False)


def test_parser_keeps_region():
    sp = os.path.join(TMP, "Order.all.geo.xlsx")
    shopee_xlsx(sp, [("260101AAA", "2026-01-05 10:00", "Selesai", "budi", "Kemeja", 1, 100000, "JAWA BARAT", "KOTA BEKASI"),
                     ("260102BBB", "2026-01-06 10:00", "Selesai", "sari", "Kemeja", 1, 100000, "NUSA TENGGARA BARAT (NTB)", "KAB. LOMBOK TIMUR")])
    o = parsers.parse_shopee_orders(sp)
    assert list(o["province"]) == ["Jawa Barat", "Nusa Tenggara Barat"] and list(o["city"]) == ["Kota Bekasi", "Kab. Lombok Timur"]
    assert list(o["district"]) == ["Aaa", "Bbb"]                                     # awalan "KEC" dibuang
    tp = os.path.join(TMP, "Semua pesanan-geo.csv")
    tiktok_csv(tp, [("579000000001", "05/01/2026 10:00:00", "Selesai", "D***i", "Rok", 1, 50000, "West Java", "Bekasi City"),
                    ("579000000002", "06/01/2026 10:00:00", "Selesai", "A***a", "Rok", 1, 50000, "Jakarta Raya", "South Jakarta")])
    t = parsers.parse_tiktok_orders(tp)
    assert list(t["province"]) == ["Jawa Barat", "DKI Jakarta"] and list(t["city"]) == ["Kota Bekasi", "Kota Jakarta Selatan"]
    assert list(t["district"]) == ["", ""]                                              # TikTok menyamarkan kecamatan
    old = os.path.join(TMP, "Semua pesanan-lama.csv")
    tiktok_csv(old, [("579000000003", "05/01/2026 10:00:00", "Selesai", "D***i", "Rok", 1, 50000, "", "")], geo=False)
    t2 = parsers.parse_tiktok_orders(old)
    assert list(t2["province"]) == [""] and list(t2["city"]) == [""]


def _ol(oid, created, buyer, prov, city, product="P", ch=parsers.SHOPEE, bruto=100.0, batal=False, unpaid=False, retur=False, district=""):
    if not district and ch == parsers.SHOPEE and city:
        district = "Kec " + city.split()[-1]
    return dict(platform=parsers.SHOPEE if ch == parsers.SHOPEE else parsers.TIKTOK, channel=ch, order_id=oid,
                created=pd.Timestamp(created), month=created[:7], status="x", is_batal=batal, is_unpaid=unpaid, is_retur=retur,
                product=product, variation="", qty=1.0, bruto=bruto, buyer=buyer, buyer_key=buyer.lower(), province=prov, city=city, district=district)


def test_geo_analysis_rules():
    O = pd.DataFrame([
        _ol("A1", "2026-01-03", "ani", "Jawa Barat", "Kota Bekasi", "Kemeja"),
        _ol("A2", "2026-02-03", "ani", "Jawa Barat", "Kota Bekasi", "Kemeja"),             # ani beli 2× di Jabar
        _ol("B1", "2026-01-10", "budi", "DKI Jakarta", "Kota Jakarta Selatan", "Celana"),
        _ol("B1", "2026-01-10", "budi", "DKI Jakarta", "Kota Jakarta Selatan", "Kemeja"),   # 2 baris, 1 order
        _ol("C1", "2026-02-15", "citra", "Jawa Timur", "Kota Surabaya", "Celana", ch=parsers.TIKTOK),
        _ol("C2", "2026-02-15", "citra", "Jawa Timur", "Kota Surabaya", "Celana", ch=parsers.TIKTOK, batal=True),   # batal tidak dihitung
        _ol("D1", "2026-02-20", "dedi", "Sulawesi Selatan", "Kota Makassar", "Kemeja", ch=parsers.TIKTOK, bruto=300.0),
        _ol("E1", "2026-02-21", "eko", "Jawa Barat", "Bogor", "Kemeja"),                    # kota tanpa jenis → Kab. Bogor (hanya itu yang ada)
        _ol("E2", "2026-02-22", "eko", "Jawa Barat", "Kab. Bogor", "Kemeja"),
        _ol("F1", "2026-02-23", "fitri", "", "", "Kemeja", ch=parsers.TIKTOK),              # export lama tanpa wilayah
        _ol("G1", "2026-02-24", "gita", "Jawa Barat", "Bandung", "Kemeja"),                 # kota & kab. dua-duanya tidak ada → tetap 'Bandung (?)'
    ])
    G = analysis.geo_customers(O)
    T = G["totals"]
    assert G["no_geo"] == 1 and G["no_geo_bruto"] == 100.0
    assert T["orders"] == 8 and T["bruto"] == 1100.0 and T["customers"] == 6 and T["provinces"] == 4
    assert T["coverage_pct"] == 88.9                                  # 8 dari 9 order
    reg = {r["region"]: r for r in G["regions"]}
    assert reg["Jawa"]["orders"] == 7 and reg["Jawa"]["orders_pct"] == 87.5 and reg["Sulawesi"]["bruto"] == 300.0
    area = {a["area"]: a for a in G["areas"]}
    assert area["Jabodetabek"]["orders"] == 5                         # Bekasi 2 + Jaksel 1 + Bogor 2
    assert area["Jawa di luar Jabodetabek"]["orders"] == 2 and area["Luar Jawa"]["orders"] == 1   # Surabaya + Bandung; Makassar
    prov = {p["province"]: p for p in G["provinces"]}
    assert G["provinces"][0]["province"] == "Jawa Barat" and prov["Jawa Barat"]["orders"] == 5
    assert prov["Jawa Barat"]["customers"] == 3 and prov["Jawa Barat"]["repeat_customers"] == 2 and prov["Jawa Barat"]["repeat_pct"] == 66.7
    assert prov["Jawa Timur"]["tiktok"] == 1 and prov["Jawa Timur"]["shopee"] == 0 and prov["Jawa Barat"]["region"] == "Jawa"
    city = {c["city"]: c for c in G["cities"]}
    assert city["Kab. Bogor"]["orders"] == 2 and "Bogor" not in city and city["Bandung (?)"]["orders"] == 1
    assert city["Kota Jakarta Selatan"]["orders"] == 1 and city["Kota Jakarta Selatan"]["bruto"] == 200.0
    # kecamatan: hanya order Shopee (6 order) yang punya; % dihitung dari order berkecamatan
    assert T["district_orders"] == 6 and T["district_coverage_pct"] == 75.0 and T["district_shopee_pct"] == 100.0
    dist = {(d["city"], d["district"]): d for d in G["districts"]}
    assert dist[("Kota Bekasi", "Kec Bekasi")]["orders"] == 2 and dist[("Kota Bekasi", "Kec Bekasi")]["orders_pct"] == 33.3
    assert dist[("Kab. Bogor", "Kec Bogor")]["orders"] == 2 and ("Kota Surabaya", "Kec Surabaya") not in dist
    assert T["districts_total"] == 4 and all(d["province"] for d in G["districts"])
    m = {x["month"]: x for x in G["monthly"]}
    assert m["2026-01"]["orders"] == 2 and m["2026-01"]["by_orders"] == {"Jawa Barat": 1, "DKI Jakarta": 1}
    assert G["monthly_provinces"] == ["Jawa Barat", "Sulawesi Selatan", "DKI Jakarta", "Jawa Timur"]   # urut order lalu bruto
    pr = [(p["province"], p["product"], p["orders"]) for p in G["products"] if p["province"] == "Jawa Barat"]
    assert pr == [("Jawa Barat", "Kemeja", 5)]
    M = G["matrix"]
    assert M["baseline"]["Jawa Barat"] == 62.5
    row = {r["product"]: r for r in M["rows"]}
    assert row["Kemeja"]["orders"] == 7 and row["Kemeja"]["share"]["Jawa Barat"] == 71.4 and row["Celana"]["share"]["Jawa Timur"] == 50.0
    # filter bulan & platform
    G2 = analysis.geo_customers(O, m_from="2026-02", m_to="2026-02")
    assert G2["totals"]["orders"] == 6 and G2["no_geo"] == 1
    G3 = analysis.geo_customers(O, platform="tiktok")
    assert G3["totals"]["orders"] == 2 and G3["totals"]["provinces"] == 2
    # tanpa kolom wilayah sama sekali (data lama) → has_geo False, totals None
    G4 = analysis.geo_customers(O.drop(columns=["province", "city"]))
    assert G4["totals"] is None and G4["has_geo"] is False and G4["no_geo"] == 9


def test_api_geo_and_reparse_old_upload():
    c = farras()
    assert c.post("/api/toko", json=dict(name=TOKO)).status_code == 200
    sp = os.path.join(TMP, "Order.all.api.xlsx")
    shopee_xlsx(sp, [("260101AAA", "2026-01-05 10:00", "Selesai", "budi", "Kemeja", 1, 100000, "JAWA BARAT", "KOTA BEKASI"),
                     ("260102BBB", "2026-01-06 10:00", "Selesai", "sari", "Kemeja", 2, 200000, "JAWA TIMUR", "KOTA SURABAYA"),
                     ("260103CCC", "2026-01-07 10:00", "Batal", "sari", "Kemeja", 1, 100000, "JAWA TIMUR", "KOTA SURABAYA")])
    with open(sp, "rb") as fh:
        r = c.post("/api/upload", data=dict(toko=TOKO), files=[("files", ("Order.all.api.xlsx", fh.read()))])
    assert r.status_code == 200, r.text
    g = c.get("/api/geo", params=dict(toko=TOKO)).json()
    assert g["totals"]["orders"] == 2 and g["totals"]["provinces"] == 2 and g["reparsed"] == 0
    assert g["provinces"][0]["province"] in ("Jawa Barat", "Jawa Timur") and g["cities"][0]["city"] in ("Kota Bekasi", "Kota Surabaya")
    x = c.get("/api/geo.xlsx", params=dict(toko=TOKO))
    assert x.status_code == 200 and x.headers["content-type"].startswith("application/vnd.openxmlformats")
    # simulasi file yang diproses parser lama (sebelum kolom wilayah): kolom dikosongkan, versi diturunkan → tab memicu parse ulang dari arsip
    eng = db.engine()
    with eng.begin() as cx:
        cx.execute(sa.text("UPDATE upload SET parser_version='13' WHERE file='Order.all.api.xlsx'"))
        cx.execute(sa.text("UPDATE order_line SET province=NULL, city=NULL, district=NULL"))
    appmod._cache.clear()
    assert db.uploads_needing_buyer(TOKO) == ["Order.all.api.xlsx"]
    g2 = c.get("/api/geo", params=dict(toko=TOKO)).json()
    assert g2["reparsed"] == 1 and g2["totals"]["orders"] == 2 and g2["totals"]["provinces"] == 2 and g2["totals"]["districts_total"] == 2
    assert db.uploads_needing_buyer(TOKO) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("semua tes wilayah lulus")
