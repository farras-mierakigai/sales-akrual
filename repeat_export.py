"""Unduhan dashboard Pelanggan berulang: Excel (semua tabel + grafik) dan PDF (ringkasan dashboard siap kirim/cetak).

Keduanya dibangun dari hasil /api/repeat yang sudah di-cache (dict R dari analysis.repeat_customers), jadi tidak
menghitung ulang. meta = dict(toko, platform, period, generated).
"""
import datetime
import io
import os

MONTHS_ID = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
COL = {"Shopee": "#F4561E", "TikTok": "#5E1AF1", "Tokopedia": "#0F766E"}
PLATFORM_LABEL = {"all": "Gabungan (semua)", "shopee": "Shopee", "tiktok": "TikTok Shop", "tokopedia": "Tokopedia",
                  "tiktok_all": "TikTok + Tokopedia"}
WIB = datetime.timezone(datetime.timedelta(hours=7))
PDF_TOP = 100          # pelanggan berulang di PDF (daftar lengkap ada di Excel)
PDF_LIST = 20          # baris tabel produk / pasangan
PDF_LAUNCH = 30        # baris produk baru dirilis
PDF_COHORT_ROWS = 24   # kohort terakhir
PDF_COHORT_COLS = 13   # +0 … +12 bulan


# ---------------------------------------------------------------- format angka (sama dengan dashboard)

def fmt(n):
    if n is None:
        return "–"
    try:
        return f"{round(float(n)):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "–"


def pct(n):
    if n is None:
        return "–"
    v = round(float(n), 1)
    s = f"{v:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return (s[:-2] if s.endswith(",0") else s) + "%"


def jt(n):
    n = float(n or 0)
    if abs(n) >= 1e9:
        s = f"{n / 1e9:,.2f}".rstrip("0").rstrip(".")
        return s.replace(",", "X").replace(".", ",").replace("X", ".") + " M"
    s = f"{n / 1e6:,.1f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " jt"


def rp(n):
    return "Rp " + fmt(n)


def month_label(m):
    if not m:
        return ""
    y, mm = str(m).split("-")[:2]
    return f"{MONTHS_ID[int(mm) - 1]} {y[2:]}"


def period_label(R, d_from=None, d_to=None):
    if d_from or d_to:
        ms = R.get("months") or []

        def d(s, first):
            if not s:
                return month_label(ms[0 if first else -1]) if ms else "…"
            y, m, dd = s.split("-")
            return f"{int(dd)} {MONTHS_ID[int(m) - 1]} {y}"
        return f"{d(d_from, True)} – {d(d_to, False)}"
    ms = R.get("months") or []
    if not ms:
        return "–"
    return month_label(ms[0]) + (f" – {month_label(ms[-1])}" if len(ms) > 1 else "")


