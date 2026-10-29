"""Sales Akrual — analisa penjualan riil Shopee / TikTok / Tokopedia dari file export.

Jalankan:  python3 app.py   lalu buka  http://127.0.0.1:8766
"""
import os
import io
import sys
import shutil
import glob
import math
import secrets
import threading
from typing import List, Optional
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse, Response
from pydantic import BaseModel

import analysis
import audit
import auth
import db
import mailer
import hpp as hppmod
import ads as adsmod
import parsers

ROOT = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Sales Akrual", docs_url=None, redoc_url=None, openapi_url=None)
NO_STORE = {"Cache-Control": "no-store, max-age=0"}
PUBLIC_PATHS = {"/", "/login", "/api/login", "/api/login-config", "/auth/google", "/auth/google/callback", "/favicon.ico", "/healthz",
                "/privacy", "/terms", "/daftar", "/daftar/lengkapi", "/api/signup/pending", "/api/signup/complete",
                "/undangan", "/api/undangan", "/api/undangan/terima", "/api/undangan/tolak"}
OAUTH_COOKIE = "sa_oauth"


# ---------------------------------------------------------------- login (mode server)

def current_user(request: Request):
    """Mode lokal: tanpa login, dianggap admin. Mode server: dari cookie sesi."""
    if auth.is_local():
        return dict(name="lokal", role="admin", workspace_id=db.DEFAULT_WS, email="", display_name="lokal", picture="", google=False)
    tok = request.cookies.get(auth.COOKIE)
    return auth.verify_token(tok) if tok else None


