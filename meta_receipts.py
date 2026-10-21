"""Baca zip kuitansi Meta Ads (Transaction #....pdf) → per transaksi: tanggal, nominal, jenis (top-up vs tagihan), akun."""
import io
import re
import zipfile
import collections
from pdfminer.high_level import extract_text


def parse_pdf_text(t):
    g = lambda pat: (re.search(pat, t) or [None, None])[1]
    date = g(r"Invoice/payment date\s*\n\s*(.+)")
    amt = g(r"IDR\s?([\d,\.]+)")
    typ = g(r"Product type\s*\n\s*(.+)")
    pm = g(r"Payment method\s*\n\s*(.+)")
    acct = g(r"Receipt for (.+)")
    tid = g(r"Transaction ID\s*\n\s*(.+)")
    st = g(r"\n(Funded|Paid|Refunded|Failed|Pending|Disputed)[^\n]*\n")
    m = re.match(r"(\d{1,2}) (\w{3}) (\d{4})", (date or "").strip())
    MON = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
           "Mei": 5, "Agu": 8, "Okt": 10, "Des": 12}
    iso = f"{m.group(3)}-{MON.get(m.group(2), 0):02d}-{int(m.group(1)):02d}" if m else ""
    return dict(date=iso, amount=float(amt.replace(",", "")) if amt else 0.0, typ=(typ or "").strip(), status=(st or "").strip(),
                pm=(pm or "").strip(), acct=(acct or "").strip(), tid=(tid or "").strip())


def parse_zip(path_or_bytes):
    zf = zipfile.ZipFile(path_or_bytes if not isinstance(path_or_bytes, bytes) else io.BytesIO(path_or_bytes))
    rows = []
    for name in zf.namelist():
        if not name.lower().endswith(".pdf"):
            continue
        with zf.open(name) as fh:
            t = extract_text(io.BytesIO(fh.read()))
        r = parse_pdf_text(t)
        r["file"] = name
        st = r["status"]
        r["kind"] = "topup" if st == "Funded" else ("spend" if st == "Paid" else (st.lower() or "?"))
        rows.append(r)
    return rows


def summarize(rows):
    """dedup by transaction id → per bulan: spend, topup, n."""
    seen, out = {}, {}
    for r in rows:
        if r["tid"] and r["tid"] in seen:
            continue
        seen[r["tid"] or r["file"]] = r
        m = r["date"][:7]
        o = out.setdefault(m, dict(spend=0.0, topup=0.0, other=0.0, n_spend=0, n_topup=0, accts=set()))
        o["accts"].add(r["acct"])
        if r["kind"] == "spend":
            o["spend"] += r["amount"]; o["n_spend"] += 1
        elif r["kind"] == "topup":
            o["topup"] += r["amount"]; o["n_topup"] += 1
        else:
            o["other"] += r["amount"]
    for o in out.values():
        o["accts"] = sorted(o["accts"])
    return dict(sorted(out.items())), list(seen.values())
