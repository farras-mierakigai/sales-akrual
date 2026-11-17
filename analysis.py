"""Agregasi akrual: semua angka diakui di BULAN ORDER DIBUAT, biaya dicocokkan ke order-nya."""
import os
import glob
import re
import numpy as np
import pandas as pd
import parsers
import db
import hpp as hppmod
import ads as adsmod
from parsers import SHOPEE, TIKTOK, TOKOPEDIA

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.expanduser(os.environ.get("SA_DATA_DIR") or os.path.join(ROOT, "data")))

# baris potongan per platform (key, label) — nilai di income bertanda negatif
FEE_LINES = {
    SHOPEE: [
        ("biaya_admin", "Biaya Administrasi"),
        ("biaya_proses", "Biaya Proses Pesanan"),
        ("biaya_xtra", "Biaya Gratis Ongkir XTRA"),
        ("biaya_layanan", "Biaya Layanan (SPayLater/Transaksi)"),
        ("komisi_ams", "Biaya Komisi AMS (Afiliasi)"),
        ("promosi_lain", "Biaya Promosi lain (AMS fee/Kampanye)"),
        ("biaya_lainnya", "Biaya Lainnya (Premi/FBS/Pre-Order)"),
        ("pph22", "PPh 22"),
    ],
    TIKTOK: [
        ("komisi_platform", "Komisi platform"),
        ("komisi_dinamis", "Komisi dinamis"),
        ("biaya_proses", "Biaya pemrosesan pesanan"),
        ("afiliasi", "Komisi afiliasi (+mitra & iklan toko)"),
        ("ongkir_net", "Ongkir ditanggung penjual (net)"),
        ("pph22", "PPh Pasal 22"),
        ("biaya_lainnya", "Biaya lainnya (payment/program/ads)"),
    ],
}
# baris non-biaya yang juga mengurangi/menambah dana (Shopee)
OTHER_LINES = {
    SHOPEE: [("ongkir_net", "Ongkir (net: dibayar pembeli − ke kurir + subsidi)"),
             ("voucher_penjual", "Voucher/Cashback ditanggung Penjual")],
    TIKTOK: [],
}


# ---------------------------------------------------------------- loading

def list_toko():
    return db.list_toko()


def toko_dir(toko):
    """Folder arsip file export mentah (<DATA>/<toko>/). Data yang dihitung dibaca dari database."""
    return db.archive_dir(toko)


def load_toko(toko):
    """→ (O, I, A, files, raw) dari database — bentuk persis sama seperti saat masih membaca file."""
    O, I, A, U, files = db.load_frames(toko)
    for m in files:
        if m["kind"] in ("shopee_orders", "tiktok_orders") and len(O):
            top = O[O["file"] == m["file"]]["product"].str.split(r"\s[-–]\s", n=1).str[0].str.strip()
            m["hint"] = top.value_counts().index[0] if len(top) else ""
    if len(U):
        U = U.sort_values("file_mtime").drop_duplicates("line_key", keep="last").reset_index(drop=True)
    # petunjuk toko untuk file tanpa nama toko/produk: cocokkan nomor order ke file pesanan di toko ini
    if len(O):
        brand_of = O.drop_duplicates("order_id").set_index("order_id")["product"].str.split(r"\s[-–]\s", n=1).str[0].str.strip()
        for m in files:
            if m.get("hint"):
                continue
            src = None
            if m["kind"] in ("shopee_income", "tiktok_income") and len(I):
                src = I[I["file"] == m["file"]]["order_id"]
            elif m["kind"] == "tiktok_unsettled" and len(U):
                uu = U[U["file"] == m["file"]]
                top = uu["product"].str.split(r"\s[-–]\s", n=1).str[0].str.strip() if "product" in uu else pd.Series(dtype=str)
                top = top[top != ""]
                if len(top):
                    m["hint"] = top.value_counts().index[0]
                    continue
                src = uu["order_id"]
            if src is None or not len(src):
                continue
            ids = set(src)
            hit = brand_of[brand_of.index.isin(ids)]
            n = int(hit.index.nunique())
            if n:
                m["hint"] = f"{hit.value_counts().index[0]} (via {n:,} order cocok)".replace(",", ".")
                m["match"] = n
            else:
                m["hint"] = "⚠ 0 order cocok dengan file pesanan di toko ini"
                m["match"] = 0
            if m.get("seller"):
                m["hint"] = f"{m['seller']} · " + m["hint"]
    raw = dict(orders=O.copy() if len(O) else O, income=I.copy() if len(I) else I, adj=A.copy() if len(A) else A, unsettled=U)
    # dedup: baris sama di beberapa export → ambil dari file terbaru (status paling mutakhir)
    if len(O):
        O = O.sort_values("file_mtime").drop_duplicates("line_key", keep="last").reset_index(drop=True)
        O["month"] = _ym(O["created"])
    if len(I):
        I = I.sort_values("file_mtime").drop_duplicates("line_key", keep="last").reset_index(drop=True)
    # aturan Farras 2026-10-08: dana dikembalikan penuh = retur → keluar dari bruto walau status non-batal
    retur_ids = full_refund_ids(I)
    if len(I):
        I["is_retur"] = I["order_id"].isin(retur_ids)
    if len(O):
        O["is_retur"] = O["order_id"].isin(retur_ids) & ~O["is_batal"]
        O = hppmod.apply(O, toko)
    if len(A):
        A = A.drop_duplicates(["id", "type", "settled", "amount"]).reset_index(drop=True)
    return O, I, A, files, raw


def full_refund_ids(I):
    """Order yang refund-nya ≥ 99% harga produk (dana dikembalikan penuh)."""
    if not len(I):
        return set()
    g = I.groupby("order_id").agg(h=("harga_produk", "sum"), r=("refund", "sum"))
    return set(g[(g["h"] > 0) & (g["r"] <= -0.99 * g["h"])].index)


def _ym(s, unit="M"):
    """= s.dt.strftime("%Y-%m") (unit "M") / "%Y-%m-%d" (unit "D"), NaT → NaN — tapi yang diformat hanya nilai uniknya
    (jutaan baris → puluhan/ratusan string), jauh lebih cepat."""
    fmt = "%Y-%m" if unit == "M" else "%Y-%m-%d"
    if not len(s) or str(s.dtype) != "datetime64[ns]":
        return s.dt.strftime(fmt)
    codes, uniq = pd.factorize(s.values.astype(f"datetime64[{unit}]"))
    lab = pd.DatetimeIndex(uniq).strftime(fmt).to_numpy(dtype=object)
    out = np.full(len(codes), np.nan, dtype=object)
    ok = codes >= 0
    out[ok] = lab[codes[ok]]
    return pd.Series(out, index=s.index, name=s.name)


def _group_min_str(keys, s):
    """= s.groupby(keys).transform("min") untuk kolom teks (mis. "YYYY-MM"), lewat kode angka berurutan supaya tidak jatuh
    ke jalur Python per kelompok (ratusan ribu pelanggan → puluhan detik)."""
    codes, uniq = pd.factorize(s, sort=True)
    big = len(uniq)
    m = pd.Series(np.where(codes < 0, big, codes), index=s.index).groupby(keys.values).transform("min").to_numpy()
    out = np.full(len(m), np.nan, dtype=object)
    ok = m < big
    out[ok] = np.asarray(uniq, dtype=object)[m[ok]]
    return pd.Series(out, index=s.index, name=s.name)


def _cols(df, cols):
    """Hanya kolom yang dipakai (kolom yang tidak ada dilewati) → filter/salin baris jauh lebih ringan."""
    return df[[c for c in cols if c in df.columns]] if len(df.columns) else df


def filter_dates(O, I, d_from=None, d_to=None):
    """Batasi ke order yang dibuat dalam rentang tanggal (inklusif). Income ikut order-nya; income tanpa order ikut tanggal order di income."""
    if not d_from and not d_to:
        return O, I
    lo = pd.Timestamp(d_from) if d_from else None
    hi = (pd.Timestamp(d_to) + pd.Timedelta(days=1)) if d_to else None
    def inrange(ts):
        m = pd.Series(True, index=ts.index)
        if lo is not None:
            m &= ts >= lo
        if hi is not None:
            m &= ts < hi
        return m
    if len(O):
        O = O[inrange(O["created"])].reset_index(drop=True)
    if len(I):
        if len(O):
            known = set(O["order_id"])
            allO = I["order_id"].isin(known)
            I = I[allO | (~I["order_id"].isin(known) & inrange(I["created"]))]
            # order yang ada di file pesanan tapi di luar rentang: buang (sudah tidak ada di O)
        else:
            I = I[inrange(I["created"])]
        I = I.reset_index(drop=True)
    return O, I


def month_fraction(month, d_from=None, d_to=None):
    """Porsi hari bulan tsb yang masuk rentang tanggal (buat prorata iklan)."""
    import calendar, datetime as dt
    y, m = int(month[:4]), int(month[5:7])
    nd = calendar.monthrange(y, m)[1]
    a, b = dt.date(y, m, 1), dt.date(y, m, nd)
    if d_from:
        a = max(a, dt.date.fromisoformat(d_from))
    if d_to:
        b = min(b, dt.date.fromisoformat(d_to))
    return max(0, (b - a).days + 1) / nd


# ---------------------------------------------------------------- agregasi

def _month_range(months, m_from, m_to):
    ms = sorted(m for m in months if m and m != "NaT")
    if m_from:
        ms = [m for m in ms if m >= m_from]
    if m_to:
        ms = [m for m in ms if m <= m_to]
    return ms


def _channel_filter(df, platform):
    if not len(df) or platform in (None, "", "all"):
        return df
    if platform == "shopee":
        return df[df["platform"] == SHOPEE]
    if platform == "tiktok":
        return df[(df["platform"] == TIKTOK) & (df["channel"] == TIKTOK)]
    if platform == "tokopedia":
        return df[df["channel"] == TOKOPEDIA]
    if platform == "tiktok_all":
        return df[df["platform"] == TIKTOK]
    return df


_MONTHLY_O = ["platform", "channel", "order_id", "month", "is_unpaid", "is_batal", "is_retur", "bruto", "qty", "hpp_unit", "harga_awal"]
_MONTHLY_I = list(dict.fromkeys(["platform", "channel", "order_id", "created", "is_retur", "neto", "harga_produk", "refund"]
                                + [k for v in FEE_LINES.values() for k, _ in v] + [k for v in OTHER_LINES.values() for k, _ in v]))


