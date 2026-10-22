"""HPP (harga pokok) per toko: impor dari Google Sheet / xlsx / csv, pencocokan ke nama listing marketplace, gross profit.

File per toko (di data/<toko>/):
  hpp.json      → {"items": [{"name", "hpp", "pembulatan", "harga_jual"}], "col": "hpp"|"pembulatan", "source": "...", "updated": "..."}
  hpp_map.json  → {"<nama produk listing>": "<name item HPP>" | ""}   ("" = sengaja tidak dipetakan)
"""
import io
import os
import re
import json
import datetime as dt
import urllib.request
import pandas as pd
import openpyxl

STOP = {"eltahj", "hijab", "kerudung", "jilbab", "sport", "instant", "olahraga", "set", "the", "dan", "&", "-", "–", "|"}


def _norm_tokens(s):
    s = str(s or "").lower().replace("–", " ").replace("|", " ")
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t]
    return toks


def norm_code(c):
    """'PG-28C' → 'PG28C'; 'BUNDLING KMB+KSFB' → 'KMB+KSFB'."""
    c = re.sub(r"[^A-Z0-9+]", "", str(c or "").upper())
    c = re.sub(r"^BUNDLING", "", c)
    return c


def code_index(items):
    """kode (ternormalisasi) → nama item. Field 'kode' boleh berisi beberapa alias dipisah '/'."""
    idx = {}
    for it in items:
        for c in str(it.get("kode") or "").split("/"):
            c = norm_code(c)
            if c:
                idx[c] = it["name"]
    return idx


def load(toko):
    """Master HPP + pemetaan dari database. `toko` = nama toko."""
    import db
    master, mapping = db.hpp_load(toko)
    # item manual (bukan dari sheet) digabung; nama sama → manual menang
    if master.get("extra"):
        names = {e["name"] for e in master["extra"]}
        master["items"] = [i for i in master["items"] if i["name"] not in names] + [dict(e, manual=True) for e in master["extra"]]
    return master, mapping


def save(toko, master=None, mapping=None):
    import db
    if master is not None:
        m = dict(master)
        m["items"] = [i for i in m.get("items", []) if not i.get("manual")]
        master = m
    db.hpp_save(toko, master=master, mapping=mapping)


# ---------------------------------------------------------------- impor

