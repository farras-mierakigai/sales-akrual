"""Supaya dashboard instan: snapshot data toko di disk + cache hasil hitung per (toko, versi data, parameter).

- Snapshot data: hasil analysis.load_toko disimpan sebagai pickle per workspace+toko+versi. Setelah restart / data
  dibuang dari memori, tidak perlu membaca ulang jutaan baris dari Postgres (Senotex: ±85 dtk → ±10 dtk).
- Hasil: JSON tiap endpoint (summary, repeat, geo, …) disimpan gzip di disk + memori. Kunci = versi data toko +
  versi kode + parameter. Versi toko naik setiap upload / hapus file / HPP / iklan berubah (db.bump), versi kode
  berubah tiap deploy yang mengubah rumus → cache lama otomatis tidak terpakai lalu dihapus.
- Pemanasan: thread latar menghitung tampilan bawaan (semua data, gabungan) tiap toko yang versinya berubah, jadi
  saat dibuka hasilnya sudah ada. Permintaan yang sama persis menunggu hitungan yang sedang berjalan (tidak dobel).
"""
import collections
import glob
import gzip
import hashlib
import json
import os
import pickle
import shutil
import threading
import time
import traceback

import analysis
import db

DIR = os.path.join(analysis.DATA, ".cache")
_ROOT = os.path.dirname(os.path.abspath(__file__))
_CODE_FILES = ("analysis.py", "audit.py", "hpp.py", "ads.py", "parsers.py", "app.py", "fastcache.py")
FRAMES_MEM = int(os.environ.get("SA_FRAMES_MEM") or 1)         # berapa toko yang datanya ditahan di memori
FRAMES_IDLE = int(os.environ.get("SA_FRAMES_IDLE") or 900)     # detik tanpa dipakai → data toko dilepas dari memori
RESULTS_MEM = int(os.environ.get("SA_RESULTS_MEM_MB") or 256) * 2**20   # total byte hasil JSON di memori (sisanya dibaca dari disk)