def monthly(O, I, platform="all", m_from=None, m_to=None, U=None, ads=None, d_from=None, d_to=None):
    """Tabel per bulan + ringkasan. Semua bulan = bulan order dibuat. U = estimasi resmi TikTok (opsional)."""
    O = _cols(O, _MONTHLY_O)
    I = _cols(I, _MONTHLY_I)
    O = _channel_filter(O, platform)
    I = _channel_filter(I, platform)
    if not len(O) and not len(I):
        return dict(months=[], rows=[], fee_lines=[], other_lines=[], totals={}, coverage=[])

    # bulan income: ikut bulan order (dari file pesanan); kalau order tidak ada di file pesanan, pakai tanggal di income
    if len(I):
        I = I.copy()
        if len(O):
            om = O.drop_duplicates("order_id").set_index("order_id")["month"]
            I["month"] = I["order_id"].map(om)
        else:
            I["month"] = None
        I["month"] = I["month"].fillna(_ym(I["created"]))
        I["matched"] = I["order_id"].isin(set(O["order_id"])) if len(O) else False

    months = set()
    if len(O):
        months |= set(O["month"].dropna())
    if len(I):
        months |= set(I["month"].dropna())
    months = _month_range(months, m_from, m_to)

    plats = set(O["platform"]) if len(O) else set(I["platform"])
    fee_lines = []
    other_lines = []
    for p in (SHOPEE, TIKTOK):
        if p in plats:
            fee_lines += [(k, l) for k, l in FEE_LINES[p] if (k, l) not in fee_lines]
            other_lines += [(k, l) for k, l in OTHER_LINES[p] if (k, l) not in other_lines]

    # rasio neto/bruto-settle per (platform, bulan) → dasar taksiran order yang belum cair
    # (tanpa order yang dananya dikembalikan penuh: harga produknya masih tercantum di laporan tapi neto & biaya 0,
    #  kalau ikut dihitung rasio dan % potongan jadi terlalu rendah — temuan Farras 2026-11-02, TikTok 9,5% vs hitung manual 12,5%)
    ratio_pm, ratio_p = {}, {}
    if len(I):
        I_ok = I[~I["is_retur"]] if "is_retur" in I else I
        g = I_ok.groupby(["platform", "month"]).agg(n=("neto", "sum"), h=("harga_produk", "sum"), c=("order_id", "nunique"))
        for (pl, mo), x in g.iterrows():
            if x["h"] > 0 and x["c"] >= 30:
                ratio_pm[(pl, mo)] = x["n"] / x["h"]
        gp = I_ok.groupby("platform").agg(n=("neto", "sum"), h=("harga_produk", "sum"))
        for pl, x in gp.iterrows():
            if x["h"] > 0:
                ratio_p[pl] = x["n"] / x["h"]
    U_est = {}
    if U is not None and len(U):
        U_est = U.groupby("order_id")["est_neto"].sum().to_dict()

    # pecah per bulan sekali jalan (dulu: satu filter penuh per bulan)
    o_by = {k: g for k, g in O.groupby("month", sort=False)} if len(O) else {}
    i_by = {k: g for k, g in I.groupby("month", sort=False)} if len(I) else {}
    rows = []
    for m in months:
        o = o_by.get(m, O.iloc[:0])
        i = i_by.get(m, I.iloc[:0])
        r = dict(month=m)
        if len(o):
            live = o[~o["is_unpaid"]]
            sold = live[(~live["is_batal"]) & (~live["is_retur"])]
            canc = live[live["is_batal"]]
            ret = live[live["is_retur"]]
            r["retur_order"] = int(ret["order_id"].nunique())
            r["retur_nilai"] = float(ret["bruto"].sum())
            r["pesanan_masuk"] = int(live["order_id"].nunique())
            r["pesanan_batal"] = int(canc["order_id"].nunique())
            r["nilai_batal"] = float(canc["bruto"].sum())
            r["nilai_masuk"] = float(live["bruto"].sum())            # ≈ angka dashboard (belum dipotong batal)
            r["pesanan_terjual"] = int(sold["order_id"].nunique())
            r["qty_terjual"] = float(sold["qty"].sum())
            r["bruto"] = float(sold["bruto"].sum())
            if "hpp_unit" in sold:
                r["hpp"] = float((sold["qty"] * sold["hpp_unit"]).sum())
                r["qty_hpp"] = float(sold[sold["hpp_unit"] > 0]["qty"].sum())
            else:
                r["hpp"] = 0.0; r["qty_hpp"] = 0.0
            r["belum_bayar"] = int(o[o["is_unpaid"]]["order_id"].nunique())
            r["harga_awal"] = float(sold["harga_awal"].sum())
            sold_ids = set(sold["order_id"])
        else:
            for k in ("pesanan_masuk", "pesanan_batal", "nilai_batal", "nilai_masuk", "pesanan_terjual",
                      "qty_terjual", "bruto", "belum_bayar", "harga_awal", "retur_order", "retur_nilai", "hpp", "qty_hpp"):
                r[k] = 0
            sold_ids = set()
        if len(i):
            r["refund"] = float(i[~i["is_retur"]]["refund"].sum())
            r["refund_retur"] = float(i[i["is_retur"]]["refund"].sum())
            for k, _ in fee_lines:
                r[k] = float(i[k].sum()) if k in i else 0.0
            for k, _ in other_lines:
                r[k] = float(i[k].sum()) if k in i else 0.0
            r["total_potongan"] = float(sum(r[k] for k, _ in fee_lines))
            r["neto"] = float(i["neto"].sum())
            ok = i[~i["is_retur"]] if "is_retur" in i else i      # basis %: hanya order yang dananya tidak dikembalikan penuh
            r["bruto_income"] = float(ok["harga_produk"].sum())
            r["bruto_refund"] = float(i["harga_produk"].sum() - r["bruto_income"])
            r["order_settled"] = int(ok["order_id"].nunique())
            r["order_refund"] = int(i["order_id"].nunique() - r["order_settled"])
            settled_ids = set(i["order_id"])
        else:
            r["refund"] = 0.0
            r["refund_retur"] = 0.0
            for k, _ in fee_lines + other_lines:
                r[k] = 0.0
            r["total_potongan"] = r["neto"] = r["bruto_income"] = r["bruto_refund"] = 0.0
            r["order_settled"] = r["order_refund"] = 0
            settled_ids = set()
        # cakupan: order terjual yang belum muncul di laporan penghasilan
        if len(o):
            sold = o[(~o["is_unpaid"]) & (~o["is_batal"]) & (~o["is_retur"])]
            uns = sold[~sold["order_id"].isin(settled_ids)]
            r["belum_settle_order"] = int(uns["order_id"].nunique())
            r["belum_settle_bruto"] = float(uns["bruto"].sum())
            r["settle_pct"] = round(100.0 * (1 - r["belum_settle_bruto"] / r["bruto"]), 1) if r["bruto"] else 0.0
            # taksiran dana akan cair
            est, est_resmi, basis = 0.0, 0, set()
            for pl, grp in uns.groupby("platform"):
                per_order = grp.groupby("order_id")["bruto"].sum()
                for oid, b in per_order.items():
                    if oid in U_est:
                        est += U_est[oid]; est_resmi += 1
                    else:
                        rt = ratio_pm.get((pl, m))
                        if rt is None:
                            rt = ratio_p.get(pl)
                            basis.add(f"{pl}: rasio semua bulan")
                        else:
                            basis.add(f"{pl}: rasio {m}")
                        est += b * (rt if rt is not None else 0.0)
                        if rt is None:
                            basis.add(f"{pl}: belum ada laporan penghasilan → 0")
            r["est_neto"] = float(est)
            r["est_resmi_order"] = int(est_resmi)
            r["est_basis"] = "; ".join(sorted(basis))
        else:
            r["belum_settle_order"] = 0
            r["belum_settle_bruto"] = 0.0
            r["settle_pct"] = 0.0
            r["est_neto"] = 0.0
            r["est_resmi_order"] = 0
            r["est_basis"] = ""
        r["neto_proyeksi"] = r["neto"] + r["est_neto"]
        r["gross_profit"] = r["bruto"] - r["hpp"]
        r["gpm_pct"] = round(100.0 * r["gross_profit"] / r["bruto"], 1) if r["bruto"] else 0.0
        r["laba_mp"] = r["neto_proyeksi"] - r["hpp"]           # setelah potongan marketplace, sebelum iklan & opex
        r["laba_mp_pct"] = round(100.0 * r["laba_mp"] / r["bruto"], 1) if r["bruto"] else 0.0
        r["hpp_cover_pct"] = round(100.0 * r["qty_hpp"] / r["qty_terjual"], 1) if r["qty_terjual"] else 0.0
        if ads:
            r["iklan"], r["iklan_detail"] = adsmod.spend_for(ads, m, platform)
            if (d_from or d_to) and ads.get("prorate"):   # opsional: rentang tanggal parsial → iklan diprorata per hari
                fr = month_fraction(m, d_from, d_to)
                r["iklan"] *= fr
                r["iklan_detail"] = {k: v * fr for k, v in r["iklan_detail"].items()}
                r["iklan_prorata"] = round(fr * 100)
        else:
            r["iklan"], r["iklan_detail"] = 0.0, {}
        r["acr_pct"] = round(100.0 * r["iklan"] / r["bruto"], 1) if r["bruto"] else 0.0
        r["laba_iklan"] = r["laba_mp"] - r["iklan"]
        r["laba_iklan_pct"] = round(100.0 * r["laba_iklan"] / r["bruto"], 1) if r["bruto"] else 0.0
        r["batal_pct"] = round(100.0 * r["pesanan_batal"] / r["pesanan_masuk"], 1) if r["pesanan_masuk"] else 0.0
        r["potongan_pct"] = round(100.0 * -r["total_potongan"] / r["bruto_income"], 1) if r["bruto_income"] else 0.0
        r["neto_pct"] = round(100.0 * r["neto"] / r["bruto_income"], 1) if r["bruto_income"] else 0.0
        rows.append(r)

    totals = {}
    if rows:
        for k in rows[0]:
            if k == "month":
                continue
            if k.endswith("_pct") or k in ("est_basis", "iklan_detail"):
                continue
            totals[k] = sum(r[k] for r in rows)
        totals["iklan_detail"] = {}
        for r in rows:
            for c, v in r.get("iklan_detail", {}).items():
                totals["iklan_detail"][c] = totals["iklan_detail"].get(c, 0.0) + v
        totals["batal_pct"] = round(100.0 * totals["pesanan_batal"] / totals["pesanan_masuk"], 1) if totals["pesanan_masuk"] else 0
        totals["potongan_pct"] = round(100.0 * -totals["total_potongan"] / totals["bruto_income"], 1) if totals["bruto_income"] else 0
        totals["neto_pct"] = round(100.0 * totals["neto"] / totals["bruto_income"], 1) if totals["bruto_income"] else 0
        totals["settle_pct"] = round(100.0 * (1 - totals["belum_settle_bruto"] / totals["bruto"]), 1) if totals["bruto"] else 0
        totals["gpm_pct"] = round(100.0 * totals["gross_profit"] / totals["bruto"], 1) if totals["bruto"] else 0
        totals["laba_mp_pct"] = round(100.0 * totals["laba_mp"] / totals["bruto"], 1) if totals["bruto"] else 0
        totals["hpp_cover_pct"] = round(100.0 * totals["qty_hpp"] / totals["qty_terjual"], 1) if totals["qty_terjual"] else 0
        totals["acr_pct"] = round(100.0 * totals["iklan"] / totals["bruto"], 1) if totals["bruto"] else 0
        totals["laba_iklan_pct"] = round(100.0 * totals["laba_iklan"] / totals["bruto"], 1) if totals["bruto"] else 0
    totals["ratio_pm"] = {f"{k[0]} {k[1]}": round(v * 100, 1) for k, v in ratio_pm.items()}
    totals["ratio_p"] = {k: round(v * 100, 1) for k, v in ratio_p.items()}
    return dict(months=months, rows=rows, fee_lines=fee_lines, other_lines=other_lines, totals=totals)


def unsettled_by_status(O, I, platform="all", m_from=None, m_to=None, U=None):
    """Order terjual yang belum ada di laporan penghasilan, dipecah per status + taksiran dana."""
    O = _cols(O, ["platform", "channel", "order_id", "status", "is_unpaid", "is_batal", "is_retur", "month", "bruto", "created"])
    I = _cols(I, ["platform", "channel", "order_id", "created", "is_retur", "neto", "harga_produk"])
    O = _channel_filter(O, platform)
    I = _channel_filter(I, platform)
    if not len(O):
        return []
    O = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"])]
    O = _filter_months(O, "month", m_from, m_to)
    settled = set(I["order_id"]) if len(I) else set()
    uns = O[~O["order_id"].isin(settled)]
    if not len(uns):
        return []
    ratio_p, ratio_pm = {}, {}
    if len(I):
        I = I.copy()
        om = O.drop_duplicates("order_id").set_index("order_id")["month"]
        I["month"] = I["order_id"].map(om).fillna(_ym(I["created"]))
        if "is_retur" in I:
            I = I[~I["is_retur"]]      # rasio neto/bruto tanpa order refund penuh (neto 0, harga produk masih tercantum)
        gp = I.groupby("platform").agg(n=("neto", "sum"), h=("harga_produk", "sum"))
        ratio_p = {pl: (x["n"] / x["h"] if x["h"] > 0 else 0.0) for pl, x in gp.iterrows()}
        g = I.groupby(["platform", "month"]).agg(n=("neto", "sum"), h=("harga_produk", "sum"), c=("order_id", "nunique"))
        ratio_pm = {k: x["n"] / x["h"] for k, x in g.iterrows() if x["h"] > 0 and x["c"] >= 30}
    U_est = U.groupby("order_id")["est_neto"].sum().to_dict() if (U is not None and len(U)) else {}
    per = uns.groupby(["platform", "status", "order_id"]).agg(bruto=("bruto", "sum"), created=("created", "min"), month=("month", "first")).reset_index()
    per["est"] = [U_est.get(oid, b * ratio_pm.get((pl, mo), ratio_p.get(pl, 0.0))) for pl, oid, b, mo in zip(per["platform"], per["order_id"], per["bruto"], per["month"])]
    per["risk"] = per["status"].str.lower().str.contains("pembatalan|pengembalian|retur|return|belum")
    g = per.groupby(["platform", "status"]).agg(order=("order_id", "nunique"), bruto=("bruto", "sum"), est=("est", "sum"),
                                                 tertua=("created", "min"), risk=("risk", "first")).reset_index()
    g["tertua"] = g["tertua"].dt.strftime("%Y-%m-%d")
    return g.sort_values("bruto", ascending=False).to_dict("records")


def _filter_months(df, col, m_from, m_to):
    if m_from:
        df = df[df[col] >= m_from]
    if m_to:
        df = df[df[col] <= m_to]
    return df


