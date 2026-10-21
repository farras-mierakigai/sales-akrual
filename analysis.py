"""Agregasi akrual: semua angka diakui di BULAN ORDER DIBUAT, biaya dicocokkan ke order-nya."""
import os
import glob
import pandas as pd
import parsers
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
    os.makedirs(DATA, exist_ok=True)
    return sorted(d for d in os.listdir(DATA) if os.path.isdir(os.path.join(DATA, d)) and not d.startswith("."))


def toko_dir(toko):
    safe = "".join(c for c in toko if c.isalnum() or c in " -_").strip()
    if not safe:
        raise ValueError("nama toko kosong")
    return os.path.join(DATA, safe)


def toko_files(toko):
    d = toko_dir(toko)
    return sorted(p for p in glob.glob(os.path.join(d, "*")) if os.path.isfile(p) and not os.path.basename(p).startswith(".") and not p.lower().endswith(".json"))


def load_toko(toko):
    d = toko_dir(toko)
    cache = os.path.join(d, ".cache")
    orders, income, adj, files, unsettled = [], [], [], [], []
    for p in toko_files(toko):
        r = parsers.parse_file(p, cache)
        if len(r.get("unsettled", [])):
            unsettled.append(r["unsettled"])
        m = dict(r["meta"]); m["kind"] = r["kind"]; m["label"] = parsers.KINDS.get(r["kind"], "Tidak dikenali")
        if len(r["orders"]):
            top = r["orders"]["product"].str.split(r"\s[-–]\s", n=1).str[0].str.strip()
            m["hint"] = top.value_counts().index[0] if len(top) else ""
            orders.append(r["orders"])
        if len(r["income"]):
            income.append(r["income"])
        if len(r["adj"]):
            adj.append(r["adj"])
        files.append(m)
    O = pd.concat(orders, ignore_index=True) if orders else pd.DataFrame()
    I = pd.concat(income, ignore_index=True) if income else pd.DataFrame()
    A = pd.concat(adj, ignore_index=True) if adj else pd.DataFrame()
    U = pd.concat(unsettled, ignore_index=True) if unsettled else pd.DataFrame()
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
        O["month"] = O["created"].dt.strftime("%Y-%m")
    if len(I):
        I = I.sort_values("file_mtime").drop_duplicates("line_key", keep="last").reset_index(drop=True)
    # aturan Farras 2026-10-08: dana dikembalikan penuh = retur → keluar dari bruto walau status non-batal
    retur_ids = full_refund_ids(I)
    if len(I):
        I["is_retur"] = I["order_id"].isin(retur_ids)
    if len(O):
        O["is_retur"] = O["order_id"].isin(retur_ids) & ~O["is_batal"]
        O = hppmod.apply(O, d)
    if len(A):
        A = A.drop_duplicates(["id", "type", "settled", "amount"]).reset_index(drop=True)
    return O, I, A, files, raw


def full_refund_ids(I):
    """Order yang refund-nya ≥ 99% harga produk (dana dikembalikan penuh)."""
    if not len(I):
        return set()
    g = I.groupby("order_id").agg(h=("harga_produk", "sum"), r=("refund", "sum"))
    return set(g[(g["h"] > 0) & (g["r"] <= -0.99 * g["h"])].index)


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


def monthly(O, I, platform="all", m_from=None, m_to=None, U=None, ads=None, d_from=None, d_to=None):
    """Tabel per bulan + ringkasan. Semua bulan = bulan order dibuat. U = estimasi resmi TikTok (opsional)."""
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
        I["month"] = I["month"].fillna(I["created"].dt.strftime("%Y-%m"))
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
    ratio_pm, ratio_p = {}, {}
    if len(I):
        g = I.groupby(["platform", "month"]).agg(n=("neto", "sum"), h=("harga_produk", "sum"), c=("order_id", "nunique"))
        for (pl, mo), x in g.iterrows():
            if x["h"] > 0 and x["c"] >= 30:
                ratio_pm[(pl, mo)] = x["n"] / x["h"]
        gp = I.groupby("platform").agg(n=("neto", "sum"), h=("harga_produk", "sum"))
        for pl, x in gp.iterrows():
            if x["h"] > 0:
                ratio_p[pl] = x["n"] / x["h"]
    U_est = {}
    if U is not None and len(U):
        U_est = U.groupby("order_id")["est_neto"].sum().to_dict()

    rows = []
    for m in months:
        o = O[O["month"] == m] if len(O) else O
        i = I[I["month"] == m] if len(I) else I
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
            r["bruto_income"] = float(i["harga_produk"].sum())
            r["order_settled"] = int(i["order_id"].nunique())
            settled_ids = set(i["order_id"])
        else:
            r["refund"] = 0.0
            r["refund_retur"] = 0.0
            for k, _ in fee_lines + other_lines:
                r[k] = 0.0
            r["total_potongan"] = r["neto"] = r["bruto_income"] = 0.0
            r["order_settled"] = 0
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
        I["month"] = I["order_id"].map(om).fillna(I["created"].dt.strftime("%Y-%m"))
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
    d = sold.groupby(sold["created"].dt.strftime("%Y-%m-%d")).agg(bruto=("bruto", "sum"), order=("order_id", "nunique")).reset_index().rename(columns={"created": "date"})
    # channel split
    ch = O.groupby("channel").agg(masuk=("order_id", "nunique")).join(sold.groupby("channel").agg(terjual=("order_id", "nunique"), bruto=("bruto", "sum"))).fillna(0).reset_index()
    return dict(products=products, products_total=tot, variants=variants, status=st.to_dict("records"), cancel_reasons=cr.to_dict("records"),
                payment=pm.to_dict("records"), daily=d.to_dict("records"), channels=ch.to_dict("records"))


def adjustments(A, m_from=None, m_to=None):
    if not len(A):
        return []
    A = A.copy()
    A["month"] = A["settled"].dt.strftime("%Y-%m")
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
