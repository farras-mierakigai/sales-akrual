"""Login multi-user untuk mode server.

Akun disimpan di tabel app_user (password di-hash PBKDF2-SHA256, tidak pernah disimpan mentah).
Sesi = cookie bertanda tangan HMAC (rahasia di <DATA_DIR>/.secret atau env SA_SECRET), tanpa database.
Mode lokal (SA_MODE=local, default di Mac) tidak butuh login: pemakai dianggap admin.
"""
import os
import json
import hmac
import time
import base64
import hashlib
import secrets
import threading

import analysis
import db

ROLES = ("admin", "user")
COOKIE = "sa_session"
SESSION_DAYS = 30
_lock = threading.Lock()
_fail = {}  # ip -> [timestamps gagal login]


def mode():
    return os.environ.get("SA_MODE", "local").strip().lower()


def is_local():
    return mode() != "server"


def users_path():
    return "tabel app_user di " + db.db_url().split("@")[-1]


def _secret():
    s = os.environ.get("SA_SECRET", "").strip()
    if s:
        return s.encode()
    p = os.path.join(analysis.DATA, ".secret")
    if not os.path.exists(p):
        os.makedirs(analysis.DATA, exist_ok=True)
        with open(p, "w") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(p, 0o600)
    with open(p) as fh:
        return fh.read().strip().encode()


# ---------------------------------------------------------------- users

def load_users():
    return db.users_load()


def save_users(users):
    db.users_save(users)


def _hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000, dklen=32)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _verify(password, stored):
    try:
        salt_b64, _ = stored.split("$", 1)
        return hmac.compare_digest(_hash(password, base64.b64decode(salt_b64)), stored)
    except Exception:
        return False


def norm_name(name):
    return "".join(c for c in (name or "").strip().lower() if c.isalnum() or c in "._-")


def add_user(name, password, role="user"):
    name = norm_name(name)
    if not name:
        raise ValueError("nama pengguna kosong / hanya huruf, angka, titik, strip")
    if len(password or "") < 8:
        raise ValueError("password minimal 8 karakter")
    if role not in ROLES:
        raise ValueError("role harus admin atau user")
    with _lock:
        users = load_users()
        users[name] = dict(hash=_hash(password), role=role, created=int(time.time()))
        save_users(users)
    return name


def set_password(name, password):
    if len(password or "") < 8:
        raise ValueError("password minimal 8 karakter")
    with _lock:
        users = load_users()
        if name not in users:
            raise ValueError("pengguna tidak ada")
        users[name]["hash"] = _hash(password)
        users[name]["pw_changed"] = int(time.time())
        save_users(users)


def set_role(name, role):
    if role not in ROLES:
        raise ValueError("role harus admin atau user")
    with _lock:
        users = load_users()
        if name not in users:
            raise ValueError("pengguna tidak ada")
        users[name]["role"] = role
        if role != "admin" and not any(u["role"] == "admin" for u in users.values()):
            raise ValueError("harus tersisa minimal satu admin")
        save_users(users)


def remove_user(name):
    with _lock:
        users = load_users()
        if name not in users:
            raise ValueError("pengguna tidak ada")
        rest = {k: v for k, v in users.items() if k != name}
        if not any(u["role"] == "admin" for u in rest.values()):
            raise ValueError("harus tersisa minimal satu admin")
        save_users(rest)


def list_users():
    return [dict(name=k, role=v.get("role", "user"), created=v.get("created")) for k, v in sorted(load_users().items())]


def bootstrap_from_env():
    """Buat admin pertama dari SA_ADMIN_USER / SA_ADMIN_PASS kalau belum ada pengguna sama sekali."""
    if is_local() or load_users():
        return None
    u, p = os.environ.get("SA_ADMIN_USER", "").strip(), os.environ.get("SA_ADMIN_PASS", "")
    if u and p:
        return add_user(u, p, "admin")
    return None


# ---------------------------------------------------------------- login & sesi

def _too_many_failures(ip):
    now = time.time()
    with _lock:
        lst = [t for t in _fail.get(ip, []) if now - t < 600]
        _fail[ip] = lst
        return len(lst) >= 10


def _record_failure(ip):
    with _lock:
        _fail.setdefault(ip, []).append(time.time())


def login(name, password, ip="?"):
    """Kembalikan token sesi kalau benar; None kalau salah; raise kalau terlalu banyak percobaan."""
    if _too_many_failures(ip):
        raise PermissionError("terlalu banyak percobaan gagal — coba lagi 10 menit lagi")
    name = norm_name(name)
    u = load_users().get(name)
    if not u or not _verify(password or "", u["hash"]):
        _record_failure(ip)
        return None
    return make_token(name)


def make_token(name):
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = f"{name}|{exp}|{load_users().get(name, {}).get('pw_changed', 0)}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def verify_token(token):
    """-> dict(name, role) atau None. Token mati kalau kedaluwarsa, pengguna dihapus, atau password diganti."""
    try:
        body_sig = base64.urlsafe_b64decode(token.encode()).decode()
        name, exp, pw_changed, sig = body_sig.rsplit("|", 3)
        body = f"{name}|{exp}|{pw_changed}"
        if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        if int(exp) < time.time():
            return None
        u = load_users().get(name)
        if not u or str(u.get("pw_changed", 0)) != pw_changed:
            return None
        return dict(name=name, role=u.get("role", "user"))
    except Exception:
        return None
