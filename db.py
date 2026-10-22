"""Lapisan database Sales Akrual (SQLAlchemy Core + pandas).

Semua hasil parsing file export disimpan per baris di tabel (bukan dibaca ulang dari file tiap Hitung).
Skema mengikuti DataFrame kanonis yang dihasilkan parsers.py, jadi analysis.py / audit.py tidak berubah.

Pilih database lewat env SA_DB_URL:
  (kosong)                                   → SQLite di <SA_DATA_DIR>/sales.db (mode lokal di Mac)
  postgresql+psycopg://user:pass@host/db     → Postgres (VPS, lihat docker-compose.yml)

File export mentah tetap diarsipkan di <SA_DATA_DIR>/<toko>/ (dibutuhkan untuk parse ulang kalau parser berubah).
"""
import os
import json
import time
import datetime as dt

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import (MetaData, Table, Column, Integer, String, Text, Float, Boolean, DateTime,
                        ForeignKey, UniqueConstraint, Index, select, insert, delete, update, func)

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.expanduser(os.environ.get("SA_DATA_DIR") or os.path.join(ROOT, "data")))

meta = MetaData()

toko = Table(
    "toko", meta,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False),
    Column("version", Integer, nullable=False, default=0),   # naik tiap ada perubahan data toko → kunci cache
    Column("hpp_col", String(16), nullable=False, default="hpp"),
    Column("hpp_source", Text, nullable=False, default=""),
    Column("hpp_updated", String(32), nullable=False, default=""),
    Column("ads_notes", Text, nullable=False, default=""),
    Column("ads_prorate", Boolean, nullable=False, default=False),
)