# Header keamanan untuk semua respons. HSTS dipasang di Caddy (hanya bermakna di HTTPS).
# CSP: skrip hanya dari situs sendiri + cdnjs (Chart.js), style/font dari Google Fonts, gambar profil Google;
# halaman tidak boleh dibingkai situs lain (clickjacking). Inline script/style masih diizinkan karena index.html memakainya.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
                               "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
                               "img-src 'self' data: blob: https://*.googleusercontent.com; connect-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if _secure_cookie(request):   # hanya lewat HTTPS: paksa HTTPS setahun ke depan (tidak bergantung pada konfigurasi proxy)
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    db.set_ws(db.DEFAULT_WS)
    if auth.is_local() or path in PUBLIC_PATHS:
        return await call_next(request)
    user = current_user(request)
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "belum login"}, status_code=401, headers=NO_STORE)
        return RedirectResponse("/login", status_code=303)
    ws = db.ws_get(user["workspace_id"])
    if ws and ws.get("status") == "suspended":
        msg = "Workspace ini dinonaktifkan. Hubungi farras@mierakigai.com."
        if path.startswith("/api/"):
            return JSONResponse({"detail": msg}, status_code=403, headers=NO_STORE)
        resp = RedirectResponse("/login?err=" + quote(msg), status_code=303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp
    request.state.user = user
    db.set_ws(user["workspace_id"])    # semua query data di request ini terkunci ke workspace pemakai
    return await call_next(request)


def require_superadmin(request: Request):
    u = current_user(request)
    if not auth.is_superadmin(u):
        raise HTTPException(403, "hanya superadmin")
    return u


def require_admin(request: Request):
    u = current_user(request)
    if not u or u["role"] != "admin":
        raise HTTPException(403, "hanya admin")
    return u


def require_local():
    if not auth.is_local():
        raise HTTPException(403, "fitur ini hanya ada saat app dijalankan di Mac (mode lokal) — pakai upload")


def _secure_cookie(request: Request):
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@app.get("/login")
def login_page(request: Request):
    if auth.is_local() or current_user(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(ROOT, "static", "login.html"), headers=NO_STORE)


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(body: LoginIn, request: Request):
    if auth.is_local():
        return dict(ok=True)
    if not auth.load_users():
        raise HTTPException(503, "belum ada pengguna — buat admin dulu: python3 app.py adduser <nama> --admin")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    try:
        tok = auth.login(body.username, body.password, ip)
    except PermissionError as e:
        raise HTTPException(429, str(e))
    if not tok:
        raise HTTPException(401, "nama pengguna atau password salah")
    resp = JSONResponse(dict(ok=True), headers=NO_STORE)
    resp.set_cookie(auth.COOKIE, tok, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=_secure_cookie(request), path="/")
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    resp = JSONResponse(dict(ok=True), headers=NO_STORE)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/login-config")
def api_login_config():
    """Dibaca halaman login: tombol Google tampil hanya kalau OAuth sudah dikonfigurasi."""
    return JSONResponse(dict(google=auth.google_enabled(), password=True), headers=NO_STORE)


@app.get("/auth/google")
def auth_google(request: Request, mode: str = "login", t: Optional[str] = None):
    """mode=login: hanya akun yang sudah ada / diundang. mode=signup: email baru diarahkan ke form data diri.
    mode=invite&t=<token>: dari tombol Terima di halaman /undangan — setelah masuk Google, undangan langsung diterima."""
    if auth.is_local():
        return RedirectResponse("/", status_code=303)
    mode = mode if mode in ("signup", "invite") else "login"
    if mode == "invite" and not (t and auth.read_invite_token(t)):
        return RedirectResponse("/undangan?err=" + quote("tautan undangan tidak valid atau kedaluwarsa"), status_code=303)
    if mode != "invite" and current_user(request):
        return RedirectResponse("/", status_code=303)
    back = {"signup": "/daftar", "invite": f"/undangan?t={quote(t or '')}"}.get(mode, "/login")
    if not auth.google_enabled():
        return RedirectResponse(back + ("&" if "?" in back else "?") + "err=" + quote("masuk dengan Google belum diaktifkan"), status_code=303)
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(auth.google_auth_url(state), status_code=303)
    resp.set_cookie(OAUTH_COOKIE, f"{state}|{mode}|{t or ''}", max_age=600, httponly=True, samesite="lax", secure=_secure_cookie(request), path="/auth/")
    return resp


def _set_session(resp, request, tok):
    resp.set_cookie(auth.COOKIE, tok, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=_secure_cookie(request), path="/")
    return resp


@app.get("/auth/google/callback")
def auth_google_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    expect, _, rest = request.cookies.get(OAUTH_COOKIE, "").partition("|")
    mode, _, t = rest.partition("|")
    back = {"signup": "/daftar", "invite": f"/undangan?t={quote(t)}"}.get(mode, "/login")

    def fail(msg, to=None):
        to = to or back
        r = RedirectResponse(to + ("&" if "?" in to else "?") + "err=" + quote(msg), status_code=303)
        r.delete_cookie(OAUTH_COOKIE, path="/auth/")
        return r
    if error or not code:
        return fail("masuk dengan Google dibatalkan")
    if not state or not expect or not secrets.compare_digest(state, expect):
        return fail("sesi masuk kedaluwarsa — coba lagi")
    try:
        profile = auth.google_fetch_profile(code)
        if mode == "invite":
            inv = auth.read_invite_token(t)
            if not inv:
                return fail("tautan undangan kedaluwarsa — minta admin mengirim ulang", "/undangan")
            # akun baru dibuat di workspace pengundang; akun yang sudah ada → workspace pengundang ditambahkan (workspace lamanya tetap)
            tok, status = auth.invite_accept(inv["workspace_id"], inv["email"], profile)
        else:
            tok, status = auth.google_login(profile, allow_create=False)
    except (PermissionError, ValueError) as e:
        return fail(str(e))
    except Exception:
        return fail("gagal menghubungi Google — coba lagi")
    if status == "invited":
        # ada undangan untuk email ini → tunjukkan dulu, biarkan dia menerima (profil Google disimpan sementara di cookie)
        resp = RedirectResponse("/undangan", status_code=303)
        resp.delete_cookie(OAUTH_COOKIE, path="/auth/")
        resp.set_cookie(auth.SIGNUP_COOKIE, auth.make_pending(profile), max_age=auth.SIGNUP_MINUTES * 60, httponly=True,
                        samesite="lax", secure=_secure_cookie(request), path="/")
        return resp
    if status == "new":
        if mode != "signup":
            return fail("Email ini belum terdaftar. Silakan daftar dulu.", "/daftar")
        resp = RedirectResponse("/daftar/lengkapi", status_code=303)
        resp.delete_cookie(OAUTH_COOKIE, path="/auth/")
        resp.set_cookie(auth.SIGNUP_COOKIE, auth.make_pending(profile), max_age=auth.SIGNUP_MINUTES * 60, httponly=True,
                        samesite="lax", secure=_secure_cookie(request), path="/")
        return resp
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(OAUTH_COOKIE, path="/auth/")
    return _set_session(resp, request, tok)


# ---------------------------------------------------------------- pendaftaran (form data diri)

@app.get("/daftar")
def signup_page(request: Request):
    if auth.is_local() or current_user(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(os.path.join(ROOT, "static", "daftar.html"), headers=NO_STORE)


@app.get("/daftar/lengkapi")
def signup_form_page(request: Request):
    if auth.is_local() or current_user(request):
        return RedirectResponse("/", status_code=303)
    if not auth.read_pending(request.cookies.get(auth.SIGNUP_COOKIE, "")):
        return RedirectResponse("/daftar?err=" + quote("sesi pendaftaran habis — klik Daftar dengan Google lagi"), status_code=303)
    return FileResponse(os.path.join(ROOT, "static", "lengkapi.html"), headers=NO_STORE)


@app.get("/api/signup/pending")
def api_signup_pending(request: Request):
    p = auth.read_pending(request.cookies.get(auth.SIGNUP_COOKIE, ""))
    if not p:
        raise HTTPException(401, "sesi pendaftaran habis")
    return JSONResponse(dict(name=p["name"], email=p["email"], picture=p["picture"]), headers=NO_STORE)


class SignupIn(BaseModel):
    name: str
    phone: str
    business: str
    city: str
    agree: bool = False


@app.post("/api/signup/complete")
def api_signup_complete(body: SignupIn, request: Request):
    p = auth.read_pending(request.cookies.get(auth.SIGNUP_COOKIE, ""))
    if not p:
        raise HTTPException(401, "sesi pendaftaran habis — klik Daftar dengan Google lagi")
    if not body.agree:
        raise HTTPException(400, "centang persetujuan Ketentuan Layanan & Kebijakan Privasi")
    try:
        tok, status = auth.signup_complete(p, body.dict())
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse(dict(ok=True, status=status), headers=NO_STORE)
    resp.delete_cookie(auth.SIGNUP_COOKIE, path="/")
    return _set_session(resp, request, tok)


# ---------------------------------------------------------------- superadmin: semua workspace (database pelanggan)

@app.get("/api/admin/workspaces")
def api_admin_workspaces(request: Request):
    require_superadmin(request)
    return db.ws_list_all()


class WsStatusIn(BaseModel):
    id: int
    status: str


@app.post("/api/admin/workspace/status")
def api_admin_ws_status(body: WsStatusIn, request: Request):
    require_superadmin(request)
    if body.id == db.DEFAULT_WS:
        raise HTTPException(400, "workspace utama tidak bisa dinonaktifkan")
    try:
        db.ws_set_status(body.id, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(ok=True)


@app.delete("/api/admin/workspace")
def api_admin_ws_delete(id: int, request: Request):
    require_superadmin(request)
    if not db.ws_get(id):
        raise HTTPException(404, "workspace tidak ada")
    try:
        db.ws_delete(id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with _lock:
        for k in [k for k in _cache if k[0] == id]:
            _cache.pop(k, None)
    return dict(ok=True)


@app.get("/api/admin/workspaces.xlsx")
def api_admin_workspaces_xlsx(request: Request):
    require_superadmin(request)
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Pelanggan"
    cols = [("id", "ID"), ("name", "Workspace"), ("status", "Status"), ("customer_name", "Nama"), ("email", "Email"),
            ("phone", "WhatsApp"), ("business", "Bisnis / toko"), ("city", "Kota"), ("members", "Anggota"), ("toko", "Toko"),
            ("files", "File"), ("last_upload", "Upload terakhir"), ("created", "Daftar")]
    ws.append([h for _, h in cols])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in db.ws_list_all():
        ws.append([r[k] for k, _ in cols])
    for i, (k, _) in enumerate(cols, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = 14 if k in ("id", "status", "members", "toko", "files") else 26
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="Pelanggan Sales Akrual.xlsx"'})


@app.get("/api/me")
def api_me(request: Request):
    u = current_user(request)
    ws = db.ws_get(u["workspace_id"]) or dict(id=u["workspace_id"], name="")
    return JSONResponse(dict(user=u["name"], role=u["role"], local=auth.is_local(), mode=auth.mode(),
                             email=u.get("email", ""), display_name=u.get("display_name") or u["name"], picture=u.get("picture", ""),
                             google=u.get("google", False), superadmin=auth.is_superadmin(u), mail=mailer.enabled(),
                             invites=[dict(token=i["token"], workspace=i["workspace"], role=i["role"], invited_by=i["invited_by"])
                                      for i in (auth.invite_info(r["workspace_id"], u["email"]) for r in db.invites_for_email(u["email"]))
                                      if i and not (i["existing"] or {}).get("same")] if u.get("email") else [],
                             workspaces=[] if auth.is_local() else [dict(id=w["id"], name=w["name"], role=w["role"], active=w["id"] == ws["id"])
                                                                    for w in db.user_workspaces(u["name"])],
                             workspace=dict(id=ws["id"], name=ws["name"])), headers=NO_STORE)


class WsSwitchIn(BaseModel):
    id: int


@app.post("/api/workspace/switch")
def api_workspace_switch(body: WsSwitchIn, request: Request):
    """Ganti workspace aktif (harus anggota). Sesi/cookie tetap; permintaan berikutnya memakai workspace itu."""
    u = current_user(request)
    if auth.is_local():
        raise HTTPException(400, "mode lokal hanya punya satu workspace")
    ws = db.ws_get(body.id)
    if not ws or ws.get("status") == "suspended":
        raise HTTPException(400, "workspace tidak ada atau dinonaktifkan")
    try:
        role = db.user_set_active(u["name"], body.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(dict(ok=True, workspace=dict(id=ws["id"], name=ws["name"]), role=role), headers=NO_STORE)


@app.post("/api/workspace/keluar")
def api_workspace_leave(request: Request):
    """Keluar dari workspace yang sedang aktif. Harus masih punya workspace lain, dan bukan admin terakhir di sini."""
    u = current_user(request)
    if auth.is_local():
        raise HTTPException(400, "mode lokal")
    others = [w for w in db.user_workspaces(u["name"]) if w["id"] != int(u["workspace_id"])]
    if not others:
        raise HTTPException(400, "ini satu-satunya workspace Anda — tidak bisa keluar")
    try:
        auth.remove_user(u["name"], u["workspace_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    me = db.users_load().get(u["name"]) or {}
    ws = db.ws_get(me.get("workspace_id")) or {}
    return JSONResponse(dict(ok=True, workspace=dict(id=ws.get("id"), name=ws.get("name", ""))), headers=NO_STORE)


class WsIn(BaseModel):
    name: str


@app.post("/api/workspace")
def api_workspace_rename(body: WsIn, request: Request):
    u = require_admin(request)
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "nama workspace kosong")
    db.ws_rename(u["workspace_id"], name)
    return dict(ok=True, name=name)


class InviteIn(BaseModel):
    email: str
    role: Optional[str] = "user"


@app.get("/api/invites")
def api_invites(request: Request):
    u = require_admin(request)
    return db.invite_list(u["workspace_id"])


def _base_url(request: Request):
    base = os.environ.get("SA_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def _send_invite_mail(request: Request, u, email, role):
    """Buat tautan terima + kirim email. → dict(link, mail: sent|off|failed, mail_msg)."""
    link = _base_url(request) + "/undangan?t=" + quote(auth.make_invite_token(u["workspace_id"], email))
    if not mailer.enabled():
        return dict(link=link, mail="off", mail_msg="email belum dikonfigurasi — bagikan tautan ini sendiri")
    ws = db.ws_get(u["workspace_id"]) or {}
    ok, msg = mailer.send_invite(email, ws.get("name", "Sales Akrual"), u.get("display_name") or u["name"], role, link, auth.INVITE_DAYS)
    return dict(link=link, mail="sent" if ok else "failed", mail_msg=msg)


@app.post("/api/invites")
def api_invites_add(body: InviteIn, request: Request):
    u = require_admin(request)
    try:
        email = auth.norm_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    role = body.role if body.role in auth.ROLES else "user"
    if any(m.get("email") == email for m in db.ws_members(u["workspace_id"]).values()):
        raise HTTPException(400, "email ini sudah jadi anggota")
    db.invite_add(u["workspace_id"], email, role, u.get("display_name") or u["name"])
    return dict(ok=True, email=email, role=role, **_send_invite_mail(request, u, email, role))


class InviteEmailIn(BaseModel):
    email: str


@app.post("/api/invites/resend")
def api_invites_resend(body: InviteEmailIn, request: Request):
    u = require_admin(request)
    email = body.email.strip().lower()
    inv = db.invite_get(u["workspace_id"], email)
    if not inv:
        raise HTTPException(404, "undangan tidak ditemukan")
    return dict(ok=True, email=email, role=inv["role"], **_send_invite_mail(request, u, email, inv["role"]))


# ---------------------------------------------------------------- halaman terima undangan (publik)

def _invite_viewer(request: Request):
    """Siapa yang membuka halaman undangan: akun yang sedang login, atau profil Google sementara (cookie pending), atau None."""
    u = current_user(request)
    if u:
        return dict(kind="session", email=u.get("email", ""), name=u.get("display_name") or u["name"], picture=u.get("picture", ""),
                    workspace=(db.ws_get(u["workspace_id"]) or {}).get("name", ""), user=u)
    p = auth.read_pending(request.cookies.get(auth.SIGNUP_COOKIE, ""))
    if p:
        return dict(kind="pending", email=p["email"], name=p["name"], picture=p["picture"], workspace="", profile=p)
    return None


@app.get("/undangan")
def invite_page():
    return FileResponse(os.path.join(ROOT, "static", "undangan.html"), headers=NO_STORE)


@app.get("/api/undangan")
def api_invite_view(request: Request, t: Optional[str] = None):
    """Isi halaman /undangan: undangan dari token (tautan email) atau semua undangan untuk email yang sedang masuk."""
    viewer = _invite_viewer(request)
    invites, err = [], None
    if t:
        inv = auth.read_invite_token(t)
        if not inv:
            err = "tautan undangan tidak valid atau sudah kedaluwarsa (berlaku %d hari) — minta admin mengirim ulang" % auth.INVITE_DAYS
        else:
            info = auth.invite_info(inv["workspace_id"], inv["email"])
            if info:
                invites.append(info)
            else:
                err = "undangan ini sudah dipakai atau dibatalkan oleh admin"
    elif viewer and viewer["email"]:
        invites = [i for i in (auth.invite_info(r["workspace_id"], viewer["email"]) for r in db.invites_for_email(viewer["email"])) if i]
    me = None
    if viewer:
        me = dict(kind=viewer["kind"], email=viewer["email"], name=viewer["name"], picture=viewer["picture"], workspace=viewer["workspace"])
    return JSONResponse(dict(invites=invites, me=me, google=auth.google_enabled(), error=err), headers=NO_STORE)


class InviteTokenIn(BaseModel):
    t: str


@app.post("/api/undangan/terima")
def api_invite_accept(body: InviteTokenIn, request: Request):
    inv = auth.read_invite_token(body.t)
    if not inv:
        raise HTTPException(400, "tautan undangan tidak valid atau kedaluwarsa")
    viewer = _invite_viewer(request)
    if not viewer:
        raise HTTPException(401, "masuk dengan Google dulu untuk menerima undangan")
    if viewer["email"] != inv["email"]:
        raise HTTPException(403, f"undangan ini untuk {inv['email']}, Anda masuk sebagai {viewer['email'] or viewer['name']}")
    try:
        tok, status = auth.invite_accept(inv["workspace_id"], inv["email"], viewer.get("profile"))
    except (PermissionError, ValueError) as e:
        raise HTTPException(400, str(e))
    ws = db.ws_get(inv["workspace_id"]) or {}
    resp = JSONResponse(dict(ok=True, status=status, workspace=ws.get("name", "")), headers=NO_STORE)
    resp.delete_cookie(auth.SIGNUP_COOKIE, path="/")
    return _set_session(resp, request, tok)


@app.post("/api/undangan/tolak")
def api_invite_decline(body: InviteTokenIn, request: Request):
    inv = auth.read_invite_token(body.t)
    if not inv:
        raise HTTPException(400, "tautan undangan tidak valid atau kedaluwarsa")
    viewer = _invite_viewer(request)
    if not viewer or viewer["email"] != inv["email"]:
        raise HTTPException(403, "hanya pemilik email yang diundang yang bisa menolak")
    db.invite_remove(inv["workspace_id"], inv["email"])
    return JSONResponse(dict(ok=True), headers=NO_STORE)


@app.delete("/api/invites")
def api_invites_delete(email: str, request: Request):
    u = require_admin(request)
    db.invite_remove(u["workspace_id"], email.strip().lower())
    return dict(ok=True)


@app.get("/api/users")
def api_users(request: Request):
    require_admin(request)
    return auth.list_users()


class UserIn(BaseModel):
    name: str
    password: Optional[str] = None
    role: Optional[str] = "user"


@app.post("/api/users")
def api_users_add(body: UserIn, request: Request):
    require_admin(request)
    try:
        name = auth.add_user(body.name, body.password or "", body.role or "user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(ok=True, name=name)


@app.post("/api/users/password")
def api_users_password(body: UserIn, request: Request):
    """Admin ganti password siapa pun; pengguna biasa hanya password sendiri."""
    me = current_user(request)
    name = auth.norm_name(body.name)
    if me["role"] != "admin" and name != me["name"]:
        raise HTTPException(403, "hanya boleh ganti password sendiri")
    if name != me["name"] and name not in db.ws_members(me["workspace_id"]):
        raise HTTPException(404, "pengguna tidak ada")
    try:
        auth.set_password(name, body.password or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(ok=True)


@app.post("/api/users/role")
def api_users_role(body: UserIn, request: Request):
    require_admin(request)
    try:
        auth.set_role(auth.norm_name(body.name), body.role or "user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(ok=True)


@app.delete("/api/users")
def api_users_delete(name: str, request: Request):
    me = require_admin(request)
    name = auth.norm_name(name)
    if name == me["name"]:
        raise HTTPException(400, "tidak bisa mengeluarkan akun sendiri — pakai 'keluar dari workspace'")
    try:
        status = auth.remove_user(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(ok=True, status=status)


@app.get("/healthz")
def healthz():
    return dict(ok=True)

_lock = threading.Lock()
_cache = {}  # toko -> (signature, (O, I, A, files))


def clean(x):
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, float):
        return None if (math.isnan(x) or math.isinf(x)) else x
    if hasattr(x, "item"):
        try:
            return clean(x.item())
        except Exception:
            return str(x)
    if x is None or isinstance(x, (str, int, bool)):
        return x
    return str(x)


def _signature(toko):
    return db.version(toko)


def get_data(toko):
    if toko not in analysis.list_toko():
        raise HTTPException(404, f"toko '{toko}' tidak ada")
    sig = _signature(toko)
    key = (db.current_ws(), toko)      # cache per workspace: nama toko boleh sama di workspace berbeda
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        data = analysis.load_toko(toko)
        _cache[key] = (sig, data)
        return data


@app.get("/")
def index(request: Request):
    # jangan di-cache browser: tiap update kode langsung kebaca tanpa hard-refresh
    u = current_user(request)
    if u and not auth.is_local():
        ws = db.ws_get(u["workspace_id"])
        if ws and ws.get("status") == "suspended":      # "/" publik, jadi cek nonaktif diulang di sini
            resp = RedirectResponse("/login?err=" + quote("Workspace ini dinonaktifkan. Hubungi farras@mierakigai.com."), status_code=303)
            resp.delete_cookie(auth.COOKIE, path="/")
            return resp
    if (auth.is_local() or u) and "lp" not in request.query_params:   # ?lp=1 = pratinjau landing page saat sudah login
        return FileResponse(os.path.join(ROOT, "static", "index.html"), headers=NO_STORE)
    # pengunjung yang belum masuk → landing page produk
    return FileResponse(os.path.join(ROOT, "static", "landing.html"), headers=NO_STORE)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/privacy")
def privacy_page():
    return FileResponse(os.path.join(ROOT, "static", "privacy.html"))


@app.get("/terms")
def terms_page():
    return FileResponse(os.path.join(ROOT, "static", "terms.html"))


@app.get("/api/toko")
def api_toko():
    out = []
    for t in analysis.list_toko():
        out.append(dict(name=t, files=db.file_count(t)))
    return out


class TokoIn(BaseModel):
    name: str


@app.post("/api/toko")
def api_toko_create(body: TokoIn):
    try:
        name = db.create_toko(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return dict(ok=True, name=name)


@app.delete("/api/toko")
def api_toko_delete(toko: str, request: Request):
    require_admin(request)
    if not db.delete_toko(toko):
        raise HTTPException(404, "toko tidak ada")
    shutil.rmtree(analysis.toko_dir(toko), ignore_errors=True)
    _cache.pop((db.current_ws(), toko), None)
    return dict(ok=True)


@app.get("/api/files")
def api_files(toko: str):
    O, I, A, files, raw = get_data(toko)
    return clean(files)


@app.delete("/api/files")
def api_files_delete(toko: str, name: str):
    if not db.delete_file(toko, os.path.basename(name)):
        raise HTTPException(404, "file tidak ada")
    p = os.path.join(analysis.toko_dir(toko), os.path.basename(name))
    if os.path.isfile(p):
        os.remove(p)
    return dict(ok=True)


@app.post("/api/upload")
async def api_upload(request: Request, toko: str = Form(...), files: List[UploadFile] = File(...)):
    if toko not in analysis.list_toko():
        raise HTTPException(404, f"toko '{toko}' tidak ada")
    d = analysis.toko_dir(toko)
    os.makedirs(d, exist_ok=True)
    who = (current_user(request) or {}).get("name", "")
    results = []
    for f in files:
        name = os.path.basename(f.filename or "file")
        dest = os.path.join(d, name)
        with open(dest, "wb") as fh:
            shutil.copyfileobj(f.file, fh)
        try:
            r = db.ingest_file(toko, dest, uploaded_by=who)
        except Exception as e:
            os.remove(dest)
            results.append(dict(file=name, ok=False, msg=f"gagal dibaca: {e}"))
            continue
        if r is None:
            os.remove(dest)
            results.append(dict(file=name, ok=False, msg="format tidak dikenali — file dibuang"))
        else:
            results.append(dict(file=name, ok=True, kind=parsers.KINDS[r["kind"]]))
    return results


@app.get("/api/scan")
def api_scan(path: str = "~/Downloads"):
    """Daftar file export yang dikenali di sebuah folder (tanpa menyalin). Hanya mode lokal."""
    require_local()
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        raise HTTPException(400, "folder tidak ada")
    out = []
    for fp in sorted(glob.glob(os.path.join(p, "*"))):
        low = fp.lower()
        if not (low.endswith(".xlsx") or low.endswith(".csv")):
            continue
        base = os.path.basename(fp)
        quick = None
        if base.startswith("Order.all"):
            quick = "shopee_orders"
        elif base.lower().startswith("income.") or base.lower().startswith("income_") or "penghasilan" in low:
            quick = "unknown_income"
        elif base.lower().startswith("semua pesanan"):
            quick = "tiktok_orders"
        elif base.lower().startswith("onhold"):
            quick = "tiktok_unsettled"
        if quick is None:
            continue
        st = os.stat(fp)
        out.append(dict(path=fp, file=base, size=st.st_size, mtime=st.st_mtime, guess=quick))
    return out


class ImportIn(BaseModel):
    toko: str
    paths: List[str]


@app.post("/api/import")
def api_import(body: ImportIn):
    require_local()
    d = analysis.toko_dir(body.toko)
    os.makedirs(d, exist_ok=True)
    results = []
    for src in body.paths:
        src = os.path.expanduser(src)
        if not os.path.isfile(src):
            results.append(dict(file=src, ok=False, msg="tidak ada"))
            continue
        kind = parsers.detect(src)
        if kind is None:
            results.append(dict(file=os.path.basename(src), ok=False, msg="format tidak dikenali"))
            continue
        dest = os.path.join(d, os.path.basename(src))
        shutil.copy2(src, dest)
        db.ingest_file(body.toko, dest)
        results.append(dict(file=os.path.basename(src), ok=True, kind=parsers.KINDS[kind]))
    return results


@app.get("/api/summary")
def api_summary(toko: str, platform: str = "all", m_from: Optional[str] = None, m_to: Optional[str] = None,
                d_from: Optional[str] = None, d_to: Optional[str] = None):
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    U = raw.get("unsettled")
    ADS = adsmod.load(toko)
    res = analysis.monthly(O, I, platform, m_from, m_to, U, ADS, d_from, d_to)
    res["breakdown"] = analysis.breakdowns(O, I, platform, m_from, m_to, top=None)
    res["unsettled_status"] = analysis.unsettled_by_status(O, I, platform, m_from, m_to, U)
    res["adjustments"] = analysis.adjustments(A, m_from, m_to) if platform in ("all", "tiktok", "tiktok_all", "tokopedia") else []
    res["reconcile"] = analysis.reconcile(analysis._channel_filter(O, platform), analysis._channel_filter(I, platform))
    res["all_months"] = sorted(set(O["month"].dropna())) if len(O) else []
    res["files"] = files
    # per-platform mini totals untuk tab ringkasan
    res["by_platform"] = {}
    for p in ("shopee", "tiktok", "tokopedia"):
        t = analysis.monthly(O, I, p, m_from, m_to, U, ADS, d_from, d_to)["totals"]
        if t:
            res["by_platform"][p] = t
    return JSONResponse(clean(res))


@app.get("/api/produk.xlsx")
def api_produk_xlsx(toko: str, platform: str = "all", m_from: Optional[str] = None, m_to: Optional[str] = None,
                    d_from: Optional[str] = None, d_to: Optional[str] = None):
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    wb = analysis.export_produk_xlsx(toko, O, I, platform, m_from, m_to)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"Produk {toko} {platform} {m_from or ''}-{m_to or ''}.xlsx".replace("  ", " ")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- pelanggan berulang

def _ensure_buyer_cols(toko):
    """File pesanan yang diunggah sebelum parser menyimpan identitas pembeli diparse ulang dari arsip (sekali saja per file)."""
    if not db.uploads_needing_buyer(toko):
        return 0, []
    with _lock:
        return db.reingest_old_orders(toko)


@app.get("/api/repeat")
def api_repeat(toko: str, platform: str = "all", m_from: Optional[str] = None, m_to: Optional[str] = None,
               d_from: Optional[str] = None, d_to: Optional[str] = None):
    if toko not in analysis.list_toko():
        raise HTTPException(404, f"toko '{toko}' tidak ada")
    done, missing = _ensure_buyer_cols(toko)
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    res = analysis.repeat_customers(O, platform, m_from, m_to)
    res["reparsed"] = done
    res["reparse_missing"] = missing
    return JSONResponse(clean(res))


@app.get("/api/repeat.xlsx")
def api_repeat_xlsx(toko: str, platform: str = "all", m_from: Optional[str] = None, m_to: Optional[str] = None,
                    d_from: Optional[str] = None, d_to: Optional[str] = None):
    if toko not in analysis.list_toko():
        raise HTTPException(404, f"toko '{toko}' tidak ada")
    _ensure_buyer_cols(toko)
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    wb = analysis.export_repeat_xlsx(toko, O, platform, m_from, m_to)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"Pelanggan berulang {toko} {platform} {m_from or ''}-{m_to or ''}.xlsx".replace("  ", " ")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export.xlsx")
def api_export(toko: str, m_from: Optional[str] = None, m_to: Optional[str] = None, d_from: Optional[str] = None, d_to: Optional[str] = None):
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    wb = analysis.export_xlsx(toko, O, I, m_from, m_to, raw.get("unsettled"), adsmod.load(toko), d_from, d_to)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"Laporan Penjualan AKRUAL {toko} {m_from or ''}-{m_to or ''}.xlsx".replace("  ", " ")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- Iklan

@app.get("/api/ads")
def api_ads(toko: str):
    O, I, A, files, raw = get_data(toko)
    data = adsmod.load(toko)
    data["data_months"] = sorted(set(O["month"].dropna())) if len(O) else []
    return JSONResponse(clean(data))


class AdsIn(BaseModel):
    toko: str
    channels: List[str]
    attribution: dict
    months: dict
    notes: Optional[str] = ""
    prorate: Optional[bool] = False


@app.post("/api/ads")
def api_ads_save(body: AdsIn):
    out = adsmod.save(body.toko, body.dict())
    return clean(dict(ok=True, months=len(out["months"])))


# ---------------------------------------------------------------- HPP

@app.get("/api/hpp")
def api_hpp(toko: str):
    O, I, A, files, raw = get_data(toko)
    master, mapping = hppmod.load(toko)
    return JSONResponse(clean(dict(master=master, mapping=mapping, table=hppmod.mapping_table(O, toko))))


class HppImportIn(BaseModel):
    toko: str
    url: str


@app.post("/api/hpp/import")
def api_hpp_import(body: HppImportIn):
    try:
        master = hppmod.import_from(body.toko, url=body.url.strip())
    except Exception as e:
        raise HTTPException(400, str(e))
    return clean(dict(ok=True, n=len(master["items"]), updated=master["updated"]))


@app.post("/api/hpp/upload")
async def api_hpp_upload(toko: str = Form(...), file: UploadFile = File(...)):
    data = await file.read()
    try:
        master = hppmod.import_from(toko, data=data, filename=file.filename or "hpp.xlsx")
    except Exception as e:
        raise HTTPException(400, str(e))
    return clean(dict(ok=True, n=len(master["items"]), updated=master["updated"]))


class HppMapIn(BaseModel):
    toko: str
    product: str
    sku: Optional[str] = ""
    item: Optional[str] = None      # None = kembali ke otomatis, "" = sengaja tanpa HPP


@app.post("/api/hpp/map")
def api_hpp_map(body: HppMapIn):
    master, mapping = hppmod.load(body.toko)
    key = (body.product + hppmod.SEP + body.sku) if body.sku else body.product
    if body.item is None:
        mapping.pop(key, None)
    else:
        mapping[key] = body.item
    hppmod.save(body.toko, mapping=mapping)
    return dict(ok=True)


class HppItemIn(BaseModel):
    toko: str
    name: str
    hpp: Optional[float] = None
    kode: Optional[str] = ""
    remove: bool = False


@app.post("/api/hpp/item")
def api_hpp_item(body: HppItemIn):
    if body.remove:
        hppmod.remove_item(body.toko, body.name.strip())
    else:
        if body.hpp is None or not body.name.strip():
            raise HTTPException(400, "nama & HPP wajib")
        hppmod.add_item(body.toko, body.name.strip(), body.hpp, kode=(body.kode or "").strip())
    return dict(ok=True)


class HppColIn(BaseModel):
    toko: str
    col: str


@app.post("/api/hpp/col")
def api_hpp_col(body: HppColIn):
    master, mapping = hppmod.load(body.toko)
    master["col"] = body.col if body.col in ("hpp", "pembulatan") else "hpp"
    hppmod.save(body.toko, master=master)
    return dict(ok=True)


@app.get("/api/audit")
def api_audit(toko: str, m_from: Optional[str] = None, m_to: Optional[str] = None, d_from: Optional[str] = None, d_to: Optional[str] = None):
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    res = dict(coverage=audit.coverage(O, I, m_from, m_to),
               reconcile=audit.reconcile_files(raw, files),
               **audit.findings(O, I, raw, m_from, m_to))
    return JSONResponse(clean(res))


@app.get("/api/trace")
def api_trace(toko: str, order_id: str):
    O, I, A, files, raw = get_data(toko)
    return JSONResponse(clean(audit.trace(O, I, A, raw, order_id)))


@app.get("/api/audit.xlsx")
def api_audit_xlsx(toko: str, m_from: Optional[str] = None, m_to: Optional[str] = None, d_from: Optional[str] = None, d_to: Optional[str] = None):
    O, I, A, files, raw = get_data(toko)
    O, I = analysis.filter_dates(O, I, d_from, d_to)
    wb = audit.export_audit_xlsx(toko, audit.coverage(O, I, m_from, m_to), audit.reconcile_files(raw, files), audit.findings(O, I, raw, m_from, m_to))
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    name = f"Audit Data Penjualan {toko} {m_from or ''}-{m_to or ''}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- tiket (dukungan & masukan)
# Lampiran: gambar/PDF, dicek dari isi file (bukan dari ekstensi/Content-Type), maks. TIKET_MAX_FILES per pesan.
TIKET_MAX_FILES = 5
TIKET_MAX_BYTES = 8 * 1024 * 1024
TIKET_MAX_TEXT = 20000
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png", ".png"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
          (b"GIF87a", "image/gif", ".gif"), (b"GIF89a", "image/gif", ".gif"), (b"%PDF-", "application/pdf", ".pdf"))


def _sniff(head):
    for sig, mime, ext in _MAGIC:
        if head.startswith(sig):
            return mime, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


def _tiket_notify_to():
    return os.environ.get("SA_TICKET_NOTIFY") or os.environ.get("SA_MAIL_REPLY_TO") or "farras@mierakigai.com"


def _tiket_mail(to, subject, text):
    """Kirim email pemberitahuan di latar belakang; kegagalan hanya dicatat ke log."""
    if not to or not mailer.enabled():
        return
    def run():
        ok, msg = mailer.send(to, subject, text)
        if not ok:
            print(f"tiket: email ke {to} gagal: {msg}")
    threading.Thread(target=run, daemon=True).start()


def _tiket_access(request: Request, t):
    """Tiket milik workspace pemakai, atau superadmin. Yang lain: 404 (tidak membocorkan keberadaan tiket)."""
    u = current_user(request)
    if not t or not (auth.is_superadmin(u) or int(t["workspace_id"]) == int(u["workspace_id"])):
        raise HTTPException(404, "tiket tidak ada")
    return u


def _tiket_save_files(tid, mid, files):
    files = [f for f in (files or []) if f and f.filename]
    if len(files) > TIKET_MAX_FILES:
        raise HTTPException(400, f"maksimal {TIKET_MAX_FILES} lampiran per pesan")
    d = db.ticket_dir(tid)
    os.makedirs(d, exist_ok=True)
    out = []
    for f in files:
        head = f.file.read(16)
        mime, ext = _sniff(head)
        if not mime:
            raise HTTPException(400, f"{f.filename}: hanya gambar (PNG/JPG/GIF/WebP) atau PDF yang bisa dilampirkan")
        f.file.seek(0, io.SEEK_END)
        size = f.file.tell()
        f.file.seek(0)
        if size > TIKET_MAX_BYTES:
            raise HTTPException(400, f"{f.filename}: lebih dari {TIKET_MAX_BYTES // (1024 * 1024)} MB")
        name = os.path.basename(f.filename)[:120] or ("lampiran" + ext)
        stored = f"{mid}_{secrets.token_hex(4)}{ext}"
        with open(os.path.join(d, stored), "wb") as fh:
            shutil.copyfileobj(f.file, fh)
        fid = db.ticket_file_add(tid, mid, name, os.path.join("_tiket", str(tid), stored), mime, size)
        out.append(dict(id=fid, name=name, mime=mime, size=size))
    return out


def _tiket_author(u):
    return u["name"], (u.get("display_name") or u["name"])[:120]


@app.get("/api/tiket")
def api_tiket_list(request: Request):
    u = current_user(request)
    return db.ticket_list(None if auth.is_superadmin(u) else u["workspace_id"])


@app.post("/api/tiket")
async def api_tiket_create(request: Request, judul: str = Form(...), isi: str = Form(""), kategori: str = Form("lain"),
                           toko: str = Form(""), files: Optional[List[UploadFile]] = File(None)):
    u = current_user(request)
    judul = " ".join(judul.split())[:200]
    isi = isi.strip()[:TIKET_MAX_TEXT]
    if not judul:
        raise HTTPException(400, "judul tiket kosong")
    if kategori not in db.TICKET_KATEGORI:
        kategori = "lain"
    author, author_name = _tiket_author(u)
    tid, mid = db.ticket_create(u["workspace_id"], judul, kategori, toko.strip()[:120], author, author_name, isi)
    try:
        _tiket_save_files(tid, mid, files)
    except HTTPException:
        db.ticket_delete(tid)
        raise
    ws = db.ws_get(u["workspace_id"]) or {}
    _tiket_mail(_tiket_notify_to(), f"[Tiket #{tid}] {judul}",
                f"Tiket baru dari {author_name} ({author}) · workspace {ws.get('name', '')} · {kategori}"
                f"{' · toko ' + toko if toko.strip() else ''}\n\n{isi}\n\n{_base_url(request)}/?tiket={tid}")
    return dict(ok=True, id=tid)


@app.get("/api/tiket/{tid}")
def api_tiket_get(tid: int, request: Request):
    t = db.ticket_get(tid)
    _tiket_access(request, t)
    return t


@app.post("/api/tiket/{tid}/balas")
async def api_tiket_reply(tid: int, request: Request, isi: str = Form(""), files: Optional[List[UploadFile]] = File(None)):
    t = db.ticket_get(tid)
    u = _tiket_access(request, t)
    isi = isi.strip()[:TIKET_MAX_TEXT]
    has_files = any(f and f.filename for f in (files or []))
    if not isi and not has_files:
        raise HTTPException(400, "balasan kosong")
    support = auth.is_superadmin(u) and u["name"] != t["created_by"]      # superadmin membalas tiket orang lain = Tim Sales Akrual
    author, author_name = _tiket_author(u)
    mid = db.ticket_msg_add(tid, author, "Tim Sales Akrual" if support else author_name, support, isi)
    _tiket_save_files(tid, mid, files)
    if support:
        creator = db.ws_members(t["workspace_id"]).get(t["created_by"]) or {}
        to = creator.get("email") or (t["created_by"] if "@" in t["created_by"] else "")
        _tiket_mail(to, f"[Tiket #{tid}] Balasan: {t['judul']}",
                    f"Tim Sales Akrual membalas tiket Anda \"{t['judul']}\":\n\n{isi}\n\nLihat tiket: {_base_url(request)}/?tiket={tid}")
    else:
        _tiket_mail(_tiket_notify_to(), f"[Tiket #{tid}] Balasan: {t['judul']}",
                    f"{author_name} ({author}) · workspace {t['workspace']}\n\n{isi}\n\n{_base_url(request)}/?tiket={tid}")
    return dict(ok=True, id=mid)


class TiketStatusIn(BaseModel):
    status: str


@app.post("/api/tiket/{tid}/status")
def api_tiket_status(tid: int, body: TiketStatusIn, request: Request):
    t = db.ticket_get(tid)
    u = _tiket_access(request, t)
    if body.status not in db.TICKET_STATUS or (body.status == "answered" and not auth.is_superadmin(u)):
        raise HTTPException(400, "status tidak dikenal")
    db.ticket_set_status(tid, body.status)
    return dict(ok=True)


@app.delete("/api/tiket/{tid}")
def api_tiket_delete(tid: int, request: Request):
    require_superadmin(request)
    if not db.ticket_get(tid):
        raise HTTPException(404, "tiket tidak ada")
    db.ticket_delete(tid)
    return dict(ok=True)


@app.get("/api/tiket/file/{fid}")
def api_tiket_file(fid: int, request: Request):
    f = db.ticket_file_get(fid)
    _tiket_access(request, f)
    p = os.path.join(db.DATA, f["path"])
    if not os.path.isfile(p):
        raise HTTPException(404, "file lampiran sudah tidak ada")
    inline = f["mime"].startswith("image/")
    return FileResponse(p, media_type=f["mime"], headers={
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f['name'])}",
        "Cache-Control": "private, max-age=3600"})


def _cli(argv):
    """python3 app.py adduser <nama> [--admin] | passwd <nama> | users | export-arsip (tulis hpp/ads JSON ke folder arsip)
    | mail-dns (record DNS pengirim email: DKIM/SPF/DMARC) | mail-test <email> (kirim email percobaan)
    | tiket (daftar tiket) | tiket <id> (isi tiket + path lampiran) | tiket-balas <id> <teks> (balas sebagai Tim Sales Akrual)
    | tiket-status <id> open|answered|closed | reingest [toko] (parse ulang file pesanan lama agar kolom pembeli terisi, semua workspace)"""
    import getpass
    cmd = argv[0]
    if cmd == "reingest":
        only = argv[1] if len(argv) > 1 else None
        for ws in db.ws_list_all():
            db.set_ws(ws["id"])
            for t in db.list_toko():
                if only and t != only:
                    continue
                need = db.uploads_needing_buyer(t)
                if not need:
                    continue
                done, missing = db.reingest_old_orders(t)
                print(f"[ws {ws['id']} {ws['name']}] {t}: {done} file diparse ulang" + (f", arsip tidak ada: {missing}" if missing else ""))
        return
    if cmd == "tiket":
        import json as _json
        args_ = [a for a in argv[1:] if a != "--json"]
        if "--json" in argv:        # dipakai deploy/tiket-bot.sh
            print(_json.dumps(db.ticket_list(None) if not args_ else (db.ticket_get(int(args_[0])) or {}), ensure_ascii=False))
            return
        if len(argv) < 2:
            for t in db.ticket_list(None):
                print(f"#{t['id']:<4} {t['status']:<9} {t['kategori']:<10} {t['workspace'][:18]:<18} {t['updated'][:16]}  {t['judul']}  "
                      f"({t['messages']} pesan, {t['files']} lampiran, terakhir: {t['last_by']})")
            return
        t = db.ticket_get(int(argv[1]))
        if not t:
            sys.exit("tiket tidak ada")
        print(f"#{t['id']} [{t['status']}] {t['judul']}\nworkspace: {t['workspace']} · kategori: {t['kategori']} · toko: {t['toko'] or '-'} · "
              f"dibuat {t['created'][:16]} oleh {t['created_by']}")
        for m in t["messages"]:
            print(f"\n--- {m['created'][:16]} · {'TIM' if m['support'] else m['author_name']} ({m['author']})\n{m['isi']}")
            for f in m["files"]:
                print(f"  [lampiran #{f['id']}] {f['name']} ({f['mime']}, {f['size']} B) → "
                      f"{os.path.join(db.DATA, (db.ticket_file_get(f['id']) or {}).get('path', ''))}")
        return
    if cmd == "tiket-balas":
        if len(argv) < 3:
            sys.exit("pakai: python3 app.py tiket-balas <id> <teks>")
        t = db.ticket_get(int(argv[1]))
        if not t:
            sys.exit("tiket tidak ada")
        db.ticket_msg_add(t["id"], auth.superadmin_name() or "support", os.environ.get("SA_REPLY_NAME") or "Tim Sales Akrual", True,
                          " ".join(argv[2:]).strip())
        creator = db.ws_members(t["workspace_id"]).get(t["created_by"]) or {}
        to = creator.get("email") or (t["created_by"] if "@" in t["created_by"] else "")
        if to and mailer.enabled():
            ok, msg = mailer.send(to, f"[Tiket #{t['id']}] Balasan: {t['judul']}",
                                  f"Tim Sales Akrual membalas tiket Anda \"{t['judul']}\":\n\n{' '.join(argv[2:]).strip()}\n\n"
                                  f"Lihat tiket: {os.environ.get('SA_BASE_URL', '').rstrip('/')}/?tiket={t['id']}")
            print("email ke", to, "terkirim" if ok else f"GAGAL: {msg}")
        print(f"balasan tersimpan di tiket #{t['id']} (status: answered)")
        return
    if cmd == "tiket-status":
        if len(argv) < 3:
            sys.exit("pakai: python3 app.py tiket-status <id> open|answered|closed")
        db.ticket_set_status(int(argv[1]), argv[2])
        print("status diubah")
        return
    if cmd == "mail-dns":
        print(f"mode email: {mailer.mode()}  pengirim: {mailer.from_addr() or '-'}  selector: {mailer.selector()}")
        for r in mailer.dns_records(os.environ.get("SA_SERVER_IP", "")):
            print(f"{r['type']} {r['name']}\n    {r['content']}")
        return
    if cmd == "mail-test":
        if len(argv) < 2:
            sys.exit("pakai: python3 app.py mail-test <email>")
        ok, msg = mailer.send(argv[1], "Tes email Sales Akrual", "Kalau email ini sampai, pengiriman dari server sudah bekerja.\n\n— Sales Akrual")
        print("terkirim" if ok else f"GAGAL: {msg}")
        sys.exit(0 if ok else 1)
    if cmd == "export-arsip":
        for t in db.list_toko():
            db.export_settings(t)
            print(f"{t}: hpp.json, hpp_map.json, ads.json ditulis ke {db.archive_dir(t)}")
        return
    if cmd == "users":
        for u in auth.list_users():
            print(f"{u['name']:20s} {u['role']}")
        return
    if cmd in ("adduser", "passwd"):
        if len(argv) < 2:
            sys.exit("pakai: python3 app.py adduser <nama> [--admin]")
        name = argv[1]
        pw = os.environ.get("SA_NEW_PASS") or getpass.getpass(f"password (min {auth.PASSWORD_MIN}): ")
        if not os.environ.get("SA_NEW_PASS") and getpass.getpass("ulangi password: ") != pw:
            sys.exit("password tidak sama")
        if cmd == "adduser":
            name = auth.add_user(name, pw, "admin" if "--admin" in argv else "user")
            print(f"pengguna '{name}' dibuat ({'admin' if '--admin' in argv else 'user'}) di {auth.users_path()}")
        else:
            auth.set_password(auth.norm_name(name), pw)
            print("password diganti")
        return
    sys.exit(f"perintah tidak dikenal: {cmd}\n{_cli.__doc__}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli(sys.argv[1:])
        sys.exit(0)
    host = os.environ.get("SA_HOST") or ("0.0.0.0" if not auth.is_local() else "127.0.0.1")
    port = int(os.environ.get("SA_PORT") or 8766)
    made = auth.bootstrap_from_env()
    if made:
        print(f"admin pertama dibuat dari env: {made}")
    print(f"Sales Akrual [{auth.mode()}] db={db.db_url().split('@')[-1]} arsip={analysis.DATA} → http://{host}:{port}")
    if mailer.mode() == "direct":
        # siapkan kunci DKIM sejak awal (bukan saat email pertama) dan tampilkan record TXT-nya di log
        print(f"email: kirim langsung sebagai {mailer.from_addr()} — TXT {mailer.selector()}._domainkey.{mailer.from_domain()} = {mailer.dkim_txt() or 'GAGAL membuat kunci'}")
    else:
        print(f"email: mode {mailer.mode()}")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*", server_header=False)
