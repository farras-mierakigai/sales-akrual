"""Login multi-user untuk mode server.

Akun disimpan di tabel app_user (password di-hash PBKDF2-SHA256, tidak pernah disimpan mentah).
Sesi = cookie bertanda tangan HMAC (rahasia di <DATA_DIR>/.secret atau env SA_SECRET), tanpa database.
Mode lokal (SA_MODE=local, default di Mac) tidak butuh login: pemakai dianggap admin.

Dua jalur masuk:
- username + password (akun dibuat admin workspace)
- Google (OIDC): sign-up mandiri. Email baru → workspace baru (dia admin-nya); email yang diundang → diarahkan ke
  halaman /undangan untuk menerima dulu (akun yang sudah ada pun bisa menerima: akunnya pindah workspace). Env: SA_GOOGLE_CLIENT_ID, SA_GOOGLE_CLIENT_SECRET, SA_BASE_URL (mis. https://apps.mierakigai.com).
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
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
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
    return "".join(c for c in (name or "").strip().lower() if c.isalnum() or c in "._-@")


def norm_email(email):
    e = (email or "").strip().lower()
    if "@" not in e or " " in e or len(e) > 254:
        raise ValueError("email tidak valid")
    return e


def add_user(name, password, role="user", workspace_id=None):
    name = norm_name(name)
    if not name:
        raise ValueError("nama pengguna kosong / hanya huruf, angka, titik, strip")
    if len(password or "") < 8:
        raise ValueError("password minimal 8 karakter")
    if role not in ROLES:
        raise ValueError("role harus admin atau user")
    ws = int(workspace_id or db.current_ws())
    with _lock:
        users = load_users()
        if name in users and users[name]["workspace_id"] != ws:
            raise ValueError("nama pengguna sudah dipakai")
        users[name] = dict(hash=_hash(password), role=role, created=int(time.time()), workspace_id=ws,
                           email=name if "@" in name else "")
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


def _same_ws(users, name, ws):
    """Akun harus ada DAN berada di workspace yang sama dengan pemanggil."""
    u = users.get(name)
    if not u or u["workspace_id"] != ws:
        raise ValueError("pengguna tidak ada")
    return u


def set_role(name, role, workspace_id=None):
    if role not in ROLES:
        raise ValueError("role harus admin atau user")
    ws = int(workspace_id or db.current_ws())
    with _lock:
        users = load_users()
        _same_ws(users, name, ws)
        users[name]["role"] = role
        if role != "admin" and not any(u["role"] == "admin" and u["workspace_id"] == ws for u in users.values()):
            raise ValueError("harus tersisa minimal satu admin")
        save_users(users)


def remove_user(name, workspace_id=None):
    ws = int(workspace_id or db.current_ws())
    with _lock:
        users = load_users()
        _same_ws(users, name, ws)
        rest = {k: v for k, v in users.items() if k != name}
        if not any(u["role"] == "admin" and u["workspace_id"] == ws for u in rest.values()):
            raise ValueError("harus tersisa minimal satu admin")
        save_users(rest)


def list_users(workspace_id=None):
    """Anggota satu workspace (default: workspace aktif)."""
    ws = int(workspace_id or db.current_ws())
    return [dict(name=k, role=v.get("role", "user"), created=v.get("created"), email=v.get("email", ""),
                 display_name=v.get("display_name", ""), google=bool(v.get("google_sub")), picture=v.get("picture", ""))
            for k, v in sorted(db.ws_members(ws).items())]


# ---------------------------------------------------------------- Google sign-in / sign-up

def google_enabled():
    return bool(os.environ.get("SA_GOOGLE_CLIENT_ID", "").strip() and os.environ.get("SA_GOOGLE_CLIENT_SECRET", "").strip())


def google_redirect_uri():
    base = os.environ.get("SA_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("SA_BASE_URL belum diisi (mis. https://apps.mierakigai.com)")
    return base + "/auth/google/callback"


def google_auth_url(state):
    from urllib.parse import urlencode
    q = dict(client_id=os.environ["SA_GOOGLE_CLIENT_ID"].strip(), redirect_uri=google_redirect_uri(), response_type="code",
             scope="openid email profile", state=state, prompt="select_account", access_type="online")
    return GOOGLE_AUTH_URL + "?" + urlencode(q)


def google_fetch_profile(code):
    """Tukar authorization code → profil Google (sub, email, email_verified, name, picture)."""
    import httpx
    with httpx.Client(timeout=15) as c:
        r = c.post(GOOGLE_TOKEN_URL, data=dict(code=code, client_id=os.environ["SA_GOOGLE_CLIENT_ID"].strip(),
                                               client_secret=os.environ["SA_GOOGLE_CLIENT_SECRET"].strip(),
                                               redirect_uri=google_redirect_uri(), grant_type="authorization_code"))
        if r.status_code != 200:
            raise PermissionError("Google menolak kode masuk — coba lagi")
        access = r.json().get("access_token")
        p = c.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
        if p.status_code != 200:
            raise PermissionError("gagal membaca profil Google")
        return p.json()


def _profile_fields(profile):
    sub = str(profile.get("sub") or "")
    email = norm_email(profile.get("email"))
    if not sub or not profile.get("email_verified", False):
        raise PermissionError("email Google belum terverifikasi")
    display = (profile.get("name") or email.split("@")[0]).strip()[:120]
    picture = (profile.get("picture") or "")[:1000]
    return sub, email, display, picture


def google_login(profile, allow_create=True):
    """Masuk dari profil Google → (token, status): 'login' (sudah ada), 'joined' (diundang), 'created' (workspace baru,
    hanya kalau allow_create), atau (None, 'new') kalau email belum terdaftar dan allow_create=False → harus lewat form daftar."""
    sub, email, display, picture = _profile_fields(profile)
    with _lock:
        users = load_users()
        # 1. sudah pernah masuk (cocokkan sub, lalu email) → login
        hit = next((k for k, v in users.items() if v.get("google_sub") == sub), None) \
            or next((k for k, v in users.items() if v.get("email") == email), None)
        if hit:
            u = users[hit]
            u.update(google_sub=sub, email=email, display_name=display, picture=picture)
            db.user_upsert(hit, u)
            return make_token(hit), "login"
        # 2. diundang ke workspace yang sudah ada → harus menerima undangan dulu (halaman /undangan), bukan auto-join
        if any(db.ws_get(i["workspace_id"]) for i in db.invites_for_email(email)):
            return None, "invited"
        # 3. pendaftar baru → workspace sendiri, dia admin
        if allow_create:
            ws = db.ws_create(display or email, email)
            role, status = "admin", "created"
        else:
            return None, "new"
        db.user_upsert(email, dict(hash="", role=role, created=int(time.time()), workspace_id=ws, email=email,
                                   google_sub=sub, display_name=display, picture=picture))
        return make_token(email), status


def norm_phone(phone):
    p = "".join(c for c in (phone or "") if c.isdigit() or c == "+").strip()
    if p.startswith("+"):
        p = p[1:]
    if p.startswith("0"):
        p = "62" + p[1:]
    if not (8 <= len(p) <= 16) or not p.isdigit():
        raise ValueError("nomor WhatsApp tidak valid")
    return p


def signup_complete(profile, form):
    """Selesaikan pendaftaran: profil Google (dari cookie pending) + form data diri → workspace baru + akun admin + data pelanggan.
    form: dict(name, phone, business, city). Kalau email ternyata sudah terdaftar (mis. daftar dua kali), langsung login."""
    sub, email, display, picture = _profile_fields(profile)
    name = (form.get("name") or display).strip()[:120]
    business = (form.get("business") or "").strip()[:120]
    city = (form.get("city") or "").strip()[:80]
    if len(name) < 2:
        raise ValueError("nama lengkap wajib diisi")
    if len(business) < 2:
        raise ValueError("nama bisnis / toko wajib diisi")
    if len(city) < 2:
        raise ValueError("kota wajib diisi")
    phone = norm_phone(form.get("phone"))
    with _lock:
        users = load_users()
        hit = next((k for k, v in users.items() if v.get("google_sub") == sub or v.get("email") == email), None)
        if hit:
            return make_token(hit), "login"
        ws = db.ws_create(business, email)
        db.user_upsert(email, dict(hash="", role="admin", created=int(time.time()), workspace_id=ws, email=email,
                                   google_sub=sub, display_name=name, picture=picture))
        db.customer_save(ws, name=name, email=email, phone=phone, business=business, city=city)
        return make_token(email), "created"


# cookie pendaftaran sementara: profil Google yang sudah diverifikasi, menunggu form data diri (15 menit)
SIGNUP_COOKIE = "sa_signup"
SIGNUP_MINUTES = 15


def make_pending(profile):
    sub, email, display, picture = _profile_fields(profile)
    body = json.dumps(dict(sub=sub, email=email, name=display, picture=picture, exp=int(time.time()) + SIGNUP_MINUTES * 60),
                      separators=(",", ":"))
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def read_pending(token):
    """→ profil Google (dict dengan email_verified=True) atau None kalau tidak ada / kedaluwarsa / dipalsukan."""
    try:
        body, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 1)
        if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        p = json.loads(body)
        if int(p.get("exp", 0)) < time.time():
            return None
        return dict(sub=p["sub"], email=p["email"], name=p.get("name", ""), picture=p.get("picture", ""), email_verified=True)
    except Exception:
        return None


# ---------------------------------------------------------------- undangan workspace (tautan bertanda tangan di email)
INVITE_DAYS = 14


def make_invite_token(ws_id, email):
    body = json.dumps(dict(t="inv", ws=int(ws_id), email=email, exp=int(time.time()) + INVITE_DAYS * 86400), separators=(",", ":"))
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def read_invite_token(token):
    """→ dict(workspace_id, email) atau None kalau kedaluwarsa / dipalsukan. Keberadaan undangannya dicek terpisah (invite_info)."""
    try:
        body, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 1)
        if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        p = json.loads(body)
        if p.get("t") != "inv" or int(p.get("exp", 0)) < time.time():
            return None
        return dict(workspace_id=int(p["ws"]), email=norm_email(p["email"]))
    except Exception:
        return None


def _account_by_email(users, email):
    return next((k for k, v in users.items() if v.get("email") == email), None)


def invite_info(ws_id, email):
    """Undangan yang masih berlaku → dict untuk halaman /undangan, atau None. 'existing' = akun email ini yang sudah ada
    (di workspace mana) supaya halaman bisa memberi tahu bahwa menerima = pindah workspace."""
    inv, ws = db.invite_get(ws_id, email), db.ws_get(ws_id)
    if not inv or not ws:
        return None
    users = load_users()
    hit = _account_by_email(users, email)
    existing = None
    if hit:
        u = users[hit]
        cws = db.ws_get(u["workspace_id"]) or {}
        same = int(u["workspace_id"]) == int(ws_id)
        others = [k for k, v in users.items() if int(v.get("workspace_id", 0)) == int(u["workspace_id"]) and k != hit]
        tokos = [] if same else db.ws_toko_names(u["workspace_id"])
        # data toko boleh ikut pindah hanya kalau dia satu-satunya anggota workspace lama (tidak mengambil data tim lain)
        existing = dict(name=hit, workspace_id=int(u["workspace_id"]), workspace=cws.get("name", ""), same=same,
                        toko=tokos, others=len(others), can_bring=bool(tokos) and not others)
    return dict(token=make_invite_token(ws_id, email), workspace_id=int(ws_id), workspace=ws["name"], email=email,
                role=inv["role"], invited_by=inv["invited_by"], created=inv["created_at"].isoformat(), existing=existing)


def invite_accept(ws_id, email, profile=None, bring_data=False):
    """Terima undangan. profile = profil Google terverifikasi (dipakai kalau akun belum ada / untuk memperbarui foto & nama).
    → (token sesi, status): 'joined' (akun baru dibuat di workspace itu), 'moved' (akun lama dipindah ke workspace itu),
    'login' (sudah anggota workspace itu). bring_data=True: semua toko (+arsip) workspace lama ikut pindah — hanya
    diizinkan kalau dia satu-satunya anggota workspace lama; workspace lama yang jadi kosong lalu dihapus."""
    if profile:
        sub, pemail, display, picture = _profile_fields(profile)
        if pemail != email:
            raise PermissionError(f"undangan ini untuk {email}, tetapi Anda masuk sebagai {pemail}")
    with _lock:
        inv, ws = db.invite_get(ws_id, email), db.ws_get(ws_id)
        if not inv or not ws:
            raise PermissionError("undangan tidak ditemukan atau sudah dibatalkan oleh admin")
        users = load_users()
        hit = _account_by_email(users, email)
        if hit:
            u = users[hit]
            if profile:
                u.update(google_sub=sub, display_name=display, picture=picture)
            old_ws = int(u["workspace_id"])
            status = "login" if old_ws == int(ws_id) else "moved"
            if status == "moved" and bring_data:
                if any(int(v.get("workspace_id", 0)) == old_ws and k != hit for k, v in users.items()):
                    raise PermissionError("data toko tidak bisa dibawa: masih ada anggota lain di workspace lama")
            u.update(workspace_id=int(ws_id), role=inv["role"])
            db.user_upsert(hit, u)
            db.invite_remove(ws_id, email)
            if status == "moved" and bring_data:
                db.ws_move_toko(old_ws, ws_id)
                if old_ws != db.DEFAULT_WS:
                    db.ws_delete(old_ws)      # sudah kosong: tanpa toko, tanpa anggota
            return make_token(hit), status
        if not profile:
            raise PermissionError("masuk dengan Google dulu untuk menerima undangan")
        db.user_upsert(email, dict(hash="", role=inv["role"], created=int(time.time()), workspace_id=int(ws_id), email=email,
                                   google_sub=sub, display_name=display, picture=picture))
        db.invite_remove(ws_id, email)
        return make_token(email), "joined"


# ---------------------------------------------------------------- superadmin (pemilik platform)

def superadmin_name():
    return norm_name(os.environ.get("SA_SUPERADMIN") or os.environ.get("SA_ADMIN_USER") or "")


def is_superadmin(user):
    """Superadmin = akun yang ditunjuk env SA_SUPERADMIN (default: SA_ADMIN_USER) di workspace utama."""
    return bool(user) and user.get("name") == superadmin_name() and int(user.get("workspace_id", 0)) == db.DEFAULT_WS \
        and user.get("role") == "admin"


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
    if not u or not u.get("hash") or not _verify(password or "", u["hash"]):   # hash "" = akun Google, tanpa password
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
        return dict(name=name, role=u.get("role", "user"), workspace_id=u.get("workspace_id", db.DEFAULT_WS),
                    email=u.get("email", ""), display_name=u.get("display_name", "") or name, picture=u.get("picture", ""),
                    google=bool(u.get("google_sub")))
    except Exception:
        return None
