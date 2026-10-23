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
import contextvars
import datetime as dt

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import (MetaData, Table, Column, Integer, String, Text, Float, Boolean, DateTime,
                        ForeignKey, UniqueConstraint, Index, select, insert, delete, update, func)

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.expanduser(os.environ.get("SA_DATA_DIR") or os.path.join(ROOT, "data")))

# ---------------------------------------------------------------- workspace aktif
# Mode SaaS: tiap pelanggan punya workspace sendiri; toko, upload, HPP, iklan semua terikat ke workspace.
# Workspace aktif disimpan di contextvar yang di-set middleware app.py per request (dari sesi login).
# Default 1 = workspace pertama ("Mierakigai"): dipakai mode lokal, CLI, migrate.py.
DEFAULT_WS = 1
_ws = contextvars.ContextVar("sa_workspace", default=DEFAULT_WS)


def current_ws():
    return _ws.get()


def set_ws(ws_id):
    _ws.set(int(ws_id or DEFAULT_WS))


meta = MetaData()

workspace = Table(
    "workspace", meta,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("owner", String(254), nullable=False, default=""),      # app_user.name pemilik
    Column("created_at", DateTime, nullable=False),
    Column("status", String(16), nullable=False, default="active"),   # active | suspended
)

# data diri pendaftar (satu per workspace) — database pelanggan untuk superadmin
customer = Table(
    "customer", meta,
    Column("id", Integer, primary_key=True),
    Column("workspace_id", Integer, ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("name", String(120), nullable=False, default=""),
    Column("email", String(254), nullable=False, default=""),
    Column("phone", String(32), nullable=False, default=""),
    Column("business", String(120), nullable=False, default=""),
    Column("city", String(80), nullable=False, default=""),
    Column("created_at", DateTime, nullable=False),
)

ws_invite = Table(
    "ws_invite", meta,
    Column("id", Integer, primary_key=True),
    Column("workspace_id", Integer, ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
    Column("email", String(254), nullable=False),
    Column("role", String(16), nullable=False, default="user"),
    Column("invited_by", String(254), nullable=False, default=""),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("workspace_id", "email", name="uq_ws_invite"),
)

toko = Table(
    "toko", meta,
    Column("id", Integer, primary_key=True),
    Column("workspace_id", Integer, ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, default=DEFAULT_WS),
    Column("name", String(120), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("version", Integer, nullable=False, default=0),   # naik tiap ada perubahan data toko → kunci cache
    Column("hpp_col", String(16), nullable=False, default="hpp"),
    Column("hpp_source", Text, nullable=False, default=""),
    Column("hpp_updated", String(32), nullable=False, default=""),
    Column("ads_notes", Text, nullable=False, default=""),
    Column("ads_prorate", Boolean, nullable=False, default=False),
    UniqueConstraint("workspace_id", "name", name="uq_toko_ws_name"),
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
    Column("name", String(254), primary_key=True),              # username (login password) atau email (login Google)
    Column("hash", Text, nullable=False),                       # "" = akun Google tanpa password
    Column("role", String(16), nullable=False, default="user"),  # peran di dalam workspace-nya
    Column("created", Integer, nullable=False, default=0),
    Column("pw_changed", Integer, nullable=False, default=0),
    Column("workspace_id", Integer, ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False, default=DEFAULT_WS),
    Column("email", String(254), nullable=False, default=""),
    Column("google_sub", String(64), nullable=False, default=""),
    Column("display_name", String(120), nullable=False, default=""),
    Column("picture", Text, nullable=False, default=""),
    Column("fail_n", Integer, nullable=False, default=0),       # login password gagal beruntun (reset saat berhasil)
    Column("lock_until", Integer, nullable=False, default=0),   # epoch: akun dikunci sampai waktu ini (0 = tidak)
)

USER_EXTRA_COLS = ("workspace_id", "email", "google_sub", "display_name", "picture", "fail_n", "lock_until")


def _migrate_schema(eng):
    """Tambah kolom/tabel versi SaaS ke database lama (sebelum ada workspace). Aman diulang."""
    def run(cx, sql):
        sp = cx.begin_nested()   # savepoint: DDL yang gagal (mis. kolom sudah ada) tidak membatalkan transaksi utama
        try:
            cx.execute(sa.text(sql))
            sp.commit()
            return True
        except Exception:
            sp.rollback()
            return False

    pg = not db_url().startswith("sqlite")
    with eng.begin() as cx:
        cols_toko = {c["name"] for c in sa.inspect(cx).get_columns("toko")}
        if "workspace_id" not in cols_toko:
            run(cx, f"ALTER TABLE toko ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT {DEFAULT_WS}")
        cols_ws = {c["name"] for c in sa.inspect(cx).get_columns("workspace")}
        if "status" not in cols_ws:
            run(cx, "ALTER TABLE workspace ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active'")
        cols_user = {c["name"] for c in sa.inspect(cx).get_columns("app_user")}
        for col, ddl in (("workspace_id", f"INTEGER NOT NULL DEFAULT {DEFAULT_WS}"), ("email", "VARCHAR(254) NOT NULL DEFAULT ''"),
                         ("google_sub", "VARCHAR(64) NOT NULL DEFAULT ''"), ("display_name", "VARCHAR(120) NOT NULL DEFAULT ''"),
                         ("picture", "TEXT NOT NULL DEFAULT ''"), ("fail_n", "INTEGER NOT NULL DEFAULT 0"),
                         ("lock_until", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols_user:
                run(cx, f"ALTER TABLE app_user ADD COLUMN {col} {ddl}")
        if pg:
            run(cx, "ALTER TABLE app_user ALTER COLUMN name TYPE VARCHAR(254)")
            run(cx, "ALTER TABLE toko DROP CONSTRAINT IF EXISTS toko_name_key")   # unik dulu per nama → kini per (workspace, nama)
            run(cx, "CREATE UNIQUE INDEX IF NOT EXISTS uq_toko_ws_name ON toko (workspace_id, name)")
        if cx.execute(select(func.count()).select_from(workspace).where(workspace.c.id == DEFAULT_WS)).scalar() == 0:
            owner = cx.execute(select(app_user.c.name).where(app_user.c.role == "admin").order_by(app_user.c.created)).scalar() or ""
            cx.execute(insert(workspace).values(id=DEFAULT_WS, name=os.environ.get("SA_WS_NAME", "Mierakigai"), owner=owner,
                                                created_at=dt.datetime.now()))
        if pg:
            run(cx, "SELECT setval(pg_get_serial_sequence('workspace','id'), GREATEST((SELECT MAX(id) FROM workspace), 1))")
            # FK workspace_id → workspace (database lama dibuat sebelum FK ini ada di definisi tabel). SQLite tidak bisa
            # ADD CONSTRAINT; di sana FK hanya untuk database yang dibuat baru lewat create_all.
            for t in ("toko", "app_user", "customer"):
                if not any(fk["referred_table"] == "workspace" for fk in sa.inspect(cx).get_foreign_keys(t)):
                    run(cx, f"ALTER TABLE {t} ADD CONSTRAINT fk_{t}_workspace FOREIGN KEY (workspace_id) "
                            f"REFERENCES workspace (id) ON DELETE CASCADE")


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
        _migrate_schema(_engine)
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
    """Folder arsip file mentah. Workspace pertama tetap <DATA>/<toko>/ (kompatibel dengan arsip lama),
    workspace lain di <DATA>/_ws/<id>/<toko>/."""
    ws = current_ws()
    if ws == DEFAULT_WS:
        return os.path.join(DATA, safe_name(name))
    return os.path.join(DATA, "_ws", str(ws), safe_name(name))


def list_toko():
    with engine().connect() as cx:
        return [r[0] for r in cx.execute(select(toko.c.name).where(toko.c.workspace_id == current_ws()).order_by(toko.c.name))]


def toko_id(cx, name, create=False):
    name = safe_name(name)
    ws = current_ws()
    tid = cx.execute(select(toko.c.id).where(toko.c.name == name, toko.c.workspace_id == ws)).scalar()
    if tid is None and create:
        tid = cx.execute(insert(toko).values(name=name, workspace_id=ws, created_at=dt.datetime.now(), version=0)).inserted_primary_key[0]
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
        return cx.execute(select(toko.c.version).where(toko.c.name == safe_name(name), toko.c.workspace_id == current_ws())).scalar()


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

def _user_row(r):
    return dict(hash=r["hash"], role=r["role"], created=r["created"], pw_changed=r["pw_changed"],
                workspace_id=int(r["workspace_id"] or DEFAULT_WS), email=r["email"] or "", google_sub=r["google_sub"] or "",
                display_name=r["display_name"] or "", picture=r["picture"] or "",
                fail_n=int(r["fail_n"] or 0), lock_until=int(r["lock_until"] or 0))


def users_load():
    """Semua akun (lintas workspace) → {name: {...}}. Dipakai auth untuk login/sesi."""
    with engine().connect() as cx:
        rows = cx.execute(select(app_user)).mappings().all()
    return {r["name"]: _user_row(r) for r in rows}


def _user_values(k, v):
    return dict(name=k, hash=v.get("hash", ""), role=v.get("role", "user"), created=int(v.get("created") or 0),
                pw_changed=int(v.get("pw_changed") or 0), workspace_id=int(v.get("workspace_id") or DEFAULT_WS),
                email=v.get("email", "") or "", google_sub=v.get("google_sub", "") or "",
                display_name=v.get("display_name", "") or "", picture=v.get("picture", "") or "",
                fail_n=int(v.get("fail_n") or 0), lock_until=int(v.get("lock_until") or 0))


def users_save(users):
    """Simpan ulang SEMUA akun (dict lengkap dari users_load yang sudah diubah)."""
    with engine().begin() as cx:
        cx.execute(delete(app_user))
        if users:
            cx.execute(insert(app_user), [_user_values(k, v) for k, v in users.items()])


def user_upsert(name, v):
    """Simpan satu akun tanpa menyentuh akun lain (aman untuk sign-up paralel)."""
    with engine().begin() as cx:
        cx.execute(delete(app_user).where(app_user.c.name == name))
        cx.execute(insert(app_user).values(**_user_values(name, v)))


def user_login_fail(name, lock_after, lock_seconds):
    """Catat login password gagal. Setelah `lock_after` gagal beruntun akun dikunci `lock_seconds` (hitungan di-reset).
    → epoch lock_until yang baru (0 = belum dikunci)."""
    now = int(dt.datetime.now().timestamp())
    with engine().begin() as cx:
        n = int(cx.execute(select(app_user.c.fail_n).where(app_user.c.name == name)).scalar() or 0) + 1
        vals = dict(fail_n=n, lock_until=0)
        if n >= int(lock_after):
            vals = dict(fail_n=0, lock_until=now + int(lock_seconds))
        cx.execute(update(app_user).where(app_user.c.name == name).values(**vals))
    return vals["lock_until"]


def user_login_ok(name):
    with engine().begin() as cx:
        cx.execute(update(app_user).where(app_user.c.name == name).values(fail_n=0, lock_until=0))


# ---------------------------------------------------------------- workspace & undangan

def ws_create(name, owner):
    with engine().begin() as cx:
        return cx.execute(insert(workspace).values(name=name, owner=owner, created_at=dt.datetime.now())).inserted_primary_key[0]


def ws_get(ws_id):
    with engine().connect() as cx:
        r = cx.execute(select(workspace).where(workspace.c.id == int(ws_id))).mappings().first()
    return dict(r) if r else None


def ws_rename(ws_id, name):
    with engine().begin() as cx:
        cx.execute(update(workspace).where(workspace.c.id == int(ws_id)).values(name=name))


def ws_set_status(ws_id, status):
    if status not in ("active", "suspended"):
        raise ValueError("status harus active atau suspended")
    with engine().begin() as cx:
        cx.execute(update(workspace).where(workspace.c.id == int(ws_id)).values(status=status))


def customer_save(ws_id, name="", email="", phone="", business="", city=""):
    with engine().begin() as cx:
        cx.execute(delete(customer).where(customer.c.workspace_id == int(ws_id)))
        cx.execute(insert(customer).values(workspace_id=int(ws_id), name=name, email=email, phone=phone, business=business,
                                           city=city, created_at=dt.datetime.now()))


def ws_list_all():
    """Semua workspace + data diri pendaftar + jumlah anggota/toko/file (untuk tab Workspace superadmin)."""
    with engine().connect() as cx:
        rows = cx.execute(select(workspace).order_by(workspace.c.id)).mappings().all()
        cust = {r["workspace_id"]: dict(r) for r in cx.execute(select(customer)).mappings().all()}
        members = dict(cx.execute(select(app_user.c.workspace_id, func.count()).group_by(app_user.c.workspace_id)).all())
        tokos = dict(cx.execute(select(toko.c.workspace_id, func.count()).group_by(toko.c.workspace_id)).all())
        files = dict(cx.execute(select(toko.c.workspace_id, func.count(upload.c.id))
                                .select_from(toko.join(upload, upload.c.toko_id == toko.c.id)).group_by(toko.c.workspace_id)).all())
        last = dict(cx.execute(select(toko.c.workspace_id, func.max(upload.c.uploaded_at))
                               .select_from(toko.join(upload, upload.c.toko_id == toko.c.id)).group_by(toko.c.workspace_id)).all())
    out = []
    for r in rows:
        c = cust.get(r["id"], {})
        out.append(dict(id=r["id"], name=r["name"], owner=r["owner"], status=r["status"], created=r["created_at"].isoformat(),
                        customer_name=c.get("name", ""), email=c.get("email", "") or r["owner"], phone=c.get("phone", ""),
                        business=c.get("business", ""), city=c.get("city", ""),
                        members=int(members.get(r["id"], 0)), toko=int(tokos.get(r["id"], 0)), files=int(files.get(r["id"], 0)),
                        last_upload=last[r["id"]].isoformat() if last.get(r["id"]) else ""))
    return out


def ws_delete(ws_id):
    """Hapus workspace beserta semua toko, data, anggota, undangan, dan arsip filenya."""
    import shutil
    ws_id = int(ws_id)
    if ws_id == DEFAULT_WS:
        raise ValueError("workspace utama tidak bisa dihapus")
    prev = current_ws()
    set_ws(ws_id)
    try:
        for name in list_toko():
            delete_toko(name)
            shutil.rmtree(archive_dir(name), ignore_errors=True)
        shutil.rmtree(os.path.join(DATA, "_ws", str(ws_id)), ignore_errors=True)
    finally:
        set_ws(prev)
    with engine().begin() as cx:
        cx.execute(delete(app_user).where(app_user.c.workspace_id == ws_id))
        cx.execute(delete(ws_invite).where(ws_invite.c.workspace_id == ws_id))
        cx.execute(delete(customer).where(customer.c.workspace_id == ws_id))
        cx.execute(delete(workspace).where(workspace.c.id == ws_id))


def ws_toko_names(ws_id):
    with engine().connect() as cx:
        return [r[0] for r in cx.execute(select(toko.c.name).where(toko.c.workspace_id == int(ws_id)).order_by(toko.c.name))]


def ws_move_toko(src_ws, dst_ws):
    """Pindahkan SEMUA toko (baris data via toko_id ikut otomatis) + folder arsipnya dari workspace src ke dst.
    Nama yang bentrok di tujuan diberi akhiran " - <nama workspace asal>". → list (nama lama, nama baru)."""
    import shutil
    src_ws, dst_ws = int(src_ws), int(dst_ws)
    if src_ws == dst_ws:
        return []
    try:                                   # akhiran harus lolos safe_name (tanpa tanda kurung dll) agar tetap bisa dicari by name
        src_name = safe_name((ws_get(src_ws) or {}).get("name") or "")
    except ValueError:
        src_name = f"ws{src_ws}"
    moved = []
    with engine().begin() as cx:
        taken = {r[0] for r in cx.execute(select(toko.c.name).where(toko.c.workspace_id == dst_ws))}
        rows = cx.execute(select(toko.c.id, toko.c.name).where(toko.c.workspace_id == src_ws).order_by(toko.c.name)).all()
        for tid, name in rows:
            new = name
            n = 2
            while new in taken:
                new = f"{name} - {src_name}" if n == 2 else f"{name} - {src_name} {n}"
                n += 1
            taken.add(new)
            cx.execute(update(toko).where(toko.c.id == tid).values(workspace_id=dst_ws, name=new, version=toko.c.version + 1))
            moved.append((name, new))
    prev = current_ws()
    try:
        for old, new in moved:
            set_ws(src_ws)
            a = archive_dir(old)
            set_ws(dst_ws)
            b = archive_dir(new)
            if os.path.isdir(a):
                os.makedirs(os.path.dirname(b), exist_ok=True)
                if os.path.isdir(b):          # tujuan sudah ada folder (jarang) → gabungkan isinya
                    for f in os.listdir(a):
                        shutil.move(os.path.join(a, f), os.path.join(b, f))
                    shutil.rmtree(a, ignore_errors=True)
                else:
                    shutil.move(a, b)
    finally:
        set_ws(prev)
    return moved


def ws_members(ws_id):
    with engine().connect() as cx:
        rows = cx.execute(select(app_user).where(app_user.c.workspace_id == int(ws_id)).order_by(app_user.c.created)).mappings().all()
    return {r["name"]: _user_row(r) for r in rows}


def invite_add(ws_id, email, role, invited_by):
    with engine().begin() as cx:
        cx.execute(delete(ws_invite).where(ws_invite.c.workspace_id == int(ws_id), ws_invite.c.email == email))
        cx.execute(insert(ws_invite).values(workspace_id=int(ws_id), email=email, role=role, invited_by=invited_by,
                                            created_at=dt.datetime.now()))


def invite_list(ws_id):
    with engine().connect() as cx:
        rows = cx.execute(select(ws_invite).where(ws_invite.c.workspace_id == int(ws_id)).order_by(ws_invite.c.created_at)).mappings().all()
    return [dict(email=r["email"], role=r["role"], invited_by=r["invited_by"], created=r["created_at"].isoformat()) for r in rows]


def invite_remove(ws_id, email):
    with engine().begin() as cx:
        cx.execute(delete(ws_invite).where(ws_invite.c.workspace_id == int(ws_id), ws_invite.c.email == email))


def invite_get(ws_id, email):
    with engine().connect() as cx:
        r = cx.execute(select(ws_invite).where(ws_invite.c.workspace_id == int(ws_id), ws_invite.c.email == email)).mappings().first()
    return dict(r) if r else None


def invites_for_email(email):
    """Semua undangan yang masih menunggu untuk email ini (terlama dulu)."""
    with engine().connect() as cx:
        rows = cx.execute(select(ws_invite).where(ws_invite.c.email == email).order_by(ws_invite.c.created_at)).mappings().all()
    return [dict(r) for r in rows]


def invite_pop(email):
    """Undangan terlama untuk email ini (dihapus setelah dipakai) → dict(workspace_id, role) atau None."""
    with engine().begin() as cx:
        r = cx.execute(select(ws_invite).where(ws_invite.c.email == email).order_by(ws_invite.c.created_at)).mappings().first()
        if not r:
            return None
        cx.execute(delete(ws_invite).where(ws_invite.c.id == r["id"]))
        return dict(workspace_id=r["workspace_id"], role=r["role"])


def stats():
    with engine().connect() as cx:
        out = {}
        for t in (toko, upload, order_line, income_line, adjustment_line, unsettled_line, hpp_item, hpp_map, ads_month, app_user):
            out[t.name] = int(cx.execute(select(func.count()).select_from(t)).scalar() or 0)
    return out