def meta_for(R, toko, platform, d_from=None, d_to=None):
    return dict(toko=toko, platform=PLATFORM_LABEL.get(platform, platform), period=period_label(R, d_from, d_to),
                generated=datetime.datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB"))


def tiles(T):
    gm, ga = T.get("gap_median"), T.get("gap_avg")
    return [
        ("Pelanggan aktif", fmt(T["customers"]), f'{fmt(T["new_customers"])} baru · {fmt(T["returning_customers"])} lama'),
        ("Pernah beli ≥ 2×", pct(T["repeat_pct"]), f'{fmt(T["repeat_customers"])} dari {fmt(T["customers"])} pelanggan aktif'),
        ("Order ulang", fmt(T["repeat_orders"]), f'{pct(T["repeat_orders_pct"])} dari {fmt(T["orders"])} order terjual'),
        ("Bruto dari order ulang", rp(T["repeat_bruto"]), f'{pct(T["repeat_bruto_pct"])} dari bruto {jt(T["bruto"])}'),
        ("Order per pelanggan", f'{T.get("orders_per_customer") or 0:.2f}'.replace(".", ","), f'{rp(T["bruto_per_customer"])} per pelanggan'),
        ("Jarak order ulang", "–" if gm is None else f"{fmt(gm)} hari", "median · rata-rata " + ("–" if ga is None else f"{fmt(ga)} hari")),
    ]


# ---------------------------------------------------------------- Excel

PLAT_KEYS = ["label", "customers", "new_customers", "returning_customers", "repeat_customers", "repeat_pct", "orders", "repeat_orders",
             "repeat_orders_pct", "bruto", "repeat_bruto", "repeat_bruto_pct", "orders_per_customer", "bruto_per_customer", "aov",
             "gap_median", "gap_avg"]
PLAT_HEAD = ["Platform", "Pelanggan aktif", "Pelanggan baru", "Pelanggan lama", "Pernah beli ≥2×", "% pernah beli ≥2×", "Order",
             "Order ulang", "% order ulang", "Bruto", "Bruto order ulang", "% bruto order ulang", "Order/pelanggan", "Bruto/pelanggan",
             "Rata-rata/order", "Median jarak order (hari)", "Rata-rata jarak (hari)"]
HAB_KEYS = ["orders", "new", "new_pct", "new_bruto", "variant", "variant_pct", "variant_bruto", "same", "same_pct", "same_bruto"]
HAB_HEAD = ["Order ulang", "Produk baru", "% produk baru", "Bruto produk baru", "Warna/varian baru", "% warna/varian baru",
            "Bruto warna/varian baru", "Item sama persis", "% item sama", "Bruto item sama"]
LAUNCH_KEYS = ["launches", "buyers", "new", "new_pct", "old", "old_pct", "orders", "bruto"]


def xlsx(R, meta):
    """Workbook: Ringkasan (KPI + grafik) lalu satu sheet per tabel dashboard, termasuk SEMUA pelanggan berulang."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    bold = Font(bold=True)
    hfill = PatternFill("solid", fgColor="EDE7FF")
    tfill = PatternFill("solid", fgColor="F6F3FF")
    H = R.get("habits") or {}
    LA = H.get("launches") or {}
    T = R.get("totals") or {}

    def style(ws, header_row, ncols, widths=None, pct_cols=(), money_cols=(), dec_cols=()):
        for c in ws[header_row]:
            c.font = bold
            c.fill = hfill
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for j in range(1, ncols + 1):
            letter = get_column_letter(j)
            ws.column_dimensions[letter].width = (widths or {}).get(j, 16 if j > 1 else 22)
            nf = "0.0" if j in pct_cols else "#,##0.00" if j in dec_cols else "#,##0"
            for (cell,) in ws.iter_rows(min_row=header_row + 1, min_col=j, max_col=j):
                if isinstance(cell.value, (int, float)):
                    cell.number_format = nf
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    def sheet(title, header, rows, total_rows=0, **kw):
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for r in rows:
            ws.append(r)
        style(ws, 1, len(header), **kw)
        for i in range(total_rows):
            for c in ws[ws.max_row - i]:
                c.font = bold
                c.fill = tfill
        return ws

    # --- Ringkasan
    ws = wb.active
    ws.title = "Ringkasan"
    ws["A1"] = f'Pelanggan berulang — {meta["toko"]}'
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f'Periode: {meta["period"]} · Platform: {meta["platform"]} · Diunduh {meta["generated"]}'
    r0 = 4
    if R.get("no_id"):
        ws["A3"] = f'{fmt(R["no_id"])} order ({jt(R.get("no_id_bruto"))}) tanpa identitas pembeli — tidak masuk analisa'
        ws["A3"].font = Font(italic=True, color="C7811A")
    ws.cell(row=r0, column=1, value="Indikator").font = bold
    ws.cell(row=r0, column=2, value="Nilai").font = bold
    ws.cell(row=r0, column=3, value="Keterangan").font = bold
    for c in ws[r0]:
        c.fill = hfill
    if T:
        for i, (k, v, s) in enumerate(tiles(T), start=1):
            ws.cell(row=r0 + i, column=1, value=k).font = bold
            ws.cell(row=r0 + i, column=2, value=v)
            ws.cell(row=r0 + i, column=3, value=s)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 60
    M = R.get("monthly") or []
    # data grafik di kolom tersembunyi kanan (E..I)
    ws["E4"], ws["F4"], ws["G4"], ws["H4"], ws["I4"] = "Bulan", "Pelanggan baru", "Pelanggan lama", "% order ulang", "% bruto pelanggan lama"
    for i, m in enumerate(M, start=5):
        ws.cell(row=i, column=5, value=month_label(m["month"]))
        ws.cell(row=i, column=6, value=m["new"])
        ws.cell(row=i, column=7, value=m["returning"])
        ws.cell(row=i, column=8, value=m["repeat_orders_pct"])
        ws.cell(row=i, column=9, value=round(100 * m["bruto_returning"] / m["bruto"], 1) if m["bruto"] else 0)
    for c in "EFGHI":
        ws.column_dimensions[c].width = 14
    if M:
        n = len(M)
        cats = Reference(ws, min_col=5, min_row=5, max_row=4 + n)
        bc = BarChart()
        bc.type, bc.grouping, bc.overlap = "col", "stacked", 100
        bc.title, bc.height, bc.width = "Pelanggan baru vs lama per bulan", 8, 18
        bc.add_data(Reference(ws, min_col=6, max_col=7, min_row=4, max_row=4 + n), titles_from_data=True)
        bc.set_categories(cats)
        bc.series[0].graphicalProperties.solidFill = "D9D4EA"
        bc.series[1].graphicalProperties.solidFill = "5E1AF1"
        ws.add_chart(bc, "A12")
        lc = LineChart()
        lc.title, lc.height, lc.width = "Porsi order ulang per bulan (%)", 8, 18
        lc.add_data(Reference(ws, min_col=8, max_col=9, min_row=4, max_row=4 + n), titles_from_data=True)
        lc.set_categories(cats)
        lc.series[0].graphicalProperties.line.solidFill = "F4561E"
        lc.series[1].graphicalProperties.line.solidFill = "0F766E"
        ws.add_chart(lc, "A29")

    # --- tabel
    rows = [[s.get(k) for k in PLAT_KEYS] for s in R.get("channels") or []]
    sheet("Per platform", PLAT_HEAD, rows + ([[T.get(k) for k in PLAT_KEYS]] if T else []), total_rows=1 if T else 0,
          pct_cols=(6, 9, 12), dec_cols=(13,))
    sheet("Per bulan", ["Bulan", "Pelanggan aktif", "Baru", "Lama", "% lama", "Order", "Order ulang", "% order ulang", "Bruto",
                        "Bruto baru", "Bruto lama"],
          [[m["month"], m["customers"], m["new"], m["returning"], m["returning_pct"], m["orders"], m["repeat_orders"],
            m["repeat_orders_pct"], m["bruto"], m["bruto_new"], m["bruto_returning"]] for m in M], pct_cols=(5, 8))
    sheet("Distribusi order", ["Jumlah order (sepanjang data)", "Pelanggan", "% pelanggan", "Bruto periode", "% bruto"],
          [[d["bucket"] + "×", d["customers"], d["customers_pct"], d["bruto"], d["bruto_pct"]] for d in R.get("dist") or []],
          pct_cols=(3, 5))
    g2 = R.get("gap_second") or {}
    ws = sheet("Jarak order ulang", ["Jarak sejak order sebelumnya", "Order ulang", "% order ulang", "Bruto"],
               [[g["bucket"], g["orders"], g["orders_pct"], g["bruto"]] for g in R.get("gap") or []], pct_cols=(3,))
    if g2.get("n"):
        ws.append([])
        ws.append([f'Order ke-1 → ke-2 ({fmt(g2["n"])} pelanggan): median {fmt(g2["median"])} hari, separuh kembali antara '
                   f'{fmt(g2["p25"])}–{fmt(g2["p75"])} hari'])
    K = R.get("cohort_cols") or 0
    sheet("Kohort retensi %", ["Kohort (bulan order pertama)", "Pelanggan"] + [f"+{k} bln" for k in range(K)],
          [[c["cohort"], c["size"]] + c["pct"] for c in R.get("cohorts") or []], pct_cols=tuple(range(3, K + 3)),
          widths={j: 9 for j in range(3, K + 3)})
    ht = H.get("totals")
    sheet("Kebiasaan order ulang", ["Platform / urutan order"] + HAB_HEAD,
          [[s["label"]] + [s.get(k) for k in HAB_KEYS] for s in H.get("channels", [])]
          + ([[ht["label"]] + [ht.get(k) for k in HAB_KEYS]] if ht else [])
          + [["Order " + s["bucket"]] + [s.get(k) for k in HAB_KEYS] for s in H.get("seq", [])], pct_cols=(4, 7, 10))
    sheet("Produk dibeli ulang", ["Platform", "Produk", "Pelanggan", "Item dibeli ulang", "Item sama persis", "Warna/varian baru", "Qty", "Bruto"],
          [[p["channel"], p["product"], p["customers"], p["lines"], p["same"], p["variant"], p["qty"], p["bruto"]] for p in H.get("products", [])],
          widths={1: 12, 2: 50})
    sheet("Produk baru pelanggan lama", ["Platform", "Produk", "Pelanggan", "Item", "Qty", "Bruto"],
          [[p["channel"], p["product"], p["customers"], p["lines"], p["qty"], p["bruto"]] for p in H.get("new_products", [])],
          widths={1: 12, 2: 50})
    sheet("Lanjut beli", ["Platform", "Pernah beli", "Lalu beli (produk baru)", "Pelanggan"],
          [[p["channel"], p["src"], p["dst"], p["customers"]] for p in H.get("pairs", [])], widths={1: 12, 2: 45, 3: 45})
    lt = LA.get("totals")
    sheet("Produk baru rilis", ["Platform / produk", "Bulan rilis", "Produk baru", "Pembeli", "Orang baru", "% orang baru",
                                "Pelanggan lama", "% pelanggan lama", "Order", "Bruto"],
          [[c["label"], ""] + [c.get(k) for k in LAUNCH_KEYS] for c in LA.get("channels", [])]
          + ([[lt["label"], ""] + [lt.get(k) for k in LAUNCH_KEYS]] if lt else [])
          + [[f'{r["label"]}: {r["product"]}', r["month"], 1, r["buyers"], r["new"], r["new_pct"], r["old"], r["old_pct"], r["orders"], r["bruto"]]
             for r in LA.get("items", [])], pct_cols=(6, 8), widths={1: 50})
    hab = lambda h: "" if not h else f'{h["new"]} produk baru · {h["variant"]} warna/varian baru · {h["same"]} item sama'
    sheet("Pelanggan berulang", ["Platform", "Pelanggan", "Order di periode", "Order sepanjang data", "Qty", "Bruto periode", "Order pertama",
                                 "Order terakhir", "Rentang (hari)", "Rata-rata jarak (hari)", "Produk paling sering", "Kebiasaan order ulang"],
          [[t["channel"], t["buyer"], t["orders"], t["orders_total"], t["qty"], t["bruto"], t["first"], t["last"], t["span_days"],
            t["gap_avg"], t["product"], hab(t.get("habit"))] for t in R.get("top") or []],
          widths={1: 12, 2: 24, 11: 45, 12: 40}, dec_cols=(10,))
    return wb


def xlsx_bytes(R, meta):
    buf = io.BytesIO()
    xlsx(R, meta).save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PDF

_FONT = None


def _fonts():
    """Font Unicode (DejaVu di server, Arial Unicode di Mac) supaya ≥, →, huruf non-Latin tampil; cadangan Helvetica."""
    global _FONT
    if _FONT:
        return _FONT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    cands = [("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
             ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
             ("/Library/Fonts/Arial Unicode.ttf", None)]
    for reg, bld in cands:
        if os.path.isfile(reg):
            try:
                pdfmetrics.registerFont(TTFont("SA", reg))
                pdfmetrics.registerFont(TTFont("SA-Bold", bld if bld and os.path.isfile(bld) else reg))
                _FONT = ("SA", "SA-Bold", True)
                return _FONT
            except Exception:
                pass
    _FONT = ("Helvetica", "Helvetica-Bold", False)
    return _FONT


def _txt(s, n=None):
    s = "" if s is None else str(s)
    if n and len(s) > n:
        s = s[: n - 1] + "…"
    if not _fonts()[2]:   # font bawaan hanya Latin-1/cp1252
        s = s.replace("≥", ">=").replace("→", "->").encode("cp1252", "replace").decode("cp1252")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pdf(R, meta):
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

    F, FB, _ = _fonts()
    C = colors.HexColor
    INK, INK2, MUTED, LINE, LAV, BG2 = C("#1E1E1E"), C("#5B5B6B"), C("#8A8A99"), C("#E6E3F0"), C("#EDE7FF"), C("#F6F3FF")
    PURPLE, ORANGE, TEAL, PURPLE_DK = C("#5E1AF1"), C("#F4561E"), C("#0F766E"), C("#340E7C")
    st = dict(
        h1=ParagraphStyle("h1", fontName=FB, fontSize=17, leading=21, textColor=INK),
        sub=ParagraphStyle("sub", fontName=F, fontSize=9, leading=12, textColor=INK2),
        h2=ParagraphStyle("h2", fontName=FB, fontSize=11.5, leading=14, textColor=INK, spaceBefore=4, spaceAfter=2),
        desc=ParagraphStyle("desc", fontName=F, fontSize=7.5, leading=9.5, textColor=INK2, spaceAfter=4),
        cell=ParagraphStyle("cell", fontName=F, fontSize=7, leading=8.5, textColor=INK),
        cellr=ParagraphStyle("cellr", fontName=F, fontSize=7, leading=8.5, textColor=INK, alignment=TA_RIGHT),
        th=ParagraphStyle("th", fontName=FB, fontSize=6.8, leading=8.2, textColor=PURPLE_DK),
        thr=ParagraphStyle("thr", fontName=FB, fontSize=6.8, leading=8.2, textColor=PURPLE_DK, alignment=TA_RIGHT),
        tk=ParagraphStyle("tk", fontName=FB, fontSize=7, leading=9, textColor=INK2),
        tv=ParagraphStyle("tv", fontName=FB, fontSize=12.5, leading=15.5, textColor=INK),
        ts=ParagraphStyle("ts", fontName=F, fontSize=7, leading=8.5, textColor=MUTED),
        note=ParagraphStyle("note", fontName=F, fontSize=8, leading=10, textColor=C("#C7811A")),
    )
    W = landscape(A4)[0] - 24 * mm
    H = R.get("habits") or {}
    LA = H.get("launches") or {}
    T = R.get("totals")

    def P(s, style="cell", n=None):
        return Paragraph(_txt(s, n), st[style])

    def table(head, rows, widths, left=1, total_last=False, cell_styles=None):
        """head/rows: teks mentah; kolom < left rata kiri, sisanya kanan."""
        data = [[P(h, "th" if j < left else "thr") for j, h in enumerate(head)]]
        for r in rows:
            data.append([P(v, "cell" if j < left else "cellr") for j, v in enumerate(r)])
        t = Table(data, colWidths=[w * W for w in widths], repeatRows=1)
        cmds = [("BACKGROUND", (0, 0), (-1, 0), LAV), ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
        if total_last and rows:
            cmds.append(("BACKGROUND", (0, len(rows)), (-1, len(rows)), BG2))
        t.setStyle(TableStyle(cmds + (cell_styles or [])))
        return t

    def tile_row(items, ncol=None):
        ncol = ncol or len(items)
        cells = [[P(k, "tk"), P(v, "tv"), P(s, "ts")] for k, v, s in items]
        cells += [""] * (ncol - len(cells))
        gap = 2 * mm
        w = (W - gap * (ncol - 1)) / ncol
        inner = [Table([[c[0]], [c[1]], [c[2]]] if c else [[""]], colWidths=[w - 2]) if c else "" for c in cells]
        for t in inner:
            if t:
                t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        row = []
        widths = []
        for i, t in enumerate(inner):
            row.append(t)
            widths.append(w)
            if i < ncol - 1:
                row.append("")
                widths.append(gap)
        out = Table([row], colWidths=widths)
        cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]
        for i in range(0, len(row), 2):
            if inner[i // 2]:
                cmds += [("BOX", (i, 0), (i, 0), 0.6, INK), ("TOPPADDING", (i, 0), (i, 0), 5), ("BOTTOMPADDING", (i, 0), (i, 0), 5)]
        out.setStyle(TableStyle(cmds))
        return out

    def legend(d, items, y):
        x = 4
        for col, label in items:
            d.add(Rect(x, y, 7, 7, fillColor=col, strokeColor=None))
            d.add(String(x + 10, y + 1, _txt(label).replace("&amp;", "&"), fontName=F, fontSize=7, fillColor=INK2))
            x += 16 + len(label) * 3.6

    def month_axis(ch, labels):
        ch.categoryAxis.categoryNames = labels
        ch.categoryAxis.labels.fontName = F
        ch.categoryAxis.labels.fontSize = 6
        if len(labels) > 14:
            ch.categoryAxis.labels.angle = 45
            ch.categoryAxis.labels.boxAnchor = "ne"
            ch.categoryAxis.labels.dy = -2
        ch.valueAxis.labels.fontName = F
        ch.valueAxis.labels.fontSize = 6.5
        ch.valueAxis.strokeColor = LINE
        ch.categoryAxis.strokeColor = LINE
        ch.valueAxis.gridStrokeColor = LINE
        ch.valueAxis.visibleGrid = True

    def bar_stacked(labels, series, cols, w, h, value_fmt=fmt):
        d = Drawing(w, h)
        ch = VerticalBarChart()
        ch.x, ch.y, ch.width, ch.height = 36, 34 if len(labels) > 14 else 22, w - 44, h - (54 if len(labels) > 14 else 42)
        ch.data = series
        ch.categoryAxis.style = "stacked"
        ch.barSpacing = 0
        ch.groupSpacing = 3
        for i, c in enumerate(cols):
            ch.bars[i].fillColor = c
            ch.bars[i].strokeColor = None
        month_axis(ch, labels)
        ch.valueAxis.valueMin = 0
        ch.valueAxis.labelTextFormat = lambda v: value_fmt(v)
        d.add(ch)
        return d, ch

    def line(labels, series, cols, w, h):
        d = Drawing(w, h)
        ch = HorizontalLineChart()
        ch.x, ch.y, ch.width, ch.height = 36, 34 if len(labels) > 14 else 22, w - 44, h - (54 if len(labels) > 14 else 42)
        ch.data = series
        for i, c in enumerate(cols):
            ch.lines[i].strokeColor = c
            ch.lines[i].strokeWidth = 1.6
        month_axis(ch, labels)
        ch.valueAxis.valueMin = 0
        ch.valueAxis.labelTextFormat = lambda v: pct(v)
        d.add(ch)
        return d

    def card(title, desc, *flow):
        return [P(title, "h2"), *( [P(desc, "desc")] if desc else []), *flow, Spacer(1, 7)]

    story = [P(f'Pelanggan berulang — {meta["toko"]}', "h1"),
             Paragraph(f'Periode <font name="{FB}">{_txt(meta["period"])}</font> · Platform <font name="{FB}">{_txt(meta["platform"])}</font>'
                          f' · diunduh {_txt(meta["generated"])}', st["sub"])]
    if R.get("no_id"):
        story.append(P(f'{fmt(R["no_id"])} order ({jt(R.get("no_id_bruto"))}) tanpa identitas pembeli (export lama / akun agregat '
                       f'marketplace) — tidak masuk analisa.', "note"))
    story.append(Spacer(1, 8))
    if not T:
        story.append(P("Tidak ada order terjual dengan identitas pembeli untuk filter ini.", "sub"))
    else:
        story.append(tile_row(tiles(T)))
        story.append(Spacer(1, 10))

        # per platform
        hd = ["Platform", "Pelanggan aktif", "Baru", "Lama", "Beli ≥2×", "%", "Order", "Order ulang", "% order ulang", "Bruto",
              "Bruto order ulang", "% bruto", "Order/ pelanggan", "Bruto/ pelanggan", "Rata-rata/ order", "Median jarak (hari)"]
        rr = lambda s: [s["label"], fmt(s["customers"]), fmt(s["new_customers"]), fmt(s["returning_customers"]), fmt(s["repeat_customers"]),
                        pct(s["repeat_pct"]), fmt(s["orders"]), fmt(s["repeat_orders"]), pct(s["repeat_orders_pct"]), fmt(s["bruto"]),
                        fmt(s["repeat_bruto"]), pct(s["repeat_bruto_pct"]), str(s.get("orders_per_customer") or 0).replace(".", ","),
                        fmt(s["bruto_per_customer"]), fmt(s["aov"]), fmt(s.get("gap_median"))]
        ch_rows = [rr(s) for s in R.get("channels") or []]
        multi = len(ch_rows) > 1
        if multi:
            ch_rows.append(rr(T))
        story += card("Per platform", "Basis = order terjual (non-batal, non-retur, sudah bayar). Pelanggan baru = order pertamanya di "
                      "periode ini; lama = sudah pernah order sebelum periode. Order ulang = order ke-2 dst dari pelanggan yang sama. "
                      "Pelanggan tidak bisa dicocokkan lintas platform, jadi Gabungan = penjumlahan.",
                      table(hd, ch_rows, [.08] + [.0577] * 8 + [.085, .085] + [.0577] * 5, total_last=multi))

        # grafik per bulan
        M = R.get("monthly") or []
        if M:
            labels = [month_label(m["month"]) for m in M]
            cw = (W - 6 * mm) / 2
            d1, _ = bar_stacked(labels, [[m["new"] for m in M], [m["returning"] for m in M]], [C("#D9D4EA"), PURPLE], cw, 170)
            legend(d1, [(C("#D9D4EA"), "Pelanggan baru"), (PURPLE, "Pelanggan lama")], 2)
            d2 = line(labels, [[m["repeat_orders_pct"] for m in M],
                               [round(100 * m["bruto_returning"] / m["bruto"], 1) if m["bruto"] else 0 for m in M]], [ORANGE, TEAL], cw, 170)
            legend(d2, [(ORANGE, "% order ulang"), (TEAL, "% bruto dari pelanggan lama")], 2)
            g = Table([[[P("Pelanggan baru vs lama per bulan", "h2"), P("Pelanggan unik yang order di bulan itu; lama = sudah pernah order di bulan sebelumnya.", "desc"), d1], "",
                        [P("Porsi order ulang per bulan", "h2"), P("% order bulan itu yang merupakan order ke-2 dst, dan % bruto dari pelanggan lama.", "desc"), d2]]],
                      colWidths=[cw, 6 * mm, cw])
            g.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [KeepTogether([g]), Spacer(1, 8)]

        # distribusi + jarak
        dist = table(["Total order (sepanjang data)", "Pelanggan", "% pelanggan", "Bruto periode", "% bruto"],
                     [[d["bucket"] + "×", fmt(d["customers"]), pct(d["customers_pct"]), fmt(d["bruto"]), pct(d["bruto_pct"])] for d in R.get("dist") or []],
                     [.16, .08, .07, .1, .07])
        gap = table(["Jarak sejak order sebelumnya", "Order ulang", "% order ulang", "Bruto"],
                    [[g["bucket"], fmt(g["orders"]), pct(g["orders_pct"]), fmt(g["bruto"])] for g in R.get("gap") or []],
                    [.18, .09, .09, .11])
        g2 = R.get("gap_second") or {}
        g2t = (f'Order ke-1 → ke-2 ({fmt(g2["n"])} pelanggan, sepanjang data): median {fmt(g2["median"])} hari, separuh pelanggan '
               f'kembali antara {fmt(g2["p25"])}–{fmt(g2["p75"])} hari.') if g2.get("n") else ""
        half = (W - 6 * mm) / 2
        t2 = Table([[[P("Berapa kali pelanggan membeli", "h2"), P("Pelanggan aktif di periode menurut total order-nya sepanjang data.", "desc"), dist], "",
                     [P("Jarak antar order ulang", "h2"), P("Hari sejak order sebelumnya, untuk order ke-2 dst di periode. " + g2t, "desc"), gap]]],
                   colWidths=[half, 6 * mm, half])
        t2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story += [KeepTogether([t2]), Spacer(1, 8)]

        # kebiasaan order ulang
        HT = H.get("totals")
        if HT:
            hab_cols = [("new", "Produk baru", ORANGE), ("variant", "Warna/varian baru, produk sama", PURPLE), ("same", "Item sama persis", C("#9CA3AF"))]
            ht = [(l, pct(HT[k + "_pct"]), f'{fmt(HT[k])} dari {fmt(HT["orders"])} order ulang · bruto {jt(HT[k + "_bruto"])}') for k, l, _ in hab_cols]
            if not H.get("has_variation"):
                ht.append(("Catatan", "Tanpa data varian", "file pesanan tanpa kolom variasi: warna/varian baru tidak bisa dibedakan"))
            hr = lambda s, label: [label, fmt(s["orders"]), fmt(s["new"]), pct(s["new_pct"]), fmt(s["variant"]), pct(s["variant_pct"]),
                                    fmt(s["same"]), pct(s["same_pct"])]
            rows = [hr(s, s["label"]) for s in H.get("channels") or []]
            if len(rows) > 1:
                rows.append(hr(HT, HT["label"]))
            rows += [hr(s, "Order " + s["bucket"]) for s in H.get("seq") or []]
            htab = table(["Platform / urutan order", "Order ulang", "Produk baru", "%", "Warna/ varian baru", "%", "Item sama", "%"], rows,
                         [.15, .055, .055, .045, .055, .045, .055, .045])
            S = H.get("seq") or []
            d3 = Drawing(W * .44, 150)
            if S:
                d3, _ = bar_stacked(["Order " + s["bucket"] for s in S], [[s[k + "_pct"] for s in S] for k, _, _ in hab_cols],
                                    [c for _, _, c in hab_cols], W * .44, 150, value_fmt=pct)
                legend(d3, [(c, l) for _, l, c in hab_cols], 2)
            body = Table([[htab, "", d3]], colWidths=[W * .53, W * .03, W * .44])
            body.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story.append(CondPageBreak(120 * mm))
            story += card("Kebiasaan order ulang: produk baru, warna baru, atau item yang sama?",
                          "Tiap order ulang (order ke-2 dst di periode) dibandingkan dengan semua item yang pernah dibeli pelanggan itu "
                          "sebelumnya.", tile_row(ht, 4), Spacer(1, 6), body)
            P1 = (H.get("products") or [])[:PDF_LIST]
            P2 = (H.get("new_products") or [])[:PDF_LIST]
            t_prod = table(["Platform", "Produk", "Pelanggan", "Item ulang", "Sama", "Varian baru", "Bruto"],
                           [[p["channel"], _short(p["product"], 48), fmt(p["customers"]), fmt(p["lines"]), fmt(p["same"]), fmt(p["variant"]), fmt(p["bruto"])] for p in P1]
                           or [["belum ada", "", "", "", "", "", ""]], [.06, .17, .055, .05, .045, .05, .07], left=2)
            t_new = table(["Platform", "Produk", "Pelanggan", "Item", "Bruto"],
                          [[p["channel"], _short(p["product"], 48), fmt(p["customers"]), fmt(p["lines"]), fmt(p["bruto"])] for p in P2]
                          or [["belum ada", "", "", "", ""]], [.06, .17, .055, .05, .085], left=2)
            half2 = W * .54
            tw = Table([[[P("Produk yang paling sering dibeli ulang", "h2"), P("Item di order ulang yang produknya sudah pernah dibeli pelanggan itu. Urut jumlah pelanggan.", "desc"), t_prod], "",
                         [P("Produk baru yang dibeli pelanggan lama", "h2"), P('Kandidat produk "pintu kedua".', "desc"), t_new]]],
                       colWidths=[half2, W * .04, W - half2 - W * .04])
            tw.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
            story += [CondPageBreak(90 * mm), tw, Spacer(1, 8)]
            Q = (H.get("pairs") or [])[:PDF_LIST]
            if Q:
                story += card("Sudah beli apa, lalu beli apa", "Pelanggan yang pernah membeli produk kiri, lalu di order ulangnya (periode) membeli "
                              "produk kanan untuk pertama kali.",
                              table(["Platform", "Pernah beli", "Lalu beli (produk baru)", "Pelanggan"],
                                    [[p["channel"], _short(p["src"], 70), _short(p["dst"], 70), fmt(p["customers"])] for p in Q],
                                    [.08, .41, .41, .1], left=3))

        # produk baru dirilis
        LT = LA.get("totals")
        if LT:
            lt = [("Produk baru dirilis", fmt(LT["launches"]), f'{fmt(LT["orders"])} order · bruto {jt(LT["bruto"])}'),
                  ("Pembeli produk baru", fmt(LT["buyers"]), "pelanggan unik, 3 bulan pertama tiap produk"),
                  ("Orang baru", pct(LT["new_pct"]), f'{fmt(LT["new"])} pembeli yang order pertamanya produk baru ini'),
                  ("Pelanggan lama", pct(LT["old_pct"]), f'{fmt(LT["old"])} pembeli yang sudah pernah order sebelumnya')]
            lr = lambda s: [s["label"], fmt(s["launches"]), fmt(s["buyers"]), fmt(s["new"]), pct(s["new_pct"]), fmt(s["old"]), pct(s["old_pct"]),
                            fmt(s["orders"]), fmt(s["bruto"])]
            rows = [lr(s) for s in LA.get("channels") or []]
            if len(rows) > 1:
                rows.append(lr(LT))
            items = (LA.get("items") or [])[:PDF_LAUNCH]
            story.append(CondPageBreak(80 * mm))
            story += card("Produk baru dirilis: siapa yang membeli?",
                          "Produk yang mulai terjual di suatu bulan tanpa penjualan di 1–3 bulan sebelumnya (min. 3 order di bulan rilis). "
                          "Pembeli dihitung di bulan rilis + 2 bulan berikutnya." + (f" Ditampilkan {PDF_LAUNCH} teratas; lengkapnya di Excel."
                                                                                     if len(LA.get("items") or []) > PDF_LAUNCH else ""),
                          tile_row(lt), Spacer(1, 6),
                          table(["Platform", "Produk baru", "Pembeli", "Orang baru", "%", "Pelanggan lama", "%", "Order", "Bruto"], rows,
                                [.2, .1, .1, .1, .08, .1, .08, .1, .14], total_last=len(rows) > 1), Spacer(1, 6),
                          table(["Bulan rilis", "Platform", "Produk", "Pembeli", "Orang baru", "%", "Pelanggan lama", "%", "Order", "Bruto"],
                                [[month_label(r["month"]), r["label"], _short(r["product"], 80), fmt(r["buyers"]), fmt(r["new"]), pct(r["new_pct"]),
                                  fmt(r["old"]), pct(r["old_pct"]), fmt(r["orders"]), fmt(r["bruto"])] for r in items],
                                [.07, .08, .38, .06, .06, .05, .07, .05, .06, .12], left=3))

        # kohort
        K = min(R.get("cohort_cols") or 0, PDF_COHORT_COLS)
        CO = (R.get("cohorts") or [])[-PDF_COHORT_ROWS:]
        if CO and K:
            rows, cmds = [], []
            for i, c in enumerate(CO, start=1):
                row = [month_label(c["cohort"]), fmt(c["size"])]
                for k in range(K):
                    if k < len(c["pct"]):
                        v = c["pct"][k]
                        row.append(pct(v))
                        if k and v is not None:
                            a = min(1.0, v / 40)
                            bg = colors.Color(94 / 255, 26 / 255, 241 / 255, alpha=0.08 + 0.6 * a)
                            cmds.append(("BACKGROUND", (k + 2, i), (k + 2, i), bg))
                    else:
                        row.append("–")
                rows.append(row)
            note = ""
            if len(R.get("cohorts") or []) > PDF_COHORT_ROWS or (R.get("cohort_cols") or 0) > PDF_COHORT_COLS:
                note = f" Ditampilkan {len(CO)} kohort terakhir, +0 s/d +{K - 1} bulan; lengkapnya di Excel."
            story.append(CondPageBreak(70 * mm))
            story += card("Retensi kohort", "Kohort = bulan order pertama pelanggan. Kolom +k = % pelanggan kohort itu yang order lagi k bulan "
                          "kemudian. Makin pekat = makin banyak yang kembali." + note,
                          table(["Kohort", "Pelanggan"] + [f"+{k}" for k in range(K)], rows, [.08, .08] + [(1 - .16) / K] * K,
                                cell_styles=cmds))

        # pelanggan berulang teratas
        top = R.get("top") or []
        if top:
            hb = lambda h: "–" if not h else f'{h["new"]} / {h["variant"]} / {h["same"]}'
            story.append(CondPageBreak(60 * mm))
            story += card(f"{min(PDF_TOP, len(top))} pelanggan berulang teratas" + (f" (dari {fmt(len(top))})" if len(top) > PDF_TOP else ""),
                          "Pelanggan yang order lebih dari sekali (sepanjang data) dan aktif di periode, urut order terbanyak. Kolom terakhir = "
                          "order ulang: produk baru / warna-varian baru / item sama." + (" Daftar lengkap ada di unduhan Excel." if len(top) > PDF_TOP else ""),
                          table(["Platform", "Pelanggan", "Produk paling sering", "Order periode", "Order total", "Qty", "Bruto periode",
                                 "Order pertama", "Order terakhir", "Jarak rata-rata (hari)", "Baru / warna / sama"],
                                [[t["channel"], _short(t["buyer"], 30), _short(t["product"], 60), fmt(t["orders"]), fmt(t["orders_total"]), fmt(t["qty"]),
                                  fmt(t["bruto"]), t["first"], t["last"], "–" if t.get("gap_avg") is None else fmt(t["gap_avg"]), hb(t.get("habit"))]
                                 for t in top[:PDF_TOP]],
                                [.07, .12, .27, .055, .055, .045, .08, .07, .07, .07, .085], left=3))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(F, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(12 * mm, 7 * mm, f'Sales Akrual · Pelanggan berulang · {meta["toko"]} · {meta["period"]}')
        canvas.drawRightString(landscape(A4)[0] - 12 * mm, 7 * mm, f"hal. {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=11 * mm, bottomMargin=13 * mm,
                            title=f'Pelanggan berulang {meta["toko"]}', author="Sales Akrual")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _short(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"