def breakdowns(O, I, platform="all", m_from=None, m_to=None, top=40):
    O = _cols(O, ["platform", "channel", "order_id", "created", "month", "status", "is_unpaid", "is_batal", "is_retur", "product",
                  "variation", "qty", "bruto", "hpp_unit", "hpp_item", "cancel_reason", "payment"])
    O = _channel_filter(O, platform)
    if not len(O):
        return dict(products=[], status=[], cancel_reasons=[], payment=[], daily=[], channels=[])
    O = O[~O["is_unpaid"]]
    if m_from:
        O = O[O["month"] >= m_from]
    if m_to:
        O = O[O["month"] <= m_to]
    O = O.assign(status=O["status"].where(~O["is_retur"], "Retur penuh (dana dikembalikan, keluar dari bruto)"))
    sold = O[(~O["is_batal"]) & (~O["is_retur"])]
    canc = O[O["is_batal"]]

    # produk
    g = O.groupby("product")
    hpp_series = (sold["qty"] * sold["hpp_unit"]).groupby(sold["product"]).sum() if "hpp_unit" in sold else None
    prod = pd.DataFrame({
        "qty": sold.groupby("product")["qty"].sum(),
        "bruto": sold.groupby("product")["bruto"].sum(),
        "hpp": hpp_series,
        "hpp_item": sold.groupby("product")["hpp_item"].first() if "hpp_item" in sold else None,
        "order": sold.groupby("product")["order_id"].nunique(),
        "order_masuk": g["order_id"].nunique(),
        "nilai_batal": canc.groupby("product")["bruto"].sum(),
    }).fillna(0)
    prod["batal_pct"] = (100 * (prod["order_masuk"] - prod["order"]) / prod["order_masuk"]).round(1)
    prod["hpp"] = prod["hpp"].fillna(0)
    prod["gross_profit"] = prod["bruto"] - prod["hpp"]
    prod["gpm_pct"] = (100 * prod["gross_profit"] / prod["bruto"].replace(0, pd.NA)).fillna(0).round(1)
    prod["avg"] = (prod["bruto"] / prod["qty"].replace(0, pd.NA)).fillna(0).round(0)
    prod = prod.sort_values("bruto", ascending=False)
    tot = dict(n=int(len(prod)), order=float(prod["order"].sum()), qty=float(prod["qty"].sum()), bruto=float(prod["bruto"].sum()),
               hpp=float(prod["hpp"].sum()), gross_profit=float(prod["gross_profit"].sum()), order_masuk=float(prod["order_masuk"].sum()),
               nilai_batal=float(prod["nilai_batal"].sum()))
    tot["avg"] = round(tot["bruto"] / tot["qty"]) if tot["qty"] else 0
    tot["gpm_pct"] = round(100 * tot["gross_profit"] / tot["bruto"], 1) if tot["bruto"] else 0
    tot["batal_pct"] = round(100 * (tot["order_masuk"] - tot["order"]) / tot["order_masuk"], 1) if tot["order_masuk"] else 0
    tot["qty_hpp"] = float(sold[sold["hpp_unit"] > 0]["qty"].sum()) if "hpp_unit" in sold else 0.0
    if top:
        prod = prod.head(top)
    prod = prod.reset_index()
    products = prod.to_dict("records")
    # varian (warna/ukuran) per listing
    gv = O.groupby(["product", "variation"])
    sv = sold.groupby(["product", "variation"])
    cv = canc.groupby(["product", "variation"])
    var = pd.DataFrame({
        "qty": sv["qty"].sum(), "bruto": sv["bruto"].sum(), "order": sv["order_id"].nunique(),
        "hpp": (sold["qty"] * sold["hpp_unit"]).groupby([sold["product"], sold["variation"]]).sum() if "hpp_unit" in sold else None,
        "order_masuk": gv["order_id"].nunique(), "nilai_batal": cv["bruto"].sum(),
    }).fillna(0).reset_index()
    var["variation"] = var["variation"].replace("", "(tanpa varian)")
    var["gross_profit"] = var["bruto"] - var["hpp"]
    var["gpm_pct"] = (100 * var["gross_profit"] / var["bruto"].replace(0, pd.NA)).fillna(0).round(1)
    var["batal_pct"] = (100 * (var["order_masuk"] - var["order"]) / var["order_masuk"].replace(0, pd.NA)).fillna(0).round(1)
    var["avg"] = (var["bruto"] / var["qty"].replace(0, pd.NA)).fillna(0).round(0)
    var = var.sort_values(["product", "bruto"], ascending=[True, False])
    variants = {}
    for rec in var.to_dict("records"):
        variants.setdefault(rec["product"], []).append(rec)

    # status
    st = O.groupby("status").agg(order=("order_id", "nunique"), nilai=("bruto", "sum")).sort_values("order", ascending=False).reset_index()
    # alasan batal
    cr = canc[canc["cancel_reason"].ne("")].groupby("cancel_reason").agg(order=("order_id", "nunique"), nilai=("bruto", "sum")).sort_values("order", ascending=False).head(15).reset_index()
    # metode bayar: batal rate
    pm = O.groupby("payment").agg(masuk=("order_id", "nunique")).join(canc.groupby("payment").agg(batal=("order_id", "nunique"))).fillna(0)
    pm["batal_pct"] = (100 * pm["batal"] / pm["masuk"]).round(1)
    pm = pm.sort_values("masuk", ascending=False).head(12).reset_index()
    # harian
    d = sold.groupby(_ym(sold["created"], "D")).agg(bruto=("bruto", "sum"), order=("order_id", "nunique")).reset_index().rename(columns={"created": "date"})
    # channel split
    ch = O.groupby("channel").agg(masuk=("order_id", "nunique")).join(sold.groupby("channel").agg(terjual=("order_id", "nunique"), bruto=("bruto", "sum"))).fillna(0).reset_index()
    return dict(products=products, products_total=tot, variants=variants, status=st.to_dict("records"), cancel_reasons=cr.to_dict("records"),
                payment=pm.to_dict("records"), daily=d.to_dict("records"), channels=ch.to_dict("records"))


def adjustments(A, m_from=None, m_to=None):
    if not len(A):
        return []
    A = A.copy()
    A["month"] = _ym(A["settled"])
    if m_from:
        A = A[A["month"] >= m_from]
    if m_to:
        A = A[A["month"] <= m_to]
    g = A.groupby(["month", "type"]).agg(jumlah=("amount", "sum"), n=("id", "count")).reset_index()
    return g.sort_values(["month", "type"]).to_dict("records")


def reconcile(O, I):
    """Cek silang: bruto per order dari file pesanan vs 'harga produk' di laporan penghasilan."""
    if not len(O) or not len(I):
        return dict(matched=0, exact=0, mismatch=[])
    O = _cols(O, ["order_id", "is_unpaid", "is_batal", "is_retur", "bruto"])
    I = _cols(I, ["order_id", "harga_produk"])
    sold = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"])]
    ob = sold.groupby("order_id")["bruto"].sum()
    ib = I.groupby("order_id")["harga_produk"].sum()
    j = ob.to_frame("orders").join(ib.to_frame("income"), how="inner")
    j["diff"] = j["orders"] - j["income"]
    mm = j[j["diff"].abs() >= 1].sort_values("diff", key=abs, ascending=False).head(20).reset_index()
    return dict(matched=int(len(j)), exact=int((j["diff"].abs() < 1).sum()), mismatch=mm.to_dict("records"))


# ---------------------------------------------------------------- export excel

def export_produk_xlsx(toko, O, I, platform="all", m_from=None, m_to=None):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Produk"
    plat_label = {"all": "Gabungan", "shopee": "Shopee", "tiktok": "TikTok Shop", "tokopedia": "Tokopedia", "tiktok_all": "TikTok + Tokopedia"}.get(platform, platform)
    ws["A1"] = f"PRODUK — BRUTO TERJUAL, HPP, GROSS PROFIT & BATAL — {toko.upper()}"; ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Platform: {plat_label} · Periode (bulan order): {m_from or 'awal'} s.d. {m_to or 'akhir'} · order non-batal non-retur · HPP dari master toko"
    ws.append([])
    hdr = ["Produk (listing)", "Item HPP", "Order terjual", "Qty", "Bruto", "Rata-rata/pcs", "HPP", "Gross profit", "GPM %", "Order masuk", "% batal", "Nilai batal"]
    ws.append(hdr)
    for c in ws[4]:
        c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="EFE9FF")
    P = breakdowns(O, I, platform, m_from, m_to, top=None)["products"]
    for p in P:
        ws.append([p["product"], p.get("hpp_item") or "(belum dipetakan)", p["order"], p["qty"], p["bruto"], p["avg"], p["hpp"], p["gross_profit"],
                   (p["gpm_pct"] / 100.0) if p.get("hpp_item") else None, p["order_masuk"], p["batal_pct"] / 100.0, p["nilai_batal"]])
    n = ws.max_row
    ws.append(["TOTAL", "", f"=SUM(C5:C{n})", f"=SUM(D5:D{n})", f"=SUM(E5:E{n})", f"=IFERROR(E{n+1}/D{n+1},0)", f"=SUM(G5:G{n})", f"=SUM(H5:H{n})",
               f"=IFERROR(H{n+1}/E{n+1},0)", f"=SUM(J5:J{n})", f"=IFERROR(1-C{n+1}/J{n+1},0)", f"=SUM(L5:L{n})"])
    for c in ws[n + 1]:
        c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="FFF1EA")
    ws.column_dimensions["A"].width = 70; ws.column_dimensions["B"].width = 28
    for col, w in zip("CDEFGHIJKL", [13, 9, 15, 13, 14, 15, 9, 12, 9, 14]):
        ws.column_dimensions[col].width = w
    for r in range(5, n + 2):
        for col in "CDEFGHJL":
            ws[f"{col}{r}"].number_format = "#,##0"
        for col in "IK":
            ws[f"{col}{r}"].number_format = "0.0%"
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:L{n}"
    # sheet varian
    B = breakdowns(O, I, platform, m_from, m_to, top=None)
    wv = wb.create_sheet("Per varian")
    wv.append(["Produk (listing)", "Varian", "Order terjual", "Qty", "Bruto", "Rata-rata/pcs", "HPP", "Gross profit", "GPM %", "Order masuk", "% batal", "Nilai batal"])
    for c in wv[1]:
        c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="EFE9FF")
    for p in B["products"]:
        for v in B["variants"].get(p["product"], []):
            wv.append([p["product"], v["variation"], v["order"], v["qty"], v["bruto"], v["avg"], v["hpp"], v["gross_profit"],
                       (v["gpm_pct"] / 100.0) if p.get("hpp_item") else None, v["order_masuk"], v["batal_pct"] / 100.0, v["nilai_batal"]])
    wv.column_dimensions["A"].width = 70; wv.column_dimensions["B"].width = 26
    for col, w in zip("CDEFGHIJKL", [13, 9, 15, 13, 14, 15, 9, 12, 9, 14]):
        wv.column_dimensions[col].width = w
    for r in range(2, wv.max_row + 1):
        for col in "CDEFGHJL":
            wv[f"{col}{r}"].number_format = "#,##0"
        for col in "IK":
            wv[f"{col}{r}"].number_format = "0.0%"
    wv.freeze_panes = "A2"; wv.auto_filter.ref = f"A1:L{wv.max_row}"
    return wb

def export_xlsx(toko, O, I, m_from=None, m_to=None, U=None, ads=None, d_from=None, d_to=None):
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="DDDDDD")
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EFE9FF")
    tot_fill = PatternFill("solid", fgColor="FFF1EA")
    sections = [("Gabungan", "all"), ("Shopee", "shopee"), ("TikTok", "tiktok"), ("Tokopedia", "tokopedia")]
    for title, plat in sections:
        res = monthly(O, I, plat, m_from, m_to, U, ads, d_from, d_to)
        if not res["rows"]:
            continue
        ws = wb.create_sheet(title)
        months = res["months"]
        ws["A1"] = f"LAPORAN PENJUALAN {toko.upper()} — {title.upper()} — BASIS AKRUAL"
        ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = "Semua angka diakui di bulan order dibuat · biaya dicocokkan ke order-nya · pesanan batal & belum bayar tidak diakui · Rupiah" + (f" · rentang tanggal order {d_from or 'awal'} s.d. {d_to or 'akhir'} (iklan diprorata per hari)" if (d_from or d_to) else "")
        hdr = ["No", "Keterangan"] + months + ["TOTAL"]
        ws.append([]); ws.append(hdr)
        for c in ws[4]:
            c.font = bold; c.fill = head_fill
        lines = [
            ("1", "Pesanan masuk (jumlah order, incl. batal)", "pesanan_masuk"),
            ("2", "Nilai pesanan masuk (≈ angka dashboard)", "nilai_masuk"),
            ("3", "Pembatalan (jumlah order)", "pesanan_batal"),
            ("4", "Pembatalan (nilai)", "nilai_batal"),
            ("4b", "Retur penuh — dana dikembalikan, keluar dari bruto (jumlah order)", "retur_order"),
            ("4c", "Retur penuh (nilai)", "retur_nilai"),
            ("5", "Pesanan terjual (non-batal, non-retur)", "pesanan_terjual"),
            ("6", "PENJUALAN — Peredaran Bruto", "bruto"),
            ("", "Potongan Marketplace:", None),
        ]
        rows = res["rows"]; T = res["totals"]
        def put(no, label, key, neg=False, is_total=False):
            vals = [(-r[key] if neg else r[key]) if key else "" for r in rows]
            tot = ((-T[key] if neg else T[key]) if key else "")
            ws.append([no, label] + vals + [tot])
            if is_total or no in ("6", "8", "9") or label.startswith("PEN"):
                for c in ws[ws.max_row]:
                    c.font = bold
                    c.fill = tot_fill
        for no, label, key in lines:
            put(no, label, key)
        for n, (k, label) in enumerate(res["fee_lines"]):
            put(f"7{chr(97+n)}", "   " + label, k, neg=True)
        put("7", "TOTAL Potongan Marketplace", "total_potongan", neg=True, is_total=True)
        put("8", "Refund ke Pembeli (parsial, order yang tetap diakui)", "refund", neg=True)
        for k, label in res["other_lines"]:
            put("", "   " + label + " (+ tambah / − kurang)", k)
        put("9", "PENGHASILAN NETO (dana diterima)", "neto", is_total=True)
        ws.append([])
        put("", "Order terjual belum muncul di laporan penghasilan (jumlah)", "belum_settle_order")
        put("", "Order terjual belum muncul di laporan penghasilan (nilai bruto)", "belum_settle_bruto")
        put("", "TAKSIRAN dana akan cair dari order belum settle (rasio neto/bruto order yang sudah cair; TikTok: estimasi resmi bila ada)", "est_neto")
        put("10", "NETO PROYEKSI (dana diterima + taksiran)", "neto_proyeksi", is_total=True)
        ws.append([])
        ws.append(["", "HPP & LABA (HPP dari master per toko; hanya qty yang punya HPP)"])
        put("11", "HPP produk terjual", "hpp")
        put("12", "GROSS PROFIT (bruto − HPP)", "gross_profit", is_total=True)
        put("", "GPM % (gross profit / bruto)", "gpm_pct")
        put("13", "LABA SETELAH MARKETPLACE (neto proyeksi − HPP; sebelum iklan & opex)", "laba_mp", is_total=True)
        put("", "Cakupan HPP (% qty terjual yang punya HPP)", "hpp_cover_pct")
        ws.append([])
        ws.append(["", "IKLAN (input manual; atribusi channel ke platform sesuai tab Iklan)"])
        chans = sorted({c for r in rows for c in r.get("iklan_detail", {})})
        for n, c in enumerate(chans):
            ws.append([f"14{chr(97+n)}", "   " + c] + [r["iklan_detail"].get(c, 0) for r in rows] + [T["iklan_detail"].get(c, 0)])
        put("14", "TOTAL BIAYA IKLAN", "iklan", is_total=True)
        put("", "ACR % (iklan / bruto)", "acr_pct")
        put("15", "LABA SETELAH IKLAN (laba setelah marketplace − iklan; sebelum opex)", "laba_iklan", is_total=True)
        put("", "Laba setelah iklan % dari bruto", "laba_iklan_pct")
        ws.append([])
        ws.append(["CATATAN:"])
        ws.append(["1. Bruto = nilai produk setelah diskon penjual (Shopee: Subtotal Pesanan; TikTok: subtotal sebelum diskon − diskon penjual), hanya order non-batal, per bulan order."])
        ws.append(["1b. Order yang dananya dikembalikan penuh (refund ≥ 99% harga produk) diperlakukan sebagai retur: dikeluarkan dari bruto walau status pesanan bukan batal (aturan 2026-10-08)."])
        ws.append(["2. Potongan & refund diambil dari laporan penghasilan dan dipasangkan ke order-nya, lalu diakui di bulan order (bukan bulan dana cair)."])
        ws.append(["3. Neto untuk bulan yang belum 100% settle masih parsial — lihat baris 'belum muncul di laporan penghasilan'."])
        ws.column_dimensions["B"].width = 58
        for col in range(3, 3 + len(months) + 1):
            L = openpyxl.utils.get_column_letter(col)
            ws.column_dimensions[L].width = 16
            for r in range(5, ws.max_row + 1):
                ws[f"{L}{r}"].number_format = "#,##0"
                ws[f"{L}{r}"].alignment = Alignment(horizontal="right")
    return wb