def _code_version():
    h = hashlib.sha1()
    for f in _CODE_FILES:
        try:
            with open(os.path.join(_ROOT, f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


CODE = _code_version()


def _h(s):
    return hashlib.sha1(str(s).encode()).hexdigest()[:16]


def _atomic_write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- kunci per item (hitungan sama tidak dobel)

_glock = threading.Lock()
_locks = {}


def _lock_for(key):
    with _glock:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def sig(toko):
    """Id + versi data toko di workspace aktif + versi kode. Id toko tidak pernah dipakai ulang, jadi toko yang dihapus
    lalu dibuat lagi dengan nama sama tidak mewarisi cache lama."""
    t = db.toko
    with db.engine().connect() as cx:
        r = cx.execute(db.select(t.c.id, t.c.version).where(t.c.name == db.safe_name(toko), t.c.workspace_id == db.current_ws())).first()
    if r is None:
        return f"none-{CODE}"
    return f"t{r[0]}v{int(r[1] or 0)}-{CODE}"


# ---------------------------------------------------------------- snapshot data toko

_frames = collections.OrderedDict()    # (ws, toko) -> [sig, data, last_used]


def _frames_dir(ws, toko):
    return os.path.join(DIR, "frames", f"ws{ws}", _h(toko))


def frames(toko):
    """= analysis.load_toko(toko), lewat memori → snapshot disk → Postgres."""
    ws = db.current_ws()
    s = sig(toko)
    key = (ws, toko)
    with _lock_for(("frames",) + key):
        with _glock:
            hit = _frames.get(key)
            if hit and hit[0] == s:
                hit[2] = time.time()
                _frames.move_to_end(key)
                return hit[1]
        d = _frames_dir(ws, toko)
        path = os.path.join(d, s + ".pkl")
        data = None
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    data = pickle.load(fh)
            except Exception:
                data = None
        if data is None:
            data = analysis.load_toko(toko)
            try:
                _atomic_write(path, pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
                for old in glob.glob(os.path.join(d, "*.pkl")):
                    if old != path:
                        os.remove(old)
            except OSError:
                traceback.print_exc()
        with _glock:
            _frames[key] = [s, data, time.time()]
            _frames.move_to_end(key)
            while len(_frames) > max(FRAMES_MEM, 1):
                _frames.popitem(last=False)
        return data


def release_idle():
    """Lepas data toko yang lama tidak dipakai (hasil tetap ada di cache JSON)."""
    now = time.time()
    with _glock:
        for k in [k for k, v in _frames.items() if now - v[2] > FRAMES_IDLE]:
            _frames.pop(k, None)


def forget_ws(ws):
    """Workspace dihapus: buang semua cache-nya."""
    with _glock:
        for k in [k for k in _frames if k[0] == ws]:
            _frames.pop(k, None)
        for k in [k for k in _results if k[0] == ws]:
            _results_put(k, b"")
            _results.pop(k, None)
    shutil.rmtree(os.path.join(DIR, "frames", f"ws{ws}"), ignore_errors=True)
    shutil.rmtree(os.path.join(DIR, "results", f"ws{ws}"), ignore_errors=True)


def forget(ws, toko):
    """Toko dihapus / dipindah: buang semua cache-nya."""
    with _glock:
        _frames.pop((ws, toko), None)
        for k in [k for k in _results if k[0] == ws and k[1] == toko]:
            _results_put(k, b"")
            _results.pop(k, None)
    shutil.rmtree(_frames_dir(ws, toko), ignore_errors=True)
    shutil.rmtree(_results_dir(ws, toko), ignore_errors=True)


# ---------------------------------------------------------------- hasil JSON

_results = collections.OrderedDict()   # (ws, toko, sig, kind, params) -> bytes JSON
_results_bytes = 0


def _results_put(key, val):
    global _results_bytes
    old = _results.pop(key, None)
    if old is not None:
        _results_bytes -= len(old)
    _results[key] = val
    _results_bytes += len(val)
    while _results_bytes > RESULTS_MEM and len(_results) > 1:
        _, v = _results.popitem(last=False)
        _results_bytes -= len(v)


def _results_dir(ws, toko):
    return os.path.join(DIR, "results", f"ws{ws}", _h(toko))


def result(kind, toko, params, compute):
    """JSON (bytes) hasil compute() untuk (toko, versi, kind, params) — dari memori/disk bila ada.
    compute() mengembalikan objek yang sudah aman untuk JSON (lewat clean())."""
    ws = db.current_ws()
    s = sig(toko)
    p = json.dumps({k: v for k, v in sorted(params.items()) if v not in (None, "")}, sort_keys=True)
    key = (ws, toko, s, kind, p)
    with _glock:
        hit = _results.get(key)
        if hit is not None:
            _results.move_to_end(key)
            return hit
    d = os.path.join(_results_dir(ws, toko), s)
    path = os.path.join(d, f"{kind}-{_h(p)}.json.gz")
    with _lock_for(key):
        with _glock:
            hit = _results.get(key)
        if hit is None and os.path.isfile(path):
            try:
                with gzip.open(path, "rb") as fh:
                    hit = fh.read()
            except Exception:
                hit = None
        if hit is None:
            hit = json.dumps(compute(), ensure_ascii=False, separators=(",", ":")).encode()
            try:
                _atomic_write(path, gzip.compress(hit, 3))
                base = _results_dir(ws, toko)
                for old in glob.glob(os.path.join(base, "*")):
                    if os.path.basename(old) != s:
                        shutil.rmtree(old, ignore_errors=True)
            except OSError:
                traceback.print_exc()
        with _glock:
            _results_put(key, hit)
    with _glock:
        _locks.pop(key, None)
    return hit


# ---------------------------------------------------------------- pemanasan latar

_warmed = {}          # (ws, toko) -> sig yang sudah dipanaskan


def _all_toko():
    t = db.toko
    with db.engine().connect() as cx:
        rows = cx.execute(db.select(t.c.workspace_id, t.c.name, t.c.version, t.c.id)).all()
        counts = dict(cx.execute(db.select(db.upload.c.toko_id, db.func.count()).group_by(db.upload.c.toko_id)).all())
    return [(int(ws), name, f"t{tid}v{int(ver or 0)}-{CODE}") for ws, name, ver, tid in rows if counts.get(tid)]


def start_warmer(warm_fns, interval=20, delay=5):
    """warm_fns: daftar fungsi(toko) yang memanggil result(...) dengan parameter bawaan. Jalan di thread latar."""
    def loop():
        time.sleep(delay)
        while True:
            try:
                for ws, toko, s in _all_toko():
                    db.set_ws(ws)
                    if _warmed.get((ws, toko)) == s:
                        continue
                    t0 = time.time()
                    for fn in warm_fns:
                        try:
                            fn(toko)
                        except Exception:
                            traceback.print_exc()
                    _warmed[(ws, toko)] = s
                    print(f"cache: {toko} (ws {ws}) siap dalam {time.time() - t0:.0f} dtk", flush=True)
                release_idle()
            except Exception:
                traceback.print_exc()
            time.sleep(interval)

    th = threading.Thread(target=loop, name="cache-warmer", daemon=True)
    th.start()
    return th