def _sheet_export_url(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return None
    gid = re.search(r"[#&?]gid=(\d+)", url)
    u = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    return u, (gid.group(1) if gid else None)


def fetch_sheet(url):
    r = _sheet_export_url(url)
    if not r:
        raise ValueError("bukan link Google Sheet")
    u, gid = r
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    if "spreadsheetml" not in ctype and not data[:2] == b"PK":
        raise ValueError("sheet tidak bisa diunduh — pastikan link dibagikan 'siapa saja yang punya link'")
    return data


def parse_items(data, filename="sheet.xlsx"):
    """Cari sheet/baris header yang punya kolom 'Nama Produk' & 'HPP'."""
    low = filename.lower()
    if low.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(data), dtype=str)
        frames = [df]
    else:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        frames = []
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            hi = None
            for i, r in enumerate(rows[:15]):
                cells = [str(c).strip().lower() if c is not None else "" for c in r]
                if "nama produk" in cells and "hpp" in cells:
                    hi = i
                    break
            if hi is None:
                continue
            hdr = [str(c).strip() if c is not None else "" for c in rows[hi]]
            df = pd.DataFrame(rows[hi + 1:], columns=hdr + [f"_{k}" for k in range(len(rows[hi + 1]) - len(hdr))] if rows[hi + 1:] and len(rows[hi + 1]) > len(hdr) else hdr)
            frames.append(df)
    items = []
    for df in frames:
        cols = {str(c).strip().lower(): c for c in df.columns}
        cn, ch = cols.get("nama produk"), cols.get("hpp")
        if cn is None or ch is None:
            continue
        cp, cj = cols.get("pembulatan"), cols.get("harga jual")
        ck = cols.get("kode") or cols.get("sku") or cols.get("kode sku")
        for _, r in df.iterrows():
            name = str(r[cn]).strip() if r[cn] is not None else ""
            if not name or name.lower() in ("none", "nan"):
                continue
            def num(c):
                if c is None:
                    return None
                v = r[c]
                try:
                    v = float(str(v).replace(".", "").replace(",", ".")) if isinstance(v, str) and re.match(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$", v) else float(v)
                    return v if v == v else None
                except (TypeError, ValueError):
                    return None
            h = num(ch)
            if h is None:
                continue
            items.append(dict(name=name, hpp=round(h), pembulatan=(round(num(cp)) if num(cp) is not None else None),
                              harga_jual=(round(num(cj)) if num(cj) is not None else None),
                              kode=(str(r[ck]).strip() if ck is not None and r[ck] is not None and str(r[ck]).strip().lower() not in ("none", "nan", "-") else "")))
    if not items:
        raise ValueError("tidak ketemu kolom 'Nama Produk' + 'HPP' di file")
    # nama ganda → ambil yang terakhir
    seen = {}
    for it in items:
        seen[it["name"]] = it
    return list(seen.values())


def import_from(toko_dir, data=None, filename="sheet.xlsx", url=None):
    if url:
        data = fetch_sheet(url)
        filename = "sheet.xlsx"
    items = parse_items(data, filename)
    master, mapping = load(toko_dir)
    master.update(items=items, source=url or filename, updated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    save(toko_dir, master=master)
    return load(toko_dir)[0]


def add_item(toko_dir, name, hpp_value, harga_jual=None, kode=""):
    master, _ = load(toko_dir)
    extra = [e for e in master.get("extra", []) if e["name"] != name]
    extra.append(dict(name=name, hpp=round(float(hpp_value)), pembulatan=round(float(hpp_value)), harga_jual=harga_jual, kode=kode or ""))
    master["extra"] = extra
    master["items"] = [i for i in master["items"] if not i.get("manual")]
    save(toko_dir, master=master)
    return load(toko_dir)[0]


def remove_item(toko_dir, name):
    master, _ = load(toko_dir)
    master["extra"] = [e for e in master.get("extra", []) if e["name"] != name]
    master["items"] = [i for i in master["items"] if not i.get("manual")]
    save(toko_dir, master=master)
    return load(toko_dir)[0]


# ---------------------------------------------------------------- pencocokan

def auto_match(product, items):
    """→ (nama item, skor) kandidat terbaik untuk satu nama listing."""
    ptoks = _norm_tokens(product)
    pset = set(ptoks)
    # berapa item yang berbagi nama model (token inti pertama) → kalau unik, boleh longgar
    first_count = {}
    for it in items:
        c = [t for t in _norm_tokens(it["name"]) if t not in STOP] or _norm_tokens(it["name"])
        if c:
            first_count[c[0]] = first_count.get(c[0], 0) + 1
    best, best_score, best_unique = None, 0.0, False
    for it in items:
        itoks = _norm_tokens(it["name"])
        if not itoks:
            continue
        core = [t for t in itoks if t not in STOP] or itoks
        if core[0] not in pset:              # nama model (token pertama) wajib ada
            continue
        hit = sum(1 for t in core if t in pset)
        score = hit / len(core)
        if score > best_score or (score == best_score and best and len(core) > len(_norm_tokens(best))):
            best, best_score, best_unique = it["name"], score, first_count.get(core[0], 0) == 1
    if best is None:
        return None, 0.0
    if best_score < 0.75 and not best_unique:   # nama model dipakai >1 item → butuh kecocokan kuat
        return None, best_score
    return best, best_score


SEP = "\x1f"


def resolve_pairs(pairs, items, mapping, col="hpp"):
    """pairs: iterable (product, sku) → dict (product, sku) → dict(item, items, hpp, source).
    Urutan: manual per-SKU → kode SKU (termasuk bundle 'A+B' = jumlah) → manual per-listing → nama otomatis."""
    by_name = {it["name"]: it for it in items}
    cidx = code_index(items)

    def unit(name):
        it = by_name.get(name)
        if not it:
            return 0.0
        v = it.get(col) if col in ("hpp", "pembulatan") else it.get("hpp")
        return float(v if v is not None else (it.get("hpp") or 0))

    out, auto_cache = {}, {}
    for p, sku in pairs:
        key = (p, sku)
        if key in out:
            continue
        mk = p + SEP + (sku or "")
        if sku and mk in mapping:
            m = mapping[mk]
            out[key] = dict(item=m if m in by_name else None, items=[m] if m in by_name else [], hpp=unit(m) if m in by_name else None, source="manual" if m else "skip")
            continue
        code = norm_code(sku)
        if code:
            parts = [c for c in code.split("+") if c]
            names = [cidx.get(c) for c in parts]
            if parts and all(names):
                out[key] = dict(item=" + ".join(names) if len(names) > 1 else names[0], items=names, hpp=sum(unit(n) for n in names),
                                source="bundle" if len(names) > 1 else "kode")
                continue
        if p in mapping:
            m = mapping[p]
            out[key] = dict(item=m if m in by_name else None, items=[m] if m in by_name else [], hpp=unit(m) if m in by_name else None, source="manual" if m else "skip")
            continue
        if p not in auto_cache:
            auto_cache[p] = auto_match(p, items)[0]
        m = auto_cache[p]
        out[key] = dict(item=m, items=[m] if m else [], hpp=unit(m) if m else None, source="auto" if m else "none")
    return out


def resolve(products, items, mapping):
    """kompatibilitas: per nama listing saja."""
    r = resolve_pairs([(p, "") for p in products], items, mapping)
    return {p: (r[(p, "")]["item"], r[(p, "")]["source"]) for p in products}


def apply(O, toko_dir):
    """Tambah kolom hpp_item & hpp_unit ke DataFrame orders. Tanpa master → kolom kosong."""
    master, mapping = load(toko_dir)
    items = master.get("items", [])
    col = master.get("col", "hpp")
    if not len(O):
        return O
    O = O.copy()
    if not items:
        O["hpp_item"] = None
        O["hpp_unit"] = 0.0
        return O
    sku = O["sku"].fillna("").astype(str) if "sku" in O else pd.Series([""] * len(O), index=O.index)
    pairs = set(zip(O["product"], sku))
    res = resolve_pairs(pairs, items, mapping, col)
    keys = list(zip(O["product"], sku))
    O["hpp_item"] = [res[k]["item"] for k in keys]
    O["hpp_unit"] = [float(res[k]["hpp"] or 0.0) for k in keys]
    O["hpp_source"] = [res[k]["source"] for k in keys]
    return O


def mapping_table(O, toko_dir):
    """Daftar produk listing + hasil pencocokan + volume, buat UI."""
    master, mapping = load(toko_dir)
    items = master.get("items", [])
    if not len(O):
        return []
    sold = O[(~O["is_unpaid"]) & (~O["is_batal"]) & (~O.get("is_retur", False))].copy()
    sold["sku"] = sold["sku"].fillna("").astype(str)
    g = sold.groupby(["product", "sku"]).agg(qty=("qty", "sum"), bruto=("bruto", "sum"), variation=("variation", lambda s: ", ".join(sorted(set(x for x in s if x))[:3]))).reset_index()
    col = master.get("col", "hpp")
    res = resolve_pairs(zip(g["product"], g["sku"]), items, mapping, col)
    rows = []
    for _, r in g.iterrows():
        x = res[(r["product"], r["sku"])]
        rows.append(dict(product=r["product"], sku=r["sku"], variation=r["variation"], qty=float(r["qty"]), bruto=float(r["bruto"]),
                         item=x["item"], source=x["source"], hpp=x["hpp"]))
    rows.sort(key=lambda x: (x["item"] is not None, -x["bruto"]))
    return rows