# ---------------------------------------------------------------- pelanggan berulang (repeat purchase) per platform

CHANNEL_LABEL = {SHOPEE: "Shopee", TIKTOK: "TikTok Shop", TOKOPEDIA: "Tokopedia"}
ORDER_BUCKETS = [("1", 1, 1), ("2", 2, 2), ("3", 3, 3), ("4–5", 4, 5), ("6+", 6, 10**9)]
GAP_BUCKETS = [("≤ 7 hari", 0, 7), ("8–30 hari", 8, 30), ("31–60 hari", 31, 60), ("61–90 hari", 61, 90),
               ("91–180 hari", 91, 180), ("> 180 hari", 181, 10**9)]


def _buyer_orders(O):
    """Satu baris per ORDER terjual (non-batal, non-retur, sudah bayar) beserta identitas pembeli."""
    sold = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"])]
    if not len(sold):
        return pd.DataFrame(columns=["channel", "order_id", "buyer_key", "buyer", "created", "month", "bruto", "qty", "product"])
    if "buyer_key" not in sold:
        sold = sold.assign(buyer_key="", buyer="")
    sold = sold.assign(buyer_key=sold["buyer_key"].fillna("").astype(str), buyer=sold["buyer"].fillna("").astype(str))
    od = sold.groupby(["channel", "order_id"], sort=False).agg(
        buyer_key=("buyer_key", "first"), buyer=("buyer", "first"), created=("created", "min"), month=("month", "first"),
        bruto=("bruto", "sum"), qty=("qty", "sum"), product=("product", "first")).reset_index()
    return od


def _repeat_stats(x, lo):
    """Ringkasan satu kelompok order (sudah ada kolom seq, n_total, first_month, gap_days). lo = bulan awal periode."""
    if not len(x):
        return None
    per = x.groupby("ckey").agg(n=("order_id", "nunique"), n_total=("n_total", "first"), bruto=("bruto", "sum"),
                                first_month=("first_month", "first"))
    cust = int(len(per))
    orders = int(per["n"].sum())
    bruto = float(per["bruto"].sum())
    rep = per[per["n_total"] >= 2]
    ret_orders = x[x["seq"] > 1]
    gaps = ret_orders["gap_days"].dropna()
    new = per[per["first_month"] >= lo] if lo else per
    return dict(
        customers=cust, orders=orders, bruto=bruto,
        new_customers=int(len(new)), returning_customers=cust - int(len(new)),
        repeat_customers=int(len(rep)), repeat_pct=round(100 * len(rep) / cust, 1) if cust else 0.0,
        repeat_orders=int(len(ret_orders)), repeat_orders_pct=round(100 * len(ret_orders) / orders, 1) if orders else 0.0,
        repeat_bruto=float(ret_orders["bruto"].sum()), repeat_bruto_pct=round(100 * ret_orders["bruto"].sum() / bruto, 1) if bruto else 0.0,
        orders_per_customer=round(orders / cust, 2) if cust else 0.0,
        bruto_per_customer=round(bruto / cust) if cust else 0.0,
        aov=round(bruto / orders) if orders else 0.0,
        gap_median=float(gaps.median()) if len(gaps) else None,
        gap_avg=round(float(gaps.mean()), 1) if len(gaps) else None,
    )


HABIT_CATS = [("new", "Produk baru"), ("variant", "Warna/varian baru, produk sama"), ("same", "Item sama persis")]
SEQ_BUCKETS = [("ke-2", 2, 2), ("ke-3", 3, 3), ("ke-4–5", 4, 5), ("ke-6+", 6, 10**9)]
LAUNCH_QUIET_MONTHS = 3     # produk dianggap baru bila tidak ada penjualan di 1–3 bulan sebelumnya
LAUNCH_MIN_ORDERS = 3       # minimal order di bulan rilis supaya tidak menghitung produk sporadis
LAUNCH_WINDOW = 3           # pembeli dihitung di bulan rilis + 2 bulan berikutnya


def _norm_key(s):
    return s.fillna("").astype(str).str.strip().str.lower().str.replace(r"\s+", " ", regex=True)


_TITLE_SEP = re.compile(r"\s+[-–—|]\s+")


def product_names(titles):
    """Nama produk ringkas dari judul listing marketplace (Series) → Series nama.

    Judul dipecah pada ' - ', ' – ', ' | '. Segmen pertama yang pendek (≤ 3 kata) dan dipakai ≥ 15% judul (≥ 2 judul)
    dianggap prefix brand (mis. "Eltahj", "KARMEN") dan dibuang; nama produk = segmen berikutnya ("Eltahj – Runa Sport
    Hijab Basic - Kerudung …" → "Runa Sport Hijab Basic"). Jadi listing yang judulnya diganti/diuji A-B tetap satu produk
    selama segmen namanya sama. Boleh lebih dari satu brand di satu toko.
    """
    t = titles.fillna("").astype(str).str.strip()
    uniq = pd.Series(t.unique())
    segs = {v: [p.strip() for p in _TITLE_SEP.split(v) if p.strip()] or [v] for v in uniq}
    firsts = pd.Series([segs[v][0].lower() for v in uniq if len(segs[v]) > 1])
    vc = firsts.value_counts()
    brands = {b for b, n in vc.items() if n >= 2 and n >= 0.15 * len(uniq) and len(b.split()) <= 3}
    name = {}
    for v in uniq:
        l = segs[v]
        if len(l) > 1 and l[0].lower() in brands:
            l = l[1:]
        name[v] = l[0]
    return t.map(name)


def _habit_group(x, key):
    """Ringkasan kategori (new/variant/same) untuk satu kelompok order ulang."""
    n = int(len(x))
    out = dict(orders=n, bruto=float(x["bruto"].sum()))
    for cat, _ in HABIT_CATS:
        b = x[x["cat"] == cat]
        out[cat] = int(len(b))
        out[cat + "_pct"] = round(100 * len(b) / n, 1) if n else 0.0
        out[cat + "_bruto"] = float(b["bruto"].sum())
    out.update(key)
    return out


def repeat_habits(O, od, sel, top=20):
    """Kebiasaan order ulang: tiap order ke-2 dst dibandingkan dengan SEMUA item yang pernah dibeli pelanggan itu sebelumnya.

    Kategori per order diambil yang paling "baru": produk baru (ada item yang produknya belum pernah dibeli) >
    warna/varian baru (produk sudah pernah, variannya belum) > item sama persis (semua item pernah dibeli persis).
    Produk = nama listing di marketplace, varian = teks variasi (warna/ukuran) apa adanya; listing yang diganti nama akan
    terbaca sebagai produk baru. Riwayat dihitung dari semua data; laporan dibatasi order ulang yang ada di `sel` (periode).
    Mengembalikan juga `by_ckey` (kebiasaan per pelanggan sepanjang data) untuk daftar pelanggan teratas.
    """
    empty = dict(channels=[], totals=None, seq=[], products=[], new_products=[], pairs=[], has_variation=False, by_ckey={},
                 launches=dict(items=[], channels=[], totals=None))
    sold = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"])]
    if not len(sold) or not len(od):
        return empty
    L = sold[["channel", "order_id", "product", "qty", "bruto"]].copy()
    L["variation"] = sold["variation"] if "variation" in sold else ""
    L["product"] = product_names(L["product"])
    L["variation"] = L["variation"].fillna("").astype(str).str.strip()
    L["pkey"] = _norm_key(L["product"])
    L["vkey"] = L["pkey"] + "|" + _norm_key(L["variation"])
    L = L.merge(od[["channel", "order_id", "ckey", "seq", "month", "n_total"]], on=["channel", "order_id"], how="inner")
    if not len(L):
        return empty
    empty["has_variation"] = bool((L["variation"] != "").any())
    labels = {k: v for k, v in zip(zip(L["channel"], L["pkey"]), L["product"])}
    L = L.sort_values(["ckey", "seq", "order_id"]).reset_index(drop=True)
    in_sel = set(zip(sel["channel"], sel["order_id"]))

    orders, lines, pairs = [], [], {}
    seen_p = seen_v = None
    cur = None          # (ckey, order_id)
    items = []          # item di order yang sedang dibaca

    def flush(order):
        nonlocal items
        if order is None:
            return
        if order["seq"] > 1:
            n = dict(new=0, variant=0, same=0)
            bruto = 0.0
            newp = set()
            for pkey, vkey, product, qty, b in items:
                cat = "new" if pkey not in seen_p else ("variant" if vkey not in seen_v else "same")
                n[cat] += 1
                bruto += b
                lines.append(dict(channel=order["channel"], ckey=order["ckey"], order_id=order["order_id"], pkey=pkey, cat=cat, qty=qty, bruto=b))
                if cat == "new":
                    newp.add(pkey)
            cat = "new" if n["new"] else ("variant" if n["variant"] else "same")
            orders.append(dict(order, cat=cat, n_new=n["new"], n_variant=n["variant"], n_same=n["same"], bruto=bruto))
            if newp and (order["channel"], order["order_id"]) in in_sel:
                for np_ in newp:
                    for op in seen_p:
                        pairs.setdefault((order["channel"], op, np_), set()).add(order["ckey"])
        for pkey, vkey, *_ in items:
            seen_p.add(pkey)
            seen_v.add(vkey)
        items = []

    # pelanggan dengan 1 order tidak punya order ulang → tidak perlu ditelusuri (hasilnya sama, jauh lebih cepat)
    M = L[L["n_total"] > 1]
    for ckey, channel, order_id, seq, month, pkey, vkey, product, qty, bruto in zip(
            M["ckey"].tolist(), M["channel"].tolist(), M["order_id"].tolist(), M["seq"].tolist(), M["month"].tolist(),
            M["pkey"].tolist(), M["vkey"].tolist(), M["product"].tolist(), M["qty"].tolist(), M["bruto"].tolist()):
        if cur is None or ckey != cur["ckey"]:
            flush(cur)
            seen_p, seen_v = set(), set()
            cur = dict(ckey=ckey, channel=channel, order_id=order_id, seq=int(seq), month=month)
        elif order_id != cur["order_id"]:
            flush(cur)
            cur = dict(ckey=ckey, channel=channel, order_id=order_id, seq=int(seq), month=month)
        items.append((pkey, vkey, product, float(qty or 0), float(bruto or 0)))
    flush(cur)
    if not orders:
        return empty

    OD = pd.DataFrame(orders)
    LN = pd.DataFrame(lines)
    # kebiasaan per pelanggan (sepanjang data) untuk daftar pelanggan teratas
    bc = OD[["ckey"]].assign(n_new=OD["cat"].eq("new").astype(int), n_variant=OD["cat"].eq("variant").astype(int),
                             n_same=OD["cat"].eq("same").astype(int)).groupby("ckey").sum()
    empty["by_ckey"] = {k: dict(new=int(a), variant=int(b), same=int(c))
                        for k, a, b, c in zip(bc.index, bc["n_new"].tolist(), bc["n_variant"].tolist(), bc["n_same"].tolist())}

    mask = [(c, o) in in_sel for c, o in zip(OD["channel"], OD["order_id"])]
    OD = OD[mask]
    if not len(OD):
        return empty
    LN = LN[[(c, o) in in_sel for c, o in zip(LN["channel"], LN["order_id"])]]

    channels = []
    for ch in [c for c in (SHOPEE, TIKTOK, TOKOPEDIA) if c in set(OD["channel"])]:
        channels.append(_habit_group(OD[OD["channel"] == ch], dict(channel=ch, label=CHANNEL_LABEL.get(ch, ch))))
    totals = _habit_group(OD, dict(channel="all", label="Gabungan"))
    seq = [_habit_group(OD[(OD["seq"] >= lo) & (OD["seq"] <= hi)], dict(bucket=label)) for label, lo, hi in SEQ_BUCKETS]
    seq = [s for s in seq if s["orders"]]

    def prod_table(df, cats):
        d = df[df["cat"].isin(cats)]
        if not len(d):
            return []
        g = d.groupby(["channel", "pkey"]).agg(lines=("cat", "size"), customers=("ckey", "nunique"), qty=("qty", "sum"), bruto=("bruto", "sum"),
                                               same=("cat", lambda s: int((s == "same").sum())), variant=("cat", lambda s: int((s == "variant").sum())))
        g = g.sort_values(["customers", "lines"], ascending=False).head(top)
        return [dict(channel=CHANNEL_LABEL.get(ch, ch), product=labels.get((ch, pk), pk), lines=int(r.lines), customers=int(r.customers),
                     qty=float(r.qty), bruto=float(r.bruto), same=int(r.same), variant=int(r.variant)) for (ch, pk), r in g.iterrows()]

    products = prod_table(LN, ["same", "variant"])
    new_products = prod_table(LN, ["new"])
    pr = sorted(pairs.items(), key=lambda kv: -len(kv[1]))[:top]
    pair_rows = [dict(channel=CHANNEL_LABEL.get(ch, ch), src=labels.get((ch, a), a), dst=labels.get((ch, b), b), customers=len(cs)) for (ch, a, b), cs in pr]
    empty.update(channels=channels, totals=totals, seq=seq, products=products, new_products=new_products, pairs=pair_rows,
                 launches=product_launches(sold, L, sel))
    return empty


