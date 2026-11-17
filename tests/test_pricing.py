"""Tes strategi harga: analysis.pricing (harga/unit, HPP, biaya marketplace dari neto, % iklan per channel) + API /api/pricing.

Jalankan dari root repo:  python3 tests/test_pricing.py   (atau pytest). Tanpa database.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd        # noqa: E402
import analysis            # noqa: E402


def _frames():
    ts = pd.Timestamp
    O = pd.DataFrame([
        # order 1: 2 unit A @100rb (harga coret 200rb), cair neto 80%
        dict(platform="Shopee", channel="Shopee", order_id="o1", created=ts("2026-01-05"), month="2026-01", is_unpaid=False, is_batal=False,
             is_retur=False, product="Brand - Produk A - x", qty=2, bruto=200000.0, harga_awal=400000.0, hpp_item="A", hpp_unit=40000.0),
        # order 2: 1 unit A @120rb, belum cair
        dict(platform="Shopee", channel="Shopee", order_id="o2", created=ts("2026-02-05"), month="2026-02", is_unpaid=False, is_batal=False,
             is_retur=False, product="Brand - Produk A - x", qty=1, bruto=120000.0, harga_awal=200000.0, hpp_item="A", hpp_unit=40000.0),
        # batal: tidak dihitung
        dict(platform="Shopee", channel="Shopee", order_id="o3", created=ts("2026-02-06"), month="2026-02", is_unpaid=False, is_batal=True,
             is_retur=False, product="Brand - Produk A - x", qty=5, bruto=500000.0, harga_awal=0.0, hpp_item="A", hpp_unit=40000.0),
        # TikTok, tanpa HPP
        dict(platform="TikTok", channel="TikTok", order_id="t1", created=ts("2026-01-07"), month="2026-01", is_unpaid=False, is_batal=False,
             is_retur=False, product="Brand - Produk B", qty=1, bruto=50000.0, harga_awal=0.0, hpp_item="", hpp_unit=0.0),
    ])
    I = pd.DataFrame([
        dict(platform="Shopee", channel="Shopee", order_id="o1", is_retur=False, neto=160000.0, harga_produk=200000.0),
        dict(platform="TikTok", channel="TikTok", order_id="t1", is_retur=False, neto=45000.0, harga_produk=50000.0),
    ])
    ads = {"channels": ["Meta", "TT"], "attribution": {"Meta": "shopee", "TT": "tiktok"},
           "months": {"2026-01": {"Meta": 20000, "TT": 5000}, "2026-02": {"Meta": 12000}}}
    return O, I, ads


def test_pricing_rows():
    O, I, ads = _frames()
    r = analysis.pricing(O, I, ads)
    by = {(x["channel"], x["hpp_item"]): x for x in r["rows"]}
    a = by[("Shopee", "A")]
    assert a["qty"] == 3 and a["bruto"] == 320000 and a["harga"] == 106667
    assert a["hpp_unit"] == 40000 and a["hpp_cover_pct"] == 100
    assert a["diskon_pct"] == 46.7                      # 1 − 320/600
    assert a["mp_pct"] == 20.0 and a["mp_src"] == "produk"   # 62,5% bruto sudah cair, rasio 0,8
    assert a["ads_pct"] == 10.0                         # 32rb iklan / 320rb bruto Shopee
    assert a["name"] == "Produk A"
    assert [h[0] for h in a["hist"]] == ["2026-01", "2026-02"] and a["hist"][1][2] == 120000
    assert a["points"][0][0] == 100000 and a["points"][0][1] == 2
    b = by[("TikTok", "")]
    assert b["hpp_unit"] is None and b["mp_pct"] == 10.0 and b["ads_pct"] == 10.0
    assert r["channels"]["Shopee"]["mp_pct"] == 20.0


def test_pricing_filters():
    O, I, ads = _frames()
    r = analysis.pricing(O, I, ads, platform="shopee", m_from="2026-02")
    assert len(r["rows"]) == 1 and r["rows"][0]["qty"] == 1
    assert r["rows"][0]["mp_pct"] == 20.0 and r["rows"][0]["mp_src"] == "platform"   # periode belum cair → rata-rata channel semua data
    assert r["channels"]["Shopee"]["mp_src"] == "semua data"
    assert analysis.pricing(O.iloc[:0], I, ads)["rows"] == []


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