upload = Table(
    "upload", meta,
    Column("id", Integer, primary_key=True),
    Column("toko_id", Integer, ForeignKey("toko.id", ondelete="CASCADE"), nullable=False),
    Column("file", Text, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("mtime", Float, nullable=False),
    Column("size", Integer, nullable=False),
    Column("seller", Text, nullable=False, default=""),
    Column("summary", Text, nullable=False, default="{}"),      # ringkasan resmi platform (JSON) buat audit
    Column("orders_rows", Integer), Column("orders_from", String(10)), Column("orders_to", String(10)),
    Column("income_rows", Integer), Column("income_from", String(10)), Column("income_to", String(10)),
    Column("unsettled_rows", Integer), Column("unsettled_from", String(10)), Column("unsettled_to", String(10)),
    Column("parser_version", String(8), nullable=False),
    Column("uploaded_by", String(64), nullable=False, default=""),
    Column("uploaded_at", DateTime, nullable=False),
    UniqueConstraint("toko_id", "file", name="uq_upload_toko_file"),
)

# kolom kanonis per jenis baris: (nama, tipe). Urutan = urutan kolom DataFrame yang dikembalikan.
ORDER_COLS = [
    ("platform", String(16)), ("channel", String(16)), ("order_id", String(64)), ("created", DateTime),
    ("status_raw", Text), ("status", String(32)), ("is_batal", Boolean), ("is_unpaid", Boolean), ("is_return", Boolean),
    ("sku", Text), ("sku_id", Text), ("product", Text), ("variation", Text), ("qty", Float), ("bruto", Float),
    ("harga_awal", Float), ("payment", Text), ("cancel_reason", Text), ("line_key", Text), ("file", Text), ("file_mtime", Float),
]
INCOME_COLS = [
    ("platform", String(16)), ("channel", String(16)), ("order_id", String(64)), ("created", DateTime), ("settled", DateTime),
    ("neto", Float), ("harga_produk", Float), ("refund", Float), ("ongkir_net", Float), ("voucher_penjual", Float),
    ("biaya_admin", Float), ("biaya_proses", Float), ("biaya_xtra", Float), ("biaya_layanan", Float), ("komisi_ams", Float),
    ("promosi_lain", Float), ("biaya_lainnya", Float), ("pph22", Float), ("komisi_platform", Float), ("komisi_dinamis", Float),
    ("afiliasi", Float), ("total_fees", Float), ("pengajuan", Text), ("line_key", Text), ("file", Text), ("file_mtime", Float),
]
ADJ_COLS = [
    ("platform", String(16)), ("type", Text), ("id", Text), ("related", Text), ("settled", DateTime), ("amount", Float),
    ("file", Text), ("file_mtime", Float),
]
UNSETTLED_COLS = [
    ("platform", String(16)), ("order_id", String(64)), ("est_neto", Float), ("est_when", Text), ("reason", Text),
    ("created", DateTime), ("sku", Text), ("product", Text), ("line_key", Text), ("file", Text), ("file_mtime", Float),
]


def _line_table(name, cols):
    columns = [Column("row_id", Integer, primary_key=True),
               Column("toko_id", Integer, ForeignKey("toko.id", ondelete="CASCADE"), nullable=False),
               Column("upload_id", Integer, ForeignKey("upload.id", ondelete="CASCADE"), nullable=False)]
    for c, t in cols:
        # kolom "id" di adjustment bentrok dengan kebiasaan; simpan apa adanya (nama kolom DataFrame harus sama persis)
        columns.append(Column(c, t))
    return Table(name, meta, *columns,
                 Index(f"ix_{name}_toko", "toko_id"), Index(f"ix_{name}_upload", "upload_id"),
                 Index(f"ix_{name}_order", "toko_id", "order_id") if any(c == "order_id" for c, _ in cols) else Index(f"ix_{name}_id", "toko_id", "id"))


order_line = _line_table("order_line", ORDER_COLS)
income_line = _line_table("income_line", INCOME_COLS)
adjustment_line = _line_table("adjustment_line", ADJ_COLS)
unsettled_line = _line_table("unsettled_line", UNSETTLED_COLS)
LINE_TABLES = {"orders": (order_line, ORDER_COLS), "income": (income_line, INCOME_COLS),
               "adj": (adjustment_line, ADJ_COLS), "unsettled": (unsettled_line, UNSETTLED_COLS)}

hpp_item = Table(
    "hpp_item", meta,
    Column("id", Integer, primary_key=True),
    Column("toko_id", Integer, ForeignKey("toko.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("kode", Text, nullable=False, default=""),
    Column("hpp", Float), Column("pembulatan", Float), Column("harga_jual", Float),
    Column("manual", Boolean, nullable=False, default=False),   # True = ditambah manual di app, bukan dari sheet
    Column("position", Integer, nullable=False, default=0),
    Index("ix_hpp_item_toko", "toko_id"),
)

hpp_map = Table(
    "hpp_map", meta,
    Column("id", Integer, primary_key=True),
    Column("toko_id", Integer, ForeignKey("toko.id", ondelete="CASCADE"), nullable=False),
    Column("key", Text, nullable=False),        # "<produk listing>" atau "<produk>|<sku>"
    Column("item", Text, nullable=False),       # nama item HPP; "" = sengaja tanpa HPP
    UniqueConstraint("toko_id", "key", name="uq_hpp_map"),
)

ads_channel = Table(
    "ads_channel", meta,
    Column("id", Integer, primary_key=True),
    Column("toko_id", Integer, ForeignKey("toko.id", ondelete="CASCADE"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("attribution", String(16), nullable=False, default="all"),
    Column("position", Integer, nullable=False, default=0),
    UniqueConstraint("toko_id", "channel", name="uq_ads_channel"),
)

ads_month = Table(
    "ads_month", meta,
    Column("id", Integer, primary_key=True),
    Column("toko_id", Integer, ForeignKey("toko.id", ondelete="CASCADE"), nullable=False),
    Column("month", String(7), nullable=False),
    Column("channel", Text, nullable=False),
    Column("amount", Float, nullable=False, default=0.0),
    UniqueConstraint("toko_id", "month", "channel", name="uq_ads_month"),
)

app_user = Table(
    "app_user", meta,
    Column("name", String(64), primary_key=True),
    Column("hash", Text, nullable=False),
    Column("role", String(16), nullable=False, default="user"),
    Column("created", Integer, nullable=False, default=0),
    Column("pw_changed", Integer, nullable=False, default=0),
)


# ---------------------------------------------------------------- engine

_engine = None


def db_url():
    u = os.environ.get("SA_DB_URL", "").strip()
    if u:
        return u
    os.makedirs(DATA, exist_ok=True)
    return "sqlite:///" + os.path.join(DATA, "sales.db")


def engine():
    global _engine
    if _engine is None:
        url = db_url()
        if url.startswith("sqlite"):
            _engine = sa.create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 30})

            @sa.event.listens_for(_engine, "connect")
            def _pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            _engine = sa.create_engine(url, future=True, pool_pre_ping=True)
        meta.create_all(_engine)
    return _engine


def is_sqlite():
    return db_url().startswith("sqlite")


# ---------------------------------------------------------------- toko

def safe_name(name):
    safe = "".join(c for c in (name or "") if c.isalnum() or c in " -_").strip()
    if not safe:
        raise ValueError("nama toko kosong")
    return safe


def archive_dir(name):
    return os.path.join(DATA, safe_name(name))


def list_toko():
    with engine().connect() as cx:
        return [r[0] for r in cx.execute(select(toko.c.name).order_by(toko.c.name))]


def toko_id(cx, name, create=False):
    name = safe_name(name)
    tid = cx.execute(select(toko.c.id).where(toko.c.name == name)).scalar()
    if tid is None and create:
        tid = cx.execute(insert(toko).values(name=name, created_at=dt.datetime.now(), version=0)).inserted_primary_key[0]
    return tid


def create_toko(name):
    with engine().begin() as cx:
        toko_id(cx, name, create=True)
    os.makedirs(archive_dir(name), exist_ok=True)
    return safe_name(name)


def delete_toko(name):
    with engine().begin() as cx:
        tid = toko_id(cx, name)
        if tid is None:
            return False
        for t in (order_line, income_line, adjustment_line, unsettled_line, upload, hpp_item, hpp_map, ads_channel, ads_month):
            cx.execute(delete(t).where(t.c.toko_id == tid))
        cx.execute(delete(toko).where(toko.c.id == tid))
    return True


def bump(cx, tid):
    cx.execute(update(toko).where(toko.c.id == tid).values(version=toko.c.version + 1))


def version(name):
    with engine().connect() as cx:
        return cx.execute(select(toko.c.version).where(toko.c.name == safe_name(name))).scalar()


def file_count(name):
    with engine().connect() as cx:
        tid = toko_id(cx, name)
        return int(cx.execute(select(func.count()).select_from(upload).where(upload.c.toko_id == tid)).scalar() or 0)


# ---------------------------------------------------------------- ingest file export

def _is(t, cls):
    return issubclass(t, cls) if isinstance(t, type) else isinstance(t, cls)


def _df_for_insert(df, cols, tid, uid):
    out = pd.DataFrame(index=df.index)
    for c, t in cols:
        if c in df:
            s = df[c]
            if _is(t, DateTime):
                s = pd.to_datetime(s, errors="coerce")
                s = s.astype(object).where(s.notna(), None)
            elif _is(t, Boolean):
                s = s.fillna(False).astype(bool)
            elif _is(t, Float):
                s = pd.to_numeric(s, errors="coerce").astype(float)
            else:
                s = s.astype(object).where(s.notna(), None)
                s = s.map(lambda v: None if v is None else str(v))
            out[c] = s
        else:
            out[c] = None
    out.insert(0, "upload_id", uid)
    out.insert(0, "toko_id", tid)
    return out


def _insert_df(cx, table, df):
    if not len(df):
        return
    recs = df.to_dict("records")
    for i in range(0, len(recs), 2000):
        cx.execute(insert(table), recs[i:i + 2000])


def ingest_file(name, path, uploaded_by=""):
    """Parse satu file export dan simpan barisnya. File dengan nama sama di toko yang sama diganti. → meta dict"""
    import parsers
    r = parsers.parse_file(path)   # tanpa cache pickle: hasilnya masuk database
    if r["kind"] is None:
        return None
    m = r["meta"]
    st = os.stat(path)
    with engine().begin() as cx:
        tid = toko_id(cx, name, create=True)
        old = cx.execute(select(upload.c.id).where(upload.c.toko_id == tid, upload.c.file == m["file"])).scalar()
        if old is not None:
            for t, _ in LINE_TABLES.values():
                cx.execute(delete(t).where(t.c.upload_id == old))
            cx.execute(delete(upload).where(upload.c.id == old))
        row = dict(toko_id=tid, file=m["file"], kind=r["kind"], mtime=float(m["mtime"]), size=int(st.st_size), seller=m.get("seller") or "",
                   summary=json.dumps(m.get("summary") or {}, ensure_ascii=False, default=str),
                   parser_version=parsers.PARSER_VERSION, uploaded_by=uploaded_by or "", uploaded_at=dt.datetime.now())
        for k in ("orders", "income", "unsettled"):
            row[k + "_rows"] = m.get(k + "_rows")
            row[k + "_from"] = m.get(k + "_from")
            row[k + "_to"] = m.get(k + "_to")
        uid = cx.execute(insert(upload).values(**row)).inserted_primary_key[0]
        for k, (t, cols) in LINE_TABLES.items():
            df = r.get(k)
            if df is not None and len(df):
                _insert_df(cx, t, _df_for_insert(df, cols, tid, uid))
        bump(cx, tid)
    return dict(file=m["file"], kind=r["kind"])


def delete_file(name, file):
    with engine().begin() as cx:
        tid = toko_id(cx, name)
        if tid is None:
            return False
        uid = cx.execute(select(upload.c.id).where(upload.c.toko_id == tid, upload.c.file == file)).scalar()
        if uid is None:
            return False
        for t, _ in LINE_TABLES.values():
            cx.execute(delete(t).where(t.c.upload_id == uid))
        cx.execute(delete(upload).where(upload.c.id == uid))
        bump(cx, tid)
    return True


# ---------------------------------------------------------------- baca → DataFrame kanonis

def _read_lines(cx, table, cols, tid):
    names = [c for c, _ in cols]
    df = pd.read_sql(select(*[table.c[c] for c in names]).where(table.c.toko_id == tid).order_by(table.c.row_id), cx)
    if not len(df):
        return pd.DataFrame(columns=names)
    for c, t in cols:
        if _is(t, DateTime):
            df[c] = pd.to_datetime(df[c], errors="coerce")
        elif _is(t, Boolean):
            df[c] = df[c].fillna(False).astype(bool)
        elif _is(t, Float):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def load_frames(name):
    """→ (orders, income, adj, unsettled, files_meta) persis seperti hasil parse semua file di folder toko."""
    import parsers
    with engine().connect() as cx:
        tid = toko_id(cx, name)
        if tid is None:
            raise KeyError(name)
        ups = cx.execute(select(upload).where(upload.c.toko_id == tid).order_by(upload.c.file)).mappings().all()
        O = _read_lines(cx, order_line, ORDER_COLS, tid)
        I = _read_lines(cx, income_line, INCOME_COLS, tid)
        A = _read_lines(cx, adjustment_line, ADJ_COLS, tid)
        U = _read_lines(cx, unsettled_line, UNSETTLED_COLS, tid)
    files = []
    for u in ups:
        m = dict(file=u["file"], mtime=u["mtime"], seller=u["seller"] or "", kind=u["kind"],
                 label=parsers.KINDS.get(u["kind"], "Tidak dikenali"), uploaded_by=u["uploaded_by"], uploaded_at=str(u["uploaded_at"])[:16])
        try:
            m["summary"] = json.loads(u["summary"] or "{}")
        except ValueError:
            m["summary"] = {}
        for k in ("orders", "income", "unsettled"):
            if u[k + "_rows"] is not None:
                m[k + "_rows"] = u[k + "_rows"]
                m[k + "_from"] = u[k + "_from"]
                m[k + "_to"] = u[k + "_to"]
        files.append(m)
    return O, I, A, U, files


# ---------------------------------------------------------------- HPP

def hpp_load(name):
    with engine().connect() as cx:
        tid = toko_id(cx, name)
        if tid is None:
            return {"items": [], "extra": [], "col": "hpp", "source": "", "updated": ""}, {}
        t = cx.execute(select(toko).where(toko.c.id == tid)).mappings().one()
        rows = cx.execute(select(hpp_item).where(hpp_item.c.toko_id == tid).order_by(hpp_item.c.manual, hpp_item.c.position, hpp_item.c.id)).mappings().all()
        mp = cx.execute(select(hpp_map.c.key, hpp_map.c.item).where(hpp_map.c.toko_id == tid)).all()
    items = [dict(name=r["name"], kode=r["kode"] or "", hpp=r["hpp"], pembulatan=r["pembulatan"], harga_jual=r["harga_jual"]) for r in rows if not r["manual"]]
    extra = [dict(name=r["name"], kode=r["kode"] or "", hpp=r["hpp"], pembulatan=r["pembulatan"], harga_jual=r["harga_jual"]) for r in rows if r["manual"]]
    master = {"items": items, "extra": extra, "col": t["hpp_col"] or "hpp", "source": t["hpp_source"] or "", "updated": t["hpp_updated"] or ""}
    return master, {k: v for k, v in mp}


def hpp_save(name, master=None, mapping=None):
    with engine().begin() as cx:
        tid = toko_id(cx, name, create=True)
        if master is not None:
            cx.execute(update(toko).where(toko.c.id == tid).values(
                hpp_col=master.get("col") or "hpp", hpp_source=master.get("source") or "", hpp_updated=master.get("updated") or ""))
            cx.execute(delete(hpp_item).where(hpp_item.c.toko_id == tid))
            recs = []
            for i, it in enumerate(it for it in master.get("items", []) if not it.get("manual")):
                recs.append(dict(toko_id=tid, name=it["name"], kode=it.get("kode") or "", hpp=it.get("hpp"), pembulatan=it.get("pembulatan"),
                                 harga_jual=it.get("harga_jual"), manual=False, position=i))
            for i, it in enumerate(master.get("extra", [])):
                recs.append(dict(toko_id=tid, name=it["name"], kode=it.get("kode") or "", hpp=it.get("hpp"), pembulatan=it.get("pembulatan"),
                                 harga_jual=it.get("harga_jual"), manual=True, position=i))
            if recs:
                cx.execute(insert(hpp_item), recs)
        if mapping is not None:
            cx.execute(delete(hpp_map).where(hpp_map.c.toko_id == tid))
            recs = [dict(toko_id=tid, key=k, item=v or "") for k, v in mapping.items()]
            if recs:
                cx.execute(insert(hpp_map), recs)
        bump(cx, tid)
    export_settings(name)


# ---------------------------------------------------------------- Iklan

def ads_load(name, defaults):
    d = dict(defaults)
    with engine().connect() as cx:
        tid = toko_id(cx, name)
        if tid is None:
            return d
        t = cx.execute(select(toko.c.ads_notes, toko.c.ads_prorate).where(toko.c.id == tid)).one()
        ch = cx.execute(select(ads_channel.c.channel, ads_channel.c.attribution).where(ads_channel.c.toko_id == tid).order_by(ads_channel.c.position)).all()
        ms = cx.execute(select(ads_month.c.month, ads_month.c.channel, ads_month.c.amount).where(ads_month.c.toko_id == tid)).all()
    if ch:
        d["channels"] = [c for c, _ in ch]
        d["attribution"] = {c: a for c, a in ch}
    months = {}
    for m, c, v in ms:
        months.setdefault(m, {})[c] = float(v or 0)
    if ms or ch:
        d["months"] = months
    d["notes"] = t[0] or ""
    d["prorate"] = bool(t[1])
    return d


def ads_save(name, clean):
    with engine().begin() as cx:
        tid = toko_id(cx, name, create=True)
        cx.execute(update(toko).where(toko.c.id == tid).values(ads_notes=clean.get("notes") or "", ads_prorate=bool(clean.get("prorate"))))
        cx.execute(delete(ads_channel).where(ads_channel.c.toko_id == tid))
        cx.execute(delete(ads_month).where(ads_month.c.toko_id == tid))
        chans = clean.get("channels") or []
        attr = clean.get("attribution") or {}
        if chans:
            cx.execute(insert(ads_channel), [dict(toko_id=tid, channel=c, attribution=attr.get(c, "all"), position=i) for i, c in enumerate(chans)])
        recs = [dict(toko_id=tid, month=m, channel=c, amount=float(v or 0)) for m, row in (clean.get("months") or {}).items() for c, v in row.items()]
        if recs:
            cx.execute(insert(ads_month), recs)
        bump(cx, tid)
    export_settings(name)


def export_settings(name):
    """Tulis hpp.json / hpp_map.json / ads.json ke folder arsip <DATA>/<toko>/ supaya arsip (yang disimpan di git)
    lengkap untuk membangun ulang database lewat migrate.py. Dipanggil tiap HPP/iklan disimpan."""
    master, mapping = hpp_load(name)
    ads = ads_load(name, {"channels": [], "attribution": {}, "months": {}, "notes": "", "prorate": False})
    d = archive_dir(name)
    os.makedirs(d, exist_ok=True)
    def dump(fn, obj):
        tmp = os.path.join(d, fn + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(d, fn))
    dump("hpp.json", {k: v for k, v in master.items() if k != "items"} | {"items": master["items"]})
    dump("hpp_map.json", mapping)
    if ads["channels"] or ads["months"]:
        dump("ads.json", ads)


# ---------------------------------------------------------------- pengguna

def users_load():
    with engine().connect() as cx:
        rows = cx.execute(select(app_user)).mappings().all()
    return {r["name"]: dict(hash=r["hash"], role=r["role"], created=r["created"], pw_changed=r["pw_changed"]) for r in rows}


def users_save(users):
    with engine().begin() as cx:
        cx.execute(delete(app_user))
        if users:
            cx.execute(insert(app_user), [dict(name=k, hash=v["hash"], role=v.get("role", "user"), created=int(v.get("created") or 0),
                                               pw_changed=int(v.get("pw_changed") or 0)) for k, v in users.items()])


def stats():
    with engine().connect() as cx:
        out = {}
        for t in (toko, upload, order_line, income_line, adjustment_line, unsettled_line, hpp_item, hpp_map, ads_month, app_user):
            out[t.name] = int(cx.execute(select(func.count()).select_from(t)).scalar() or 0)
    return out