def product_launches(sold, L, sel):
    """Produk baru diluncurkan & siapa pembelinya (orang baru vs pelanggan lama).

    Produk (nama dari `product_names`) dianggap BARU di bulan M bila ada penjualan di M, tidak ada penjualan di M-1..M-3,
    data channel itu sudah mencakup M-1, dan order di M ≥ LAUNCH_MIN_ORDERS (semua order terjual, termasuk tanpa identitas).
    Pembeli dihitung dari pelanggan teridentifikasi di bulan rilis + 2 bulan berikutnya (dibatasi periode): tiap pelanggan
    sekali, status = order pertamanya membeli produk itu → "baru" bila itu order pertamanya di toko (seq 1), selain itu "lama".
    Hanya rilis yang bulannya masuk periode terpilih yang ditampilkan.
    """
    out = dict(items=[], channels=[], totals=None)
    if not len(sold) or not len(sel):
        return out
    PM = pd.DataFrame(dict(channel=sold["channel"].values, pkey=_norm_key(product_names(sold["product"])).values,
                           month=sold["month"].values, order_id=sold["order_id"].values))
    PM = PM[PM["month"].notna() & (PM["month"].astype(str) != "") & (PM["month"].astype(str) != "NaT")]
    if not len(PM):
        return out
    PM["month"] = PM["month"].astype(str)
    ch_start = PM.groupby("channel")["month"].min().to_dict()
    cnt = PM.groupby(["channel", "pkey", "month"])["order_id"].nunique()
    sel_months = sorted(set(sel["month"].dropna().astype(str)))
    if not sel_months:
        return out
    lo, hi = sel_months[0], sel_months[-1]
    labels = {k: v for k, v in zip(zip(L["channel"], L["pkey"]), L["product"])}

    launches = []
    for (ch, pk), g in cnt.groupby(level=[0, 1]):
        months = {m: int(n) for (_, _, m), n in g.items()}
        start = pd.Period(ch_start[ch], "M")
        for m in sorted(months):
            P = pd.Period(m, "M")
            if P <= start or m < lo or m > hi or months[m] < LAUNCH_MIN_ORDERS:
                continue
            if any(str(P - k) in months for k in range(1, LAUNCH_QUIET_MONTHS + 1)):
                continue
            launches.append((ch, pk, m, [str(P + k) for k in range(LAUNCH_WINDOW) if str(P + k) <= hi]))
    if not launches:
        return out

    items = []
    by_prod = {k: g for k, g in L.groupby(["channel", "pkey"], sort=False)}
    for ch, pk, m, window in launches:
        x = by_prod.get((ch, pk), L.iloc[:0])
        x = x[x["month"].astype(str).isin(window)]
        if not len(x):
            items.append(dict(channel=ch, label=CHANNEL_LABEL.get(ch, ch), product=labels.get((ch, pk), pk), month=m,
                              buyers=0, new=0, old=0, new_pct=0.0, old_pct=0.0, orders=0, bruto=0.0))
            continue
        first = x.sort_values("seq").drop_duplicates("ckey")
        new = int((first["seq"] == 1).sum())
        buyers = int(len(first))
        items.append(dict(channel=ch, label=CHANNEL_LABEL.get(ch, ch), product=labels.get((ch, pk), pk), month=m, buyers=buyers,
                          new=new, old=buyers - new, new_pct=round(100 * new / buyers, 1) if buyers else 0.0,
                          old_pct=round(100 * (buyers - new) / buyers, 1) if buyers else 0.0,
                          orders=int(x["order_id"].nunique()), bruto=float(x["bruto"].sum())))
    items.sort(key=lambda r: (r["month"], -r["buyers"]))
    items.reverse()

    def agg(rows, key):
        b = sum(r["buyers"] for r in rows)
        n = sum(r["new"] for r in rows)
        return dict(key, launches=len(rows), buyers=b, new=n, old=b - n, new_pct=round(100 * n / b, 1) if b else 0.0,
                    old_pct=round(100 * (b - n) / b, 1) if b else 0.0, orders=sum(r["orders"] for r in rows), bruto=sum(r["bruto"] for r in rows))

    channels = [agg([r for r in items if r["channel"] == ch], dict(channel=ch, label=CHANNEL_LABEL.get(ch, ch)))
                for ch in (SHOPEE, TIKTOK, TOKOPEDIA) if any(r["channel"] == ch for r in items)]
    out.update(items=items, channels=channels, totals=agg(items, dict(channel="all", label="Gabungan")))
    return out


def repeat_customers(O, platform="all", m_from=None, m_to=None, top=None):
    """Analisa transaksi berulang per pelanggan & per platform.

    Basis = order TERJUAL (non-batal, non-retur, sudah bayar) dari file pesanan. Identitas pelanggan: Shopee = username pembeli;
    TikTok/Tokopedia = pola username + telepon yang disamarkan TikTok (jadi bisa sedikit meleset). Pelanggan tidak bisa
    dicocokkan lintas platform (username berbeda), karena itu semua angka per channel; "gabungan" = penjumlahan.
    Urutan order per pelanggan dihitung dari SEMUA data toko, baru dibatasi ke periode terpilih → "pelanggan baru" =
    order pertamanya memang jatuh di periode itu.
    """
    O = _cols(O, ["platform", "channel", "order_id", "created", "month", "is_unpaid", "is_batal", "is_retur", "buyer_key", "buyer",
                  "bruto", "qty", "product", "variation"])
    O = _channel_filter(O, platform)
    empty = dict(channels=[], monthly=[], months=[], dist=[], gap=[], cohorts=[], cohort_cols=0, top=[], totals=None,
                 no_id=0, no_id_bruto=0.0, has_buyer=False, habits=None)
    if not len(O):
        return empty
    od = _buyer_orders(O)
    if not len(od):
        return empty
    empty["has_buyer"] = bool((od["buyer_key"] != "").any())
    noid = od[od["buyer_key"] == ""]
    od = od[od["buyer_key"] != ""].copy()
    if m_from:
        noid = noid[noid["month"] >= m_from]
    if m_to:
        noid = noid[noid["month"] <= m_to]
    empty["no_id"] = int(len(noid))
    empty["no_id_bruto"] = float(noid["bruto"].sum())
    if not len(od):
        return empty

    od["ckey"] = od["channel"] + "|" + od["buyer_key"]
    od = od.sort_values(["created", "order_id"]).reset_index(drop=True)
    g = od.groupby("ckey", sort=False)
    od["seq"] = g.cumcount() + 1
    od["n_total"] = g["order_id"].transform("count")
    od["first_month"] = _group_min_str(od["ckey"], od["month"])
    od["first_created"] = g["created"].transform("min")
    od["last_created"] = g["created"].transform("max")
    od["gap_days"] = (od["created"] - g["created"].shift()).dt.days

    sel = od
    if m_from:
        sel = sel[sel["month"] >= m_from]
    if m_to:
        sel = sel[sel["month"] <= m_to]
    if not len(sel):
        return empty
    months = sorted(m for m in sel["month"].dropna().unique() if m and m != "NaT")

    # per channel + gabungan
    channels = []
    for ch in [c for c in (SHOPEE, TIKTOK, TOKOPEDIA) if c in set(sel["channel"])]:
        s = _repeat_stats(sel[sel["channel"] == ch], m_from or (months[0] if months else None))
        s.update(channel=ch, label=CHANNEL_LABEL.get(ch, ch))
        channels.append(s)
    totals = _repeat_stats(sel, m_from or (months[0] if months else None))
    totals.update(channel="all", label="Gabungan")

    # per bulan: pelanggan baru vs lama (per bulan tiap pelanggan hanya salah satu: bulan order pertamanya = baru)
    monthly = []
    sel_by = {k: g for k, g in sel.groupby("month", sort=False)}
    for m in months:
        x = sel_by.get(m, sel.iloc[:0])
        per = x.groupby("ckey").agg(first_month=("first_month", "first"), bruto=("bruto", "sum"), n=("order_id", "nunique"))
        new = per[per["first_month"] == m]
        old = per[per["first_month"] != m]
        rep = x[x["seq"] > 1]
        monthly.append(dict(month=m, customers=int(len(per)), new=int(len(new)), returning=int(len(old)),
                            returning_pct=round(100 * len(old) / len(per), 1) if len(per) else 0.0,
                            orders=int(x["order_id"].nunique()), repeat_orders=int(len(rep)),
                            repeat_orders_pct=round(100 * len(rep) / x["order_id"].nunique(), 1) if len(x) else 0.0,
                            bruto=float(x["bruto"].sum()), bruto_new=float(new["bruto"].sum()), bruto_returning=float(old["bruto"].sum())))

    # distribusi jumlah order per pelanggan (sepanjang data) untuk pelanggan yang aktif di periode
    per_all = sel.groupby("ckey").agg(n_total=("n_total", "first"), bruto=("bruto", "sum"))
    dist = []
    for label, lo, hi in ORDER_BUCKETS:
        b = per_all[(per_all["n_total"] >= lo) & (per_all["n_total"] <= hi)]
        dist.append(dict(bucket=label, customers=int(len(b)), customers_pct=round(100 * len(b) / len(per_all), 1) if len(per_all) else 0.0,
                         bruto=float(b["bruto"].sum()), bruto_pct=round(100 * b["bruto"].sum() / per_all["bruto"].sum(), 1) if per_all["bruto"].sum() else 0.0))

    # jarak order ulang (hari sejak order sebelumnya) untuk order ke-2 dst di periode
    rep = sel[(sel["seq"] > 1) & sel["gap_days"].notna()]
    gap = []
    for label, lo, hi in GAP_BUCKETS:
        b = rep[(rep["gap_days"] >= lo) & (rep["gap_days"] <= hi)]
        gap.append(dict(bucket=label, orders=int(len(b)), orders_pct=round(100 * len(b) / len(rep), 1) if len(rep) else 0.0,
                        bruto=float(b["bruto"].sum())))
    second = od[od["seq"] == 2]["gap_days"].dropna()        # khusus jarak order ke-1 → ke-2 (sepanjang data)
    gap_second = dict(n=int(len(second)), median=float(second.median()) if len(second) else None,
                      p25=float(second.quantile(.25)) if len(second) else None, p75=float(second.quantile(.75)) if len(second) else None)

    # retensi kohort: kohort = bulan order pertama; kolom k = % pelanggan kohort yang order lagi k bulan kemudian
    cohorts = []
    cohort_cols = 0
    if months:
        act = od[["ckey", "month", "first_month"]].drop_duplicates(["ckey", "month"])
        act = act[act["month"].notna() & act["first_month"].notna()]
        try:
            def _mi(s):      # "YYYY-MM" → nomor bulan absolut
                s = s.astype(str)
                return s.str.slice(0, 4).astype(int) * 12 + s.str.slice(5, 7).astype(int)
            act = act.assign(k=(_mi(act["month"]) - _mi(act["first_month"])).astype(int))
        except Exception:
            act = act.assign(k=0)
        sizes = act.groupby("first_month")["ckey"].nunique()
        nk = act.groupby(["first_month", "k"])["ckey"].nunique()
        last = pd.Period(months[-1], "M")
        for cm in months:
            size = int(sizes.get(cm, 0))
            if not size:
                continue
            kmax = min(11, (last - pd.Period(cm, "M")).n)
            cohort_cols = max(cohort_cols, kmax + 1)
            row = dict(cohort=cm, size=size, pct=[])
            for k in range(kmax + 1):
                n = int(nk.get((cm, k), 0))
                row["pct"].append(round(100 * n / size, 1))
            cohorts.append(row)

    # kebiasaan order ulang: produk baru / warna-varian baru / item sama persis
    H = repeat_habits(O, od, sel)
    by_ckey = H.pop("by_ckey", {})

    # pelanggan teratas (aktif di periode): urut jumlah order sepanjang data lalu bruto periode
    per_top = sel.groupby("ckey").agg(channel=("channel", "first"), buyer=("buyer", "first"), orders=("order_id", "nunique"),
                                      n_total=("n_total", "first"), qty=("qty", "sum"), bruto=("bruto", "sum"),
                                      first=("first_created", "first"), last=("last_created", "first"))
    per_top = per_top[per_top["n_total"] >= 2].sort_values(["n_total", "bruto"], ascending=[False, False])
    if top:
        per_top = per_top.head(top)
    top_rows = []
    if len(per_top):
        # produk paling sering per pelanggan: hitung (ckey, produk) sekali, ambil yang terbanyak
        pc = od[od["ckey"].isin(per_top.index)].groupby(["ckey", "product"]).size().reset_index(name="n")
        pc = pc.sort_values(["ckey", "n"], ascending=[True, False]).drop_duplicates("ckey")
        prods = pc.set_index("ckey")["product"]
        for key, r in per_top.iterrows():
            span = (r["last"] - r["first"]).days if pd.notna(r["last"]) and pd.notna(r["first"]) else None
            top_rows.append(dict(channel=CHANNEL_LABEL.get(r["channel"], r["channel"]), buyer=r["buyer"] or "(tanpa nama)",
                                 orders=int(r["orders"]), orders_total=int(r["n_total"]), qty=float(r["qty"]), bruto=float(r["bruto"]),
                                 first=str(r["first"])[:10], last=str(r["last"])[:10], span_days=span,
                                 gap_avg=round(span / (r["n_total"] - 1), 1) if span is not None and r["n_total"] > 1 else None,
                                 product=prods.get(key, ""), habit=by_ckey.get(key)))

    empty.update(channels=channels, totals=totals, monthly=monthly, months=months, dist=dist, gap=gap, gap_second=gap_second,
                 cohorts=cohorts, cohort_cols=cohort_cols, top=top_rows, habits=H)
    return empty


def export_repeat_xlsx(toko, O, platform="all", m_from=None, m_to=None):
    """Excel analisa pelanggan berulang: ringkasan per platform, per bulan, distribusi, kohort, dan SEMUA pelanggan berulang."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    R = repeat_customers(O, platform, m_from, m_to)
    wb = Workbook()
    bold = Font(bold=True)

    def sheet(title, header, rows, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(header)
        for c in ws[1]:
            c.font = bold
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        return ws

    keys = ["label", "customers", "new_customers", "returning_customers", "repeat_customers", "repeat_pct", "orders", "repeat_orders",
            "repeat_orders_pct", "bruto", "repeat_bruto", "repeat_bruto_pct", "orders_per_customer", "bruto_per_customer", "aov", "gap_median", "gap_avg"]
    head = ["Platform", "Pelanggan aktif", "Pelanggan baru", "Pelanggan lama", "Pernah beli ≥2×", "% pernah beli ≥2×", "Order", "Order ulang",
            "% order ulang", "Bruto", "Bruto order ulang", "% bruto order ulang", "Order/pelanggan", "Bruto/pelanggan", "Rata-rata/order",
            "Median jarak order (hari)", "Rata-rata jarak (hari)"]
    rows = [[s.get(k) for k in keys] for s in R["channels"]] + ([[R["totals"].get(k) for k in keys]] if R["totals"] else [])
    sheet("Per platform", head, rows, first=True)
    sheet("Per bulan", ["Bulan", "Pelanggan aktif", "Baru", "Lama", "% lama", "Order", "Order ulang", "% order ulang", "Bruto", "Bruto baru", "Bruto lama"],
          [[m["month"], m["customers"], m["new"], m["returning"], m["returning_pct"], m["orders"], m["repeat_orders"], m["repeat_orders_pct"],
            m["bruto"], m["bruto_new"], m["bruto_returning"]] for m in R["monthly"]])
    sheet("Distribusi order", ["Jumlah order (sepanjang data)", "Pelanggan", "% pelanggan", "Bruto periode", "% bruto"],
          [[d["bucket"], d["customers"], d["customers_pct"], d["bruto"], d["bruto_pct"]] for d in R["dist"]])
    sheet("Jarak order ulang", ["Jarak sejak order sebelumnya", "Order ulang", "% order ulang", "Bruto"],
          [[g["bucket"], g["orders"], g["orders_pct"], g["bruto"]] for g in R["gap"]])
    sheet("Kohort retensi %", ["Kohort (bulan order pertama)", "Pelanggan"] + [f"+{k} bln" for k in range(R["cohort_cols"])],
          [[c["cohort"], c["size"]] + c["pct"] for c in R["cohorts"]])
    H = R.get("habits") or {}
    hk = ["orders", "new", "new_pct", "new_bruto", "variant", "variant_pct", "variant_bruto", "same", "same_pct", "same_bruto"]
    hh = ["Order ulang", "Produk baru", "% produk baru", "Bruto produk baru", "Warna/varian baru", "% warna/varian baru", "Bruto warna/varian baru",
          "Item sama persis", "% item sama", "Bruto item sama"]
    sheet("Kebiasaan order ulang", ["Platform / urutan order"] + hh,
          [[s["label"]] + [s.get(k) for k in hk] for s in H.get("channels", [])]
          + ([[H["totals"]["label"]] + [H["totals"].get(k) for k in hk]] if H.get("totals") else [])
          + [["Order " + s["bucket"]] + [s.get(k) for k in hk] for s in H.get("seq", [])])
    sheet("Produk dibeli ulang", ["Platform", "Produk", "Pelanggan", "Item dibeli ulang", "Item sama persis", "Warna/varian baru", "Qty", "Bruto"],
          [[p["channel"], p["product"], p["customers"], p["lines"], p["same"], p["variant"], p["qty"], p["bruto"]] for p in H.get("products", [])])
    sheet("Produk baru pelanggan lama", ["Platform", "Produk", "Pelanggan", "Item", "Qty", "Bruto"],
          [[p["channel"], p["product"], p["customers"], p["lines"], p["qty"], p["bruto"]] for p in H.get("new_products", [])])
    sheet("Lanjut beli", ["Platform", "Pernah beli", "Lalu beli (produk baru)", "Pelanggan"],
          [[p["channel"], p["src"], p["dst"], p["customers"]] for p in H.get("pairs", [])])
    LA = H.get("launches") or {}
    lk = ["launches", "buyers", "new", "new_pct", "old", "old_pct", "orders", "bruto"]
    sheet("Produk baru rilis", ["Platform / produk", "Bulan rilis", "Produk baru", "Pembeli", "Orang baru", "% orang baru", "Pelanggan lama", "% pelanggan lama", "Order", "Bruto"],
          [[c["label"], ""] + [c.get(k) for k in lk] for c in LA.get("channels", [])]
          + ([[LA["totals"]["label"], ""] + [LA["totals"].get(k) for k in lk]] if LA.get("totals") else [])
          + [[f'{r["label"]}: {r["product"]}', r["month"], 1, r["buyers"], r["new"], r["new_pct"], r["old"], r["old_pct"], r["orders"], r["bruto"]] for r in LA.get("items", [])])
    hab = lambda h: "" if not h else f'{h["new"]} produk baru · {h["variant"]} warna/varian baru · {h["same"]} item sama'
    sheet("Pelanggan berulang", ["Platform", "Pelanggan", "Order di periode", "Order sepanjang data", "Qty", "Bruto periode", "Order pertama",
                                 "Order terakhir", "Rentang (hari)", "Rata-rata jarak (hari)", "Produk paling sering", "Kebiasaan order ulang"],
          [[t["channel"], t["buyer"], t["orders"], t["orders_total"], t["qty"], t["bruto"], t["first"], t["last"], t["span_days"], t["gap_avg"], t["product"], hab(t.get("habit"))]
           for t in R["top"]])
    return wb


# ---------------------------------------------------------------- demografi wilayah pembeli (provinsi / kota / pulau)

REGIONS = [
    ("Jawa", ["DKI Jakarta", "Jawa Barat", "Banten", "Jawa Tengah", "DI Yogyakarta", "Jawa Timur"]),
    ("Sumatera", ["Aceh", "Sumatera Utara", "Sumatera Barat", "Riau", "Kepulauan Riau", "Jambi", "Sumatera Selatan",
                  "Kepulauan Bangka Belitung", "Bengkulu", "Lampung"]),
    ("Kalimantan", ["Kalimantan Barat", "Kalimantan Tengah", "Kalimantan Selatan", "Kalimantan Timur", "Kalimantan Utara"]),
    ("Sulawesi", ["Sulawesi Utara", "Gorontalo", "Sulawesi Tengah", "Sulawesi Barat", "Sulawesi Selatan", "Sulawesi Tenggara"]),
    ("Bali & Nusa Tenggara", ["Bali", "Nusa Tenggara Barat", "Nusa Tenggara Timur"]),
    ("Maluku & Papua", ["Maluku", "Maluku Utara", "Papua", "Papua Barat", "Papua Tengah", "Papua Pegunungan", "Papua Selatan", "Papua Barat Daya"]),
]
REGION_OF = {p: r for r, ps in REGIONS for p in ps}
REGION_OTHER = "Tidak dikenali"
JABODETABEK = {"Kota Bogor", "Kab. Bogor", "Kota Depok", "Kota Tangerang", "Kab. Tangerang", "Kota Tangerang Selatan", "Kota Bekasi", "Kab. Bekasi"}
AREA_LABELS = ["Jabodetabek", "Jawa di luar Jabodetabek", "Luar Jawa"]
GEO_TOP_PROV_CHART = 8      # provinsi yang digambar sendiri di grafik per bulan (sisanya "Lainnya")
GEO_TOP_PROV_PRODUCT = 10   # provinsi untuk tabel produk favorit
GEO_TOP_PRODUCT = 5         # produk per provinsi
GEO_MATRIX_PRODUCTS = 12    # produk di matriks produk × provinsi


def _area_of(province, city):
    if province == "DKI Jakarta" or city in JABODETABEK:
        return AREA_LABELS[0]
    if REGION_OF.get(province) == "Jawa":
        return AREA_LABELS[1]
    return AREA_LABELS[2]


def _geo_orders(O):
    """Satu baris per ORDER terjual (non-batal, non-retur, sudah bayar) beserta provinsi, kota, dan pembeli."""
    sold = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"])]
    cols = ["channel", "order_id", "province", "city", "district", "buyer_key", "created", "month", "bruto", "qty", "product"]
    if not len(sold):
        return pd.DataFrame(columns=cols)
    for c in ("province", "city", "district", "buyer_key"):
        sold = sold.assign(**{c: sold[c].fillna("").astype(str) if c in sold else ""})
    # baris yang punya wilayah didahulukan supaya 'first' mengambil nilai yang terisi
    sold = sold.assign(_noprov=(sold["province"] == "")).sort_values("_noprov", kind="stable")
    od = sold.groupby(["channel", "order_id"], sort=False).agg(
        province=("province", "first"), city=("city", "first"), district=("district", "first"), buyer_key=("buyer_key", "first"), created=("created", "min"),
        month=("month", "first"), bruto=("bruto", "sum"), qty=("qty", "sum"), product=("product", "first")).reset_index()
    return od


_CITY_TYPE = re.compile(r"^(Kota|Kab\.) ")


def _merge_bare_cities(od):
    """Kota tanpa jenis ('Bogor', dari export TikTok lama) digabung ke 'Kota Bogor'/'Kab. Bogor' bila di provinsi itu hanya ada satu;
    kalau dua-duanya ada (kota & kabupaten) tetap dipisah dengan tanda '(?)' karena tidak bisa dipastikan."""
    if not len(od):
        return od
    typed = od[od["city"].str.match(_CITY_TYPE)]
    bare = od[(od["city"] != "") & ~od["city"].str.match(_CITY_TYPE)]
    if not len(bare):
        return od
    cand = typed.assign(base=typed["city"].str.replace(_CITY_TYPE, "", regex=True)).groupby(["province", "base"])["city"].agg(lambda s: sorted(set(s)))
    mp = {}
    for prov, name in bare[["province", "city"]].drop_duplicates().itertuples(index=False):
        c = cand.get((prov, name))
        mp[(prov, name)] = c[0] if c is not None and len(c) == 1 else name + " (?)"
    key = list(zip(od["province"], od["city"]))
    od = od.assign(city=[mp.get(k, k[1]) for k in key])
    return od


def _geo_group(df, by, total_orders, total_bruto, label_col=None):
    """Agregasi per wilayah → list dict terurut jumlah order."""
    if not len(df):
        return []
    # _oid / _ck = kode angka order & pelanggan (dibuat di geo_customers) → nunique jauh lebih cepat daripada teks; hasil sama
    oid = "_oid" if "_oid" in df else "order_id"
    g = df.groupby(by, sort=False)
    rows = g.agg(orders=(oid, "nunique"), bruto=("bruto", "sum"), qty=("qty", "sum"))
    ck = df["_ck"] if "_ck" in df else df["ckey"].where(df["ckey"] != "")
    rows["customers"] = ck.groupby([df[c] for c in by], sort=False).nunique().reindex(rows.index).fillna(0).astype(int)
    for ch, key in ((SHOPEE, "shopee"), (TIKTOK, "tiktok"), (TOKOPEDIA, "tokopedia")):
        rows[key] = df[df["channel"] == ch].groupby(by)[oid].nunique().reindex(rows.index).fillna(0).astype(int)
    # pelanggan yang beli ≥2× di periode & wilayah ini
    per = df[df["ckey"] != ""].groupby(by + (["_ck"] if "_ck" in df else ["ckey"]))[oid].nunique()
    rep = per[per >= 2].groupby(level=list(range(len(by)))).size() if len(per) else pd.Series(dtype=int)
    rows["repeat_customers"] = rep.reindex(rows.index).fillna(0).astype(int)
    rows = rows.sort_values(["orders", "bruto"], ascending=[False, False])
    out = []
    for idx, r in rows.iterrows():
        idx = idx if isinstance(idx, tuple) else (idx,)
        d = dict(zip(by, idx))
        o, b = int(r["orders"]), float(r["bruto"])
        d.update(orders=o, orders_pct=round(100 * o / total_orders, 1) if total_orders else 0.0,
                 bruto=b, bruto_pct=round(100 * b / total_bruto, 1) if total_bruto else 0.0,
                 qty=float(r["qty"]), customers=int(r["customers"]), aov=round(b / o) if o else 0.0,
                 shopee=int(r["shopee"]), tiktok=int(r["tiktok"]), tokopedia=int(r["tokopedia"]),
                 repeat_customers=int(r["repeat_customers"]),
                 repeat_pct=round(100 * float(r["repeat_customers"]) / float(r["customers"]), 1) if r["customers"] else 0.0)
        out.append(d)
    return out


GEO_TOP_DISTRICT = 100      # kecamatan teratas di tab (Excel memuat semua)


def geo_customers(O, platform="all", m_from=None, m_to=None, top_city=60, top_district=GEO_TOP_DISTRICT):
    """Demografi wilayah pembeli: sebaran order/bruto/pelanggan per pulau, kawasan, provinsi, kota; tren bulanan; produk favorit per provinsi.

    Basis = order TERJUAL (non-batal, non-retur, sudah bayar) dari file pesanan, dibatasi periode. Wilayah = alamat pengiriman
    (kolom Provinsi & Kota/Kabupaten di Order.all Shopee; Province & Regency and City di Semua Pesanan TikTok — export TikTok
    sebelum ±Nov 2025 tidak memuat kolom itu → order tanpa wilayah dilaporkan terpisah). Pelanggan = kunci pembeli per channel
    (tidak bisa dicocokkan lintas platform); provinsi/kota diambil dari order, jadi pelanggan yang pindah alamat dihitung di
    tiap wilayah yang pernah ia pakai.
    """
    O = _cols(O, ["platform", "channel", "order_id", "is_unpaid", "is_batal", "is_retur", "province", "city", "district", "buyer_key",
                  "created", "month", "bruto", "qty", "product"])
    O = _channel_filter(O, platform)
    empty = dict(totals=None, regions=[], areas=[], provinces=[], cities=[], districts=[], months=[], monthly=[], monthly_provinces=[],
                 products=[], matrix=None, has_geo=False, no_geo=0, no_geo_bruto=0.0)
    if not len(O):
        return empty
    od = _geo_orders(O)
    if m_from:
        od = od[od["month"] >= m_from]
    if m_to:
        od = od[od["month"] <= m_to]
    if not len(od):
        return empty
    empty["has_geo"] = bool((od["province"] != "").any())
    nogeo = od[od["province"] == ""]
    empty["no_geo"] = int(len(nogeo))
    empty["no_geo_bruto"] = float(nogeo["bruto"].sum())
    od = od[od["province"] != ""].copy()
    if not len(od):
        return empty
    od = _merge_bare_cities(od)
    od["ckey"] = (od["channel"] + "|" + od["buyer_key"]).where(od["buyer_key"] != "", "")
    od["_oid"] = pd.factorize(od["order_id"])[0]
    _c = pd.factorize(od["ckey"])[0].astype(float)
    od["_ck"] = np.where(od["ckey"].to_numpy() == "", np.nan, _c)
    od["region"] = od["province"].map(REGION_OF).fillna(REGION_OTHER)
    pc = list(zip(od["province"], od["city"]))
    amap = {k: _area_of(*k) for k in set(pc)}
    od["area"] = [amap[k] for k in pc]
    orders = int(od["order_id"].nunique())
    bruto = float(od["bruto"].sum())
    customers = int(od.loc[od["ckey"] != "", "ckey"].nunique())
    all_orders = orders + empty["no_geo"]
    totals = dict(orders=orders, bruto=bruto, qty=float(od["qty"].sum()), customers=customers, aov=round(bruto / orders) if orders else 0.0,
                  provinces=int(od["province"].nunique()), cities=int(od.loc[od["city"] != "", "city"].nunique()),
                  coverage_pct=round(100 * orders / all_orders, 1) if all_orders else 0.0)

    regions = _geo_group(od, ["region"], orders, bruto)
    order_r = {r: i for i, (r, _) in enumerate(REGIONS)}
    regions.sort(key=lambda r: order_r.get(r["region"], 99))
    areas = _geo_group(od, ["area"], orders, bruto)
    areas.sort(key=lambda r: AREA_LABELS.index(r["area"]) if r["area"] in AREA_LABELS else 99)
    provinces = _geo_group(od, ["province"], orders, bruto)
    for p in provinces:
        p["region"] = REGION_OF.get(p["province"], REGION_OTHER)
    with_city = od[od["city"] != ""]
    cities = _geo_group(with_city, ["province", "city"], orders, bruto)
    totals["cities_total"] = len(cities)
    cities = cities[:top_city] if top_city else cities
    # kecamatan: hanya order yang alamatnya memuat kecamatan (Shopee; TikTok menyamarkannya) → % dihitung terhadap order berkecamatan
    with_dist = od[od["district"] != ""]
    d_orders, d_bruto = int(with_dist["order_id"].nunique()), float(with_dist["bruto"].sum())
    districts = _geo_group(with_dist, ["province", "city", "district"], d_orders, d_bruto)
    totals.update(districts_total=len(districts), district_orders=d_orders, district_bruto=d_bruto,
                  district_coverage_pct=round(100 * d_orders / orders, 1) if orders else 0.0,
                  district_shopee_pct=round(100 * int(with_dist[with_dist["channel"] == SHOPEE]["order_id"].nunique())
                                            / max(1, int(od[od["channel"] == SHOPEE]["order_id"].nunique())), 1))
    districts = districts[:top_district] if top_district else districts

    # per bulan: order & bruto per provinsi teratas (+ Lainnya) → grafik komposisi
    months = sorted(m for m in od["month"].dropna().unique() if m and m != "NaT")
    top_prov = [p["province"] for p in provinces[:GEO_TOP_PROV_CHART]]
    od["prov_chart"] = od["province"].where(od["province"].isin(top_prov), "Lainnya")
    monthly = []
    od_by_month = {k: g for k, g in od.groupby("month", sort=False)}
    for m in months:
        x = od_by_month.get(m, od.iloc[:0])
        bo = x.groupby("prov_chart")["order_id"].nunique()
        bb = x.groupby("prov_chart")["bruto"].sum()
        monthly.append(dict(month=m, orders=int(x["order_id"].nunique()), bruto=float(x["bruto"].sum()),
                            customers=int(x.loc[x["ckey"] != "", "ckey"].nunique()), provinces=int(x["province"].nunique()),
                            by_orders={k: int(v) for k, v in bo.items()}, by_bruto={k: float(v) for k, v in bb.items()}))
    monthly_provinces = top_prov + (["Lainnya"] if (od["prov_chart"] == "Lainnya").any() else [])

    # produk favorit per provinsi: dari BARIS item (satu order bisa berisi beberapa produk), provinsi diambil dari order-nya;
    # nama produk dikelompokkan dari judul listing seperti tab Pelanggan berulang
    sold = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"])][["channel", "order_id", "product", "bruto"]]
    L = sold.merge(od[["channel", "order_id", "province", "prov_chart"]], on=["channel", "order_id"], how="inner")
    L["pname"] = product_names(L["product"])
    products = []
    L_by_prov = {k: g for k, g in L.groupby("province", sort=False)}
    n_by_prov = od.groupby("province")["order_id"].nunique()
    for prov in [p["province"] for p in provinces[:GEO_TOP_PROV_PRODUCT]]:
        x = L_by_prov.get(prov, L.iloc[:0])
        n = int(n_by_prov.get(prov, 0))
        g = x.groupby("pname").agg(orders=("order_id", "nunique"), bruto=("bruto", "sum")).sort_values(["orders", "bruto"], ascending=[False, False])
        for name, r in g.head(GEO_TOP_PRODUCT).iterrows():
            products.append(dict(province=prov, product=name, orders=int(r["orders"]), bruto=float(r["bruto"]),
                                 pct=round(100 * float(r["orders"]) / n, 1) if n else 0.0))

    # matriks produk × provinsi: % order produk itu yang jatuh di tiap provinsi, dibanding sebaran semua order (baseline)
    matrix = None
    if len(top_prov):
        gp = L.groupby("pname")["order_id"].nunique().sort_values(ascending=False).head(GEO_MATRIX_PRODUCTS)
        base = {p: round(100 * int(n_by_prov.get(p, 0)) / orders, 1) for p in top_prov}
        base["Lainnya"] = round(100 - sum(base.values()), 1) if "Lainnya" in monthly_provinces else None
        rows = []
        Lt = L[L["pname"].isin(set(gp.index))]
        kk = Lt.groupby(["pname", "prov_chart"])["order_id"].nunique()
        for name, n in gp.items():
            share = {}
            for p in monthly_provinces:
                k = int(kk.get((name, p), 0))
                share[p] = round(100 * k / n, 1) if n else 0.0
            rows.append(dict(product=name, orders=int(n), share=share))
        matrix = dict(provinces=monthly_provinces, baseline={p: base.get(p) for p in monthly_provinces}, rows=rows)

    empty.update(totals=totals, regions=regions, areas=areas, provinces=provinces, cities=cities, districts=districts, months=months,
                 monthly=monthly, monthly_provinces=monthly_provinces, products=products, matrix=matrix)
    return empty


def export_geo_xlsx(toko, O, platform="all", m_from=None, m_to=None):
    """Excel demografi wilayah: pulau, kawasan, SEMUA provinsi & kota, per bulan, produk favorit per provinsi, matriks produk × provinsi."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    G = geo_customers(O, platform, m_from, m_to, top_city=0, top_district=0)
    wb = Workbook()
    bold = Font(bold=True)

    def sheet(title, header, rows, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(header)
        for c in ws[1]:
            c.font = bold
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        return ws

    K = ["orders", "orders_pct", "bruto", "bruto_pct", "qty", "customers", "aov", "shopee", "tiktok", "tokopedia", "repeat_customers", "repeat_pct"]
    H = ["Order", "% order", "Bruto", "% bruto", "Qty", "Pelanggan", "Rata-rata/order", "Order Shopee", "Order TikTok", "Order Tokopedia",
         "Pelanggan beli ≥2×", "% beli ≥2×"]
    T = G["totals"] or {}
    sheet("Ringkasan", ["Keterangan", "Nilai"],
          [["Order terjual dengan wilayah", T.get("orders")], ["Bruto", T.get("bruto")], ["Pelanggan", T.get("customers")],
           ["Provinsi", T.get("provinces")], ["Kota/kabupaten", T.get("cities")], ["Kecamatan", T.get("districts_total")],
           ["% order yang punya wilayah", T.get("coverage_pct")],
           ["Order yang punya kecamatan (Shopee saja; TikTok menyamarkan)", T.get("district_orders")], ["% order yang punya kecamatan", T.get("district_coverage_pct")],
           ["Order tanpa wilayah (export lama)", G["no_geo"]], ["Bruto tanpa wilayah", G["no_geo_bruto"]]], first=True)
    sheet("Pulau", ["Pulau"] + H, [[r["region"]] + [r.get(k) for k in K] for r in G["regions"]])
    sheet("Kawasan", ["Kawasan"] + H, [[r["area"]] + [r.get(k) for k in K] for r in G["areas"]])
    sheet("Provinsi", ["Provinsi", "Pulau"] + H, [[r["province"], r["region"]] + [r.get(k) for k in K] for r in G["provinces"]])
    sheet("Kota", ["Kota/Kabupaten", "Provinsi"] + H, [[r["city"], r["province"]] + [r.get(k) for k in K] for r in G["cities"]])
    sheet("Kecamatan", ["Kecamatan", "Kota/Kabupaten", "Provinsi"] + H, [[r["district"], r["city"], r["province"]] + [r.get(k) for k in K] for r in G["districts"]])
    MP = G["monthly_provinces"]
    sheet("Per bulan (order)", ["Bulan", "Order", "Bruto", "Pelanggan", "Provinsi aktif"] + MP,
          [[m["month"], m["orders"], m["bruto"], m["customers"], m["provinces"]] + [m["by_orders"].get(p, 0) for p in MP] for m in G["monthly"]])
    sheet("Per bulan (bruto)", ["Bulan"] + MP, [[m["month"]] + [m["by_bruto"].get(p, 0.0) for p in MP] for m in G["monthly"]])
    sheet("Produk per provinsi", ["Provinsi", "Produk", "Order", "% order provinsi", "Bruto"],
          [[p["province"], p["product"], p["orders"], p["pct"], p["bruto"]] for p in G["products"]])
    M = G["matrix"]
    if M:
        sheet("Produk x Provinsi (%)", ["Produk", "Order"] + M["provinces"],
              [["(semua order)", T.get("orders")] + [M["baseline"].get(p) for p in M["provinces"]]]
              + [[r["product"], r["orders"]] + [r["share"].get(p) for p in M["provinces"]] for r in M["rows"]])
    return wb


# ---------------------------------------------------------------- strategi harga

PRICE_STEP = 1000           # pembulatan harga satuan untuk "titik harga" (riwayat harga yang pernah dipakai)
PRICE_POINTS = 6            # titik harga per baris (urut qty)
PRICE_MIN_SETTLED = 0.3     # biaya marketplace per produk dipakai bila ≥ 30% bruto-nya sudah ada di laporan penghasilan


def _ads_pct_by_channel(Oall, ads, months):
    """% iklan dari bruto per channel (Shopee / TikTok / Tokopedia) di bulan-bulan periode.
    Atribusi 'shopee' → Shopee, 'tiktok' → TikTok + Tokopedia, 'all' → dibagi menurut bruto tiap channel di bulan itu."""
    out = {SHOPEE: None, TIKTOK: None, TOKOPEDIA: None}
    if not ads or not (ads.get("months") or {}) or not len(Oall):
        return out, {}
    b = Oall.groupby(["month", "channel"])["bruto"].sum()
    pool = {SHOPEE: 0.0, "tt": 0.0}
    brut = {SHOPEE: 0.0, "tt": 0.0}
    attr = ads.get("attribution") or {}
    for m in months:
        row = (ads.get("months") or {}).get(m) or {}
        bs = float(b.get((m, SHOPEE), 0.0))
        bt = float(b.get((m, TIKTOK), 0.0)) + float(b.get((m, TOKOPEDIA), 0.0))
        brut[SHOPEE] += bs
        brut["tt"] += bt
        for c in ads.get("channels", []):
            v = float(row.get(c, 0) or 0)
            a = attr.get(c, "all")
            if a == "shopee":
                pool[SHOPEE] += v
            elif a == "tiktok":
                pool["tt"] += v
            elif bs + bt > 0:
                pool[SHOPEE] += v * bs / (bs + bt)
                pool["tt"] += v * bt / (bs + bt)
    if brut[SHOPEE] > 0:
        out[SHOPEE] = round(100.0 * pool[SHOPEE] / brut[SHOPEE], 2)
    if brut["tt"] > 0:
        out[TIKTOK] = out[TOKOPEDIA] = round(100.0 * pool["tt"] / brut["tt"], 2)
    return out, {SHOPEE: pool[SHOPEE], "TikTok + Tokopedia": pool["tt"]}


def pricing(O, I, ads=None, platform="all", m_from=None, m_to=None):
    """Bahan strategi harga per (channel, listing, item HPP) di periode: harga jual & harga coret rata-rata per unit,
    HPP per unit, biaya marketplace % (1 − neto/harga produk order-nya, dibagi ke baris menurut bruto), % iklan per channel,
    titik harga yang pernah dipakai, dan riwayat per bulan. Hitungan laba/harga target dilakukan di browser supaya
    target margin & asumsi iklan bisa diubah tanpa menghitung ulang."""
    O = _cols(O, ["platform", "channel", "order_id", "created", "month", "is_unpaid", "is_batal", "is_retur", "product",
                  "qty", "bruto", "harga_awal", "hpp_item", "hpp_unit"])
    I = _cols(I, ["platform", "channel", "order_id", "is_retur", "neto", "harga_produk"])
    empty = dict(rows=[], channels={}, months=[], totals=None)
    if not len(O):
        return empty
    O = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O["is_retur"]) & (O["qty"] > 0)]
    # rasio neto/harga produk per order (order yang sudah cair & tidak dikembalikan penuh)
    ratio = pd.Series(dtype=float)
    if len(I):
        I_ok = I[~I["is_retur"]] if "is_retur" in I else I
        g = I_ok.groupby("order_id").agg(n=("neto", "sum"), h=("harga_produk", "sum"))
        g = g[g["h"] > 0]
        ratio = (g["n"] / g["h"]).clip(-1.0, 1.5)
    # cadangan: biaya marketplace % per channel dari seluruh data (periode yang belum punya laporan penghasilan)
    rt_all = O["order_id"].map(ratio)
    ok_all = rt_all.notna()
    mp_all = {}
    for ch, x in O[ok_all].assign(mp=O["bruto"] * (1 - rt_all)).groupby("channel"):
        if x["bruto"].sum() > 0:
            mp_all[ch] = round(100.0 * float(x["mp"].sum()) / float(x["bruto"].sum()), 2)
    O = _filter_months(O, "month", m_from, m_to)
    months = sorted(O["month"].dropna().unique())
    ads_pct, ads_pool = _ads_pct_by_channel(O, ads, months)
    O = _channel_filter(O, platform)
    if not len(O):
        return empty
    O = O.copy()
    for c in ("hpp_item", "product"):
        if c not in O:
            O[c] = ""
    if "hpp_unit" not in O:
        O["hpp_unit"] = 0.0
    if "harga_awal" not in O:
        O["harga_awal"] = 0.0
    O["hpp_item"] = O["hpp_item"].fillna("").astype(str)
    O["hpp_unit"] = O["hpp_unit"].fillna(0.0)

    O["rt"] = O["order_id"].map(ratio)
    st = O["rt"].notna()
    O["sb"] = O["bruto"].where(st, 0.0)                    # bruto yang sudah ada di laporan penghasilan
    O["mp"] = (O["bruto"] * (1 - O["rt"])).where(st, 0.0)  # biaya marketplace (potongan + ongkir/voucher penjual), Rp
    O["hq"] = O["qty"].where(O["hpp_unit"] > 0, 0.0)
    O["ht"] = O["qty"] * O["hpp_unit"]
    O["ha"] = O["harga_awal"].where(O["harga_awal"] > 0, 0.0)
    O["hb"] = O["bruto"].where(O["harga_awal"] > 0, 0.0)  # bruto pasangan harga coret (untuk % diskon)
    O["unit"] = O["bruto"] / O["qty"]

    ch_stats = {}
    for ch, x in O.groupby("channel"):
        sb = float(x["sb"].sum())
        ch_stats[ch] = dict(bruto=float(x["bruto"].sum()), settled_bruto=sb,
                            mp_pct=round(100.0 * float(x["mp"].sum()) / sb, 2) if sb > 0 else mp_all.get(ch),
                            mp_src="periode" if sb > 0 else ("semua data" if ch in mp_all else None),
                            ads_pct=ads_pct.get(ch))

    keys = ["channel", "product", "hpp_item"]
    agg = O.groupby(keys, sort=False).agg(qty=("qty", "sum"), bruto=("bruto", "sum"), order=("order_id", "nunique"),
                                         sb=("sb", "sum"), mp=("mp", "sum"), hq=("hq", "sum"), ht=("ht", "sum"),
                                         ha=("ha", "sum"), hb=("hb", "sum"), pmed=("unit", "median"), first=("created", "min"), last=("created", "max")).reset_index()
    # rentang harga tanpa baris gratis/bonus (harga satuan 0) dan pencilan: persentil 5–95
    q = O[O["unit"] > 0].groupby(keys, sort=False)["unit"].quantile([0.05, 0.95]).unstack()
    q.columns = ["pmin", "pmax"]
    agg = agg.merge(q.reset_index(), on=keys, how="left")
    # titik harga: harga satuan dibulatkan → qty & order per titik
    O["pp"] = (O["unit"] / PRICE_STEP).round() * PRICE_STEP
    pts = O.groupby(keys + ["pp"], sort=False).agg(q=("qty", "sum"), o=("order_id", "nunique"),
                                                   a=("created", "min"), z=("created", "max")).reset_index()
    pts = pts.sort_values("q", ascending=False)
    pts_by = {}
    for r in pts.itertuples(index=False):
        k = (r.channel, r.product, r.hpp_item)
        lst = pts_by.setdefault(k, [])
        if len(lst) < PRICE_POINTS:
            lst.append([float(r.pp), float(r.q), int(r.o), r.a.strftime("%Y-%m-%d") if pd.notna(r.a) else "",
                        r.z.strftime("%Y-%m-%d") if pd.notna(r.z) else ""])
    # riwayat per bulan: qty, harga rata-rata
    hist = O.groupby(keys + ["month"], sort=False).agg(q=("qty", "sum"), b=("bruto", "sum")).reset_index().sort_values("month")
    hist_by = {}
    for r in hist.itertuples(index=False):
        hist_by.setdefault((r.channel, r.product, r.hpp_item), []).append([r.month, float(r.q), round(float(r.b) / float(r.q)) if r.q else None])

    names = product_names(agg["product"])
    tot_bruto = float(agg["bruto"].sum())
    rows = []
    for i, r in enumerate(agg.itertuples(index=False)):
        k = (r.channel, r.product, r.hpp_item)
        cs = ch_stats.get(r.channel, {})
        own = r.sb / r.bruto if r.bruto else 0.0
        if r.sb > 0 and own >= PRICE_MIN_SETTLED:
            mp_pct, mp_src = round(100.0 * r.mp / r.sb, 2), "produk"
        else:
            mp_pct, mp_src = cs.get("mp_pct"), "platform"
        rows.append(dict(
            channel=r.channel, product=r.product, name=names.iloc[i], hpp_item=r.hpp_item,
            qty=float(r.qty), order=int(r.order), bruto=float(r.bruto), share_pct=round(100.0 * r.bruto / tot_bruto, 2) if tot_bruto else 0.0,
            harga=round(r.bruto / r.qty) if r.qty else 0,
            harga_coret=round(r.bruto / r.qty * r.ha / r.hb) if (r.qty and r.hb > 0) else None,   # harga jual ÷ (1 − diskon)
            diskon_pct=round(100.0 * (1 - r.hb / r.ha), 1) if r.ha > 0 else None,
            hpp_unit=round(r.ht / r.hq) if r.hq > 0 else None, hpp_cover_pct=round(100.0 * r.hq / r.qty, 1) if r.qty else 0.0,
            mp_pct=mp_pct, mp_src=mp_src, settled_pct=round(100.0 * own, 1),
            ads_pct=ads_pct.get(r.channel),
            pmin=float(r.pmin) if pd.notna(r.pmin) else None, pmed=float(r.pmed), pmax=float(r.pmax) if pd.notna(r.pmax) else None,
            first=r.first.strftime("%Y-%m-%d") if pd.notna(r.first) else "", last=r.last.strftime("%Y-%m-%d") if pd.notna(r.last) else "",
            points=pts_by.get(k, []), hist=hist_by.get(k, []),
        ))
    rows.sort(key=lambda x: -x["bruto"])
    # selisih harga antar channel untuk item HPP yang sama (berbobot qty)
    by_item = {}
    for x in rows:
        if x["hpp_item"]:
            d = by_item.setdefault(x["hpp_item"], {})
            q, b = d.get(x["channel"], (0.0, 0.0))
            d[x["channel"]] = (q + x["qty"], b + x["bruto"])
    for x in rows:
        d = by_item.get(x["hpp_item"]) or {}
        x["cross"] = {c: round(b / q) for c, (q, b) in d.items() if q > 0 and c != x["channel"]}

    totals = dict(rows=len(rows), qty=float(O["qty"].sum()), bruto=tot_bruto, settled_bruto=float(O["sb"].sum()),
                  hpp_cover_pct=round(100.0 * float(O["hq"].sum()) / float(O["qty"].sum()), 1) if float(O["qty"].sum()) else 0.0)
    return dict(rows=rows, channels=ch_stats, ads_pool=ads_pool, months=months, totals=totals)
