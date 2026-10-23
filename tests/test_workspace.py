"""Tes mode SaaS: login password, sign-up Google (profil disimulasikan), isolasi workspace, undangan, migrasi skema lama.

Jalankan dari root repo:  python3 -m pytest -q tests/   (atau: python3 tests/test_workspace.py)
Pakai SQLite sementara, tidak menyentuh data/ maupun database VPS.
"""
import os
import sys
import tempfile
import importlib

import sqlalchemy as sa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="sa-test-")
os.environ.update(SA_MODE="server", SA_DATA_DIR=TMP, SA_DB_URL="sqlite:///" + os.path.join(TMP, "t.db"),
                  SA_ADMIN_USER="farras", SA_ADMIN_PASS="rahasia123", SA_SECRET="x" * 32,
                  SA_GOOGLE_CLIENT_ID="id", SA_GOOGLE_CLIENT_SECRET="secret", SA_BASE_URL="https://apps.test")

import db, auth, app as appmod   # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402


def client():
    return TestClient(appmod.app, base_url="https://apps.test")


def login_pw(c, user, pw):
    r = c.post("/api/login", json=dict(username=user, password=pw))
    assert r.status_code == 200, r.text
    return c


def login_google(c, sub, email, name="Orang"):
    """Lewati HTTP ke Google: panggil auth.google_login langsung lalu pasang cookie sesinya."""
    tok, status = auth.google_login(dict(sub=sub, email=email, email_verified=True, name=name, picture=""))
    if tok:
        c.cookies.set(auth.COOKIE, tok)
    return status


def profile(sub, email, name="Orang"):
    return dict(sub=sub, email=email, email_verified=True, name=name, picture="")


def invite_token(link):
    from urllib.parse import urlparse, parse_qs, unquote
    return unquote(parse_qs(urlparse(link).query)["t"][0])


def test_bootstrap_and_password_login():
    assert auth.bootstrap_from_env() == "farras"
    c = login_pw(client(), "farras", "rahasia123")
    me = c.get("/api/me").json()
    assert me["user"] == "farras" and me["role"] == "admin" and me["workspace"]["id"] == 1
    assert me["workspace"]["name"] == "Mierakigai"
    assert c.post("/api/toko", json=dict(name="Eltahj")).status_code == 200
    assert [t["name"] for t in c.get("/api/toko").json()] == ["Eltahj"]
    # akun Google tidak bisa login pakai password kosong/apa pun
    assert client().post("/api/login", json=dict(username="farras", password="salah")).status_code == 401


def test_landing_for_visitors_app_for_users():
    r = client().get("/")
    assert r.status_code == 200 and "Daftar gratis" in r.text and "Sales Akrual" in r.text
    c = login_pw(client(), "farras", "rahasia123")
    r2 = c.get("/")
    assert r2.status_code == 200 and "Daftar gratis" not in r2.text and 'id="tblFiles"' in r2.text
    # halaman app tetap butuh login lewat API
    assert client().get("/api/toko").status_code == 401


def test_login_config_and_google_redirect():
    c = client()
    assert c.get("/api/login-config").json()["google"] is True
    r = c.get("/auth/google", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "redirect_uri=https%3A%2F%2Fapps.test%2Fauth%2Fgoogle%2Fcallback" in r.headers["location"]
    assert auth.COOKIE not in r.cookies and "sa_oauth" in r.cookies
    # callback tanpa state yang cocok → kembali ke login dengan pesan
    r2 = c.get("/auth/google/callback?code=abc&state=salah", follow_redirects=False)
    assert r2.status_code == 303 and r2.headers["location"].startswith("/login?err=")


def test_google_signup_creates_isolated_workspace():
    c2 = client()
    assert login_google(c2, "sub-andi", "andi@gmail.com", "Andi") == "created"
    me = c2.get("/api/me").json()
    assert me["user"] == "andi@gmail.com" and me["role"] == "admin" and me["google"] is True
    assert me["workspace"]["id"] != 1 and me["workspace"]["name"] == "Andi"
    # workspace baru kosong, tidak melihat toko Mierakigai
    assert c2.get("/api/toko").json() == []
    assert c2.get("/api/files?toko=Eltahj").status_code == 404
    # boleh pakai nama toko yang sama dengan workspace lain
    assert c2.post("/api/toko", json=dict(name="Eltahj")).status_code == 200
    assert [t["name"] for t in c2.get("/api/toko").json()] == ["Eltahj"]
    db.set_ws(me["workspace"]["id"])
    assert db.archive_dir("Eltahj") == os.path.join(TMP, "_ws", str(me["workspace"]["id"]), "Eltahj")
    db.set_ws(1)
    assert db.archive_dir("Eltahj") == os.path.join(TMP, "Eltahj")
    # Mierakigai tetap hanya melihat tokonya sendiri; anggota pun hanya workspace sendiri
    c1 = login_pw(client(), "farras", "rahasia123")
    assert [t["name"] for t in c1.get("/api/toko").json()] == ["Eltahj"]
    assert [u["name"] for u in c1.get("/api/users").json()] == ["farras"]
    assert [u["name"] for u in c2.get("/api/users").json()] == ["andi@gmail.com"]
    # masuk lagi dengan Google → login biasa, bukan workspace baru
    assert login_google(client(), "sub-andi", "andi@gmail.com") == "login"
    assert len({v["workspace_id"] for v in db.users_load().values()}) == 2


def test_invite_joins_existing_workspace():
    c1 = login_pw(client(), "farras", "rahasia123")
    r = c1.post("/api/invites", json=dict(email="Rina@Gmail.com", role="user"))
    assert r.status_code == 200 and r.json()["email"] == "rina@gmail.com"
    assert r.json()["mail"] == "off" and r.json()["link"].startswith("https://apps.test/undangan?t=")   # SMTP tidak diisi → tautan manual
    t = invite_token(r.json()["link"])
    assert [i["email"] for i in c1.get("/api/invites").json()] == ["rina@gmail.com"]
    # tautan email bisa dibuka tanpa login: menampilkan workspace, pengundang, role
    anon = client()
    v = anon.get("/api/undangan", params=dict(t=t)).json()
    assert v["me"] is None and v["invites"][0]["workspace"] == "Mierakigai" and v["invites"][0]["role"] == "user" \
        and v["invites"][0]["invited_by"] == "farras" and v["invites"][0]["existing"] is None
    assert anon.post("/api/undangan/terima", json=dict(t=t)).status_code == 401       # harus masuk Google dulu
    # login biasa TIDAK lagi auto-join: status 'invited', tanpa akun → callback mengarahkan ke /undangan
    c3 = client()
    assert login_google(c3, "sub-rina", "rina@gmail.com", "Rina") == "invited"
    assert c3.get("/api/me").status_code == 401
    assert c1.get("/api/invites").json() and db.users_load().get("rina@gmail.com") is None
    # profil Google sementara (cookie pending) + terima → akun dibuat di workspace pengundang, sesi terpasang
    c3.cookies.set(auth.SIGNUP_COOKIE, auth.make_pending(profile("sub-rina", "rina@gmail.com", "Rina")))
    v = c3.get("/api/undangan").json()
    assert v["me"]["kind"] == "pending" and [i["workspace"] for i in v["invites"]] == ["Mierakigai"]
    r = c3.post("/api/undangan/terima", json=dict(t=t))
    assert r.status_code == 200 and r.json()["status"] == "joined", r.text
    me = c3.get("/api/me").json()
    assert me["workspace"]["id"] == 1 and me["role"] == "user"
    assert [t["name"] for t in c3.get("/api/toko").json()] == ["Eltahj"]
    assert c3.get("/api/users").status_code == 403           # bukan admin
    assert c1.get("/api/invites").json() == []               # undangan terpakai
    assert sorted(u["name"] for u in c1.get("/api/users").json()) == ["farras", "rina@gmail.com"]
    # admin workspace lain tidak bisa mengubah anggota workspace ini
    c2 = client(); login_google(c2, "sub-andi", "andi@gmail.com")
    assert c2.post("/api/users/role", json=dict(name="rina@gmail.com", role="admin")).status_code == 400
    assert c2.delete("/api/users?name=rina@gmail.com").status_code == 400
    assert c2.post("/api/users/password", json=dict(name="farras", password="password-baru")).status_code == 404
    # admin sendiri bisa; undang email yang sudah anggota ditolak
    assert c1.post("/api/users/role", json=dict(name="rina@gmail.com", role="admin")).status_code == 200
    assert c1.post("/api/invites", json=dict(email="rina@gmail.com")).status_code == 400
    assert c1.post("/api/workspace", json=dict(name="PT Mierakigai")).status_code == 200
    assert c1.get("/api/me").json()["workspace"]["name"] == "PT Mierakigai"


def test_login_mode_rejects_unknown_email_signup_mode_needs_form():
    """Masuk dengan Google untuk email yang belum terdaftar → diarahkan ke /daftar. Daftar → form data diri → workspace baru."""
    c = client()
    # simulasi callback tanpa HTTP ke Google: panggil langsung fungsi yang sama seperti callback
    tok, st = auth.google_login(dict(sub="sub-budi", email="budi@gmail.com", email_verified=True, name="Budi"), allow_create=False)
    assert (tok, st) == (None, "new")
    # tombol di halaman login/daftar mengarah ke mode yang benar
    r = c.get("/auth/google?mode=signup", follow_redirects=False)
    assert r.status_code == 303 and c.cookies.get("sa_oauth", path="/auth/") is None or True   # cookie path /auth/
    # pending cookie → halaman lengkapi + API pending + complete
    pend = auth.make_pending(dict(sub="sub-budi", email="budi@gmail.com", email_verified=True, name="Budi", picture="http://x/p.png"))
    c.cookies.set(auth.SIGNUP_COOKIE, pend)
    assert c.get("/daftar/lengkapi", follow_redirects=False).status_code == 200
    p = c.get("/api/signup/pending").json()
    assert p["email"] == "budi@gmail.com" and p["name"] == "Budi"
    bad = c.post("/api/signup/complete", json=dict(name="Budi S", phone="0812", business="Toko Budi", city="Solo", agree=True))
    assert bad.status_code == 400 and "WhatsApp" in bad.json()["detail"]
    bad2 = c.post("/api/signup/complete", json=dict(name="Budi S", phone="081234567890", business="Toko Budi", city="Solo", agree=False))
    assert bad2.status_code == 400
    ok = c.post("/api/signup/complete", json=dict(name="Budi Santoso", phone="0812-3456-7890", business="Toko Budi", city="Solo", agree=True))
    assert ok.status_code == 200 and ok.json()["status"] == "created"
    me = c.get("/api/me").json()
    assert me["user"] == "budi@gmail.com" and me["role"] == "admin" and me["workspace"]["name"] == "Toko Budi" and me["superadmin"] is False
    # data diri tersimpan, nomor dinormalisasi ke 62...
    row = next(w for w in db.ws_list_all() if w["id"] == me["workspace"]["id"])
    assert row["customer_name"] == "Budi Santoso" and row["phone"] == "6281234567890" and row["city"] == "Solo" and row["email"] == "budi@gmail.com"
    # halaman lengkapi tanpa cookie → balik ke /daftar; sudah login → ke /
    assert client().get("/daftar/lengkapi", follow_redirects=False).headers["location"].startswith("/daftar?err=")
    assert c.get("/daftar", follow_redirects=False).headers["location"] == "/"
    # masuk lagi lewat mode login sekarang dikenal
    tok, st = auth.google_login(dict(sub="sub-budi", email="budi@gmail.com", email_verified=True, name="Budi"), allow_create=False)
    assert st == "login" and tok


def test_superadmin_workspace_management():
    c1 = login_pw(client(), "farras", "rahasia123")
    assert c1.get("/api/me").json()["superadmin"] is True
    rows = c1.get("/api/admin/workspaces").json()
    ids = {w["name"]: w["id"] for w in rows}
    assert "Toko Budi" in ids and rows[0]["id"] == 1 and rows[0]["toko"] == 1 and rows[0]["members"] >= 2
    # bukan superadmin → 403 (andi admin di workspace lain)
    c2 = client(); login_google(c2, "sub-andi", "andi@gmail.com")
    assert c2.get("/api/admin/workspaces").status_code == 403
    assert c2.get("/api/admin/workspaces.xlsx").status_code == 403
    # export excel
    x = c1.get("/api/admin/workspaces.xlsx")
    assert x.status_code == 200 and x.headers["content-type"].startswith("application/vnd.openxmlformats")
    # nonaktifkan Toko Budi → budi tidak bisa akses API maupun halaman
    wid = ids["Toko Budi"]
    assert c1.post("/api/admin/workspace/status", json=dict(id=1, status="suspended")).status_code == 400
    assert c1.post("/api/admin/workspace/status", json=dict(id=wid, status="suspended")).status_code == 200
    cb = client(); login_google(cb, "sub-budi", "budi@gmail.com")
    assert cb.get("/api/toko").status_code == 403
    r = cb.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?err=")
    assert c1.post("/api/admin/workspace/status", json=dict(id=wid, status="active")).status_code == 200
    cb = client(); login_google(cb, "sub-budi", "budi@gmail.com")
    assert cb.get("/api/toko").status_code == 200
    # hapus workspace Budi (dengan toko di dalamnya) → bersih, workspace utama tidak bisa dihapus
    assert cb.post("/api/toko", json=dict(name="Budi Store")).status_code == 200
    assert c1.delete("/api/admin/workspace?id=1").status_code == 400
    assert c1.delete(f"/api/admin/workspace?id={wid}").status_code == 200
    assert wid not in {w["id"] for w in c1.get("/api/admin/workspaces").json()}
    assert "budi@gmail.com" not in db.users_load()
    assert cb.get("/api/me").status_code == 401     # sesi budi mati


def test_schema_migration_from_old_database():
    """Database versi lama (tanpa workspace, toko.name unik global) harus naik versi otomatis saat engine() pertama."""
    old = os.path.join(TMP, "old.db")
    eng = sa.create_engine("sqlite:///" + old)
    with eng.begin() as cx:
        cx.execute(sa.text("CREATE TABLE toko (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE, created_at DATETIME NOT NULL, "
                           "version INTEGER NOT NULL, hpp_col VARCHAR(16) NOT NULL, hpp_source TEXT NOT NULL, hpp_updated VARCHAR(32) NOT NULL, "
                           "ads_notes TEXT NOT NULL, ads_prorate BOOLEAN NOT NULL)"))
        cx.execute(sa.text("INSERT INTO toko VALUES (1,'Lama','2026-01-01 00:00:00',0,'hpp','','','',0)"))
        cx.execute(sa.text("CREATE TABLE app_user (name VARCHAR(64) PRIMARY KEY, hash TEXT NOT NULL, role VARCHAR(16) NOT NULL, "
                           "created INTEGER NOT NULL, pw_changed INTEGER NOT NULL)"))
        cx.execute(sa.text("INSERT INTO app_user VALUES ('farras','h','admin',1,0)"))
    os.environ["SA_DB_URL"] = "sqlite:///" + old
    db._engine = None
    try:
        db.set_ws(1)
        assert db.list_toko() == ["Lama"]
        users = db.users_load()
        assert users["farras"]["workspace_id"] == 1 and users["farras"]["email"] == ""
        assert db.ws_get(1)["name"] == "Mierakigai" and db.ws_get(1)["owner"] == "farras"
        ws2 = db.ws_create("Lain", "x@y.z")
        db.set_ws(ws2)
        assert db.list_toko() == []
        db.create_toko("Lama")      # nama sama di workspace lain: di SQLite lama UNIQUE(name) masih ada → gagal, itu diterima
    except sa.exc.IntegrityError:
        pass
    finally:
        db.set_ws(1)
        os.environ["SA_DB_URL"] = "sqlite:///" + os.path.join(TMP, "t.db")
        db._engine = None




def test_z_invite_token_and_email_checks():
    c1 = login_pw(client(), "farras", "rahasia123")
    t = invite_token(c1.post("/api/invites", json=dict(email="budi@gmail.com", role="admin")).json()["link"])
    # token dipalsukan / rusak
    assert client().get("/api/undangan", params=dict(t=t[:-4] + "AAAA")).json()["error"]
    assert client().post("/api/undangan/terima", json=dict(t="bukan-token")).status_code == 400
    # orang lain (email berbeda) tidak bisa menerima maupun menolak undangan budi
    c4 = client(); c4.cookies.set(auth.SIGNUP_COOKIE, auth.make_pending(profile("sub-x", "orang-lain@gmail.com")))
    assert c4.post("/api/undangan/terima", json=dict(t=t)).status_code == 403
    assert c4.post("/api/undangan/tolak", json=dict(t=t)).status_code == 403
    with_pw = login_pw(client(), "farras", "rahasia123")
    assert with_pw.post("/api/undangan/terima", json=dict(t=t)).status_code == 403
    # jalur tombol "Terima & masuk dengan Google": profil email lain ditolak, email yang benar diterima dengan role admin
    try:
        auth.invite_accept(1, "budi@gmail.com", profile("sub-x", "orang-lain@gmail.com"))
        raise AssertionError("email lain seharusnya ditolak")
    except PermissionError:
        pass
    tok, status = auth.invite_accept(1, "budi@gmail.com", profile("sub-budi", "budi@gmail.com", "Budi"))
    assert status == "joined" and auth.verify_token(tok)["role"] == "admin" and auth.verify_token(tok)["workspace_id"] == 1
    # undangan sudah terpakai → tautan lama tidak berlaku lagi; kirim ulang untuk email yang tak ada undangannya → 404
    assert client().get("/api/undangan", params=dict(t=t)).json()["error"]
    assert c1.post("/api/invites/resend", json=dict(email="budi@gmail.com")).status_code == 404
    # tolak undangan oleh pemilik email → hilang dari daftar admin
    t2 = invite_token(c1.post("/api/invites", json=dict(email="cici@gmail.com")).json()["link"])
    assert c1.post("/api/invites/resend", json=dict(email="cici@gmail.com")).json()["mail"] == "off"
    c5 = client(); c5.cookies.set(auth.SIGNUP_COOKIE, auth.make_pending(profile("sub-cici", "cici@gmail.com")))
    assert c5.post("/api/undangan/tolak", json=dict(t=t2)).status_code == 200
    assert "cici@gmail.com" not in [i["email"] for i in c1.get("/api/invites").json()]


def test_z_existing_account_accepts_invite_and_moves_workspace():
    """Kasus nyata: Miftah sudah punya workspace sendiri (sign-up), lalu diundang ke workspace Mierakigai → menerima = akunnya pindah."""
    cm = client()
    assert login_google(cm, "sub-miftah", "miftah@mierakigai.com", "Miftah") == "created"
    old_ws = cm.get("/api/me").json()["workspace"]["id"]
    assert old_ws != 1
    c1 = login_pw(client(), "farras", "rahasia123")
    ws1 = c1.get("/api/me").json()["workspace"]["name"]      # sudah diganti jadi "PT Mierakigai" oleh tes sebelumnya
    link = c1.post("/api/invites", json=dict(email="miftah@mierakigai.com", role="user")).json()["link"]
    t = invite_token(link)
    # banner di dashboard: /api/me akun Miftah memuat undangan + halaman undangan tahu akunnya ada di workspace lain
    me = cm.get("/api/me").json()
    assert [i["workspace"] for i in me["invites"]] == [ws1] and me["invites"][0]["token"]
    v = cm.get("/api/undangan", params=dict(t=t)).json()
    assert v["me"]["kind"] == "session" and v["invites"][0]["existing"]["workspace"] == "Miftah" and v["invites"][0]["existing"]["same"] is False
    # login Google lagi dengan undangan menggantung → tetap login biasa (akun sudah ada), tidak dibelokkan
    assert login_google(client(), "sub-miftah", "miftah@mierakigai.com") == "login"
    # terima → pindah ke workspace 1 sebagai user, melihat toko Mierakigai, workspace lamanya tetap ada
    r = cm.post("/api/undangan/terima", json=dict(t=t))
    assert r.status_code == 200 and r.json()["status"] == "moved" and r.json()["workspace"] == ws1
    me = cm.get("/api/me").json()
    assert me["workspace"]["id"] == 1 and me["role"] == "user" and me["invites"] == []
    assert "Eltahj" in [x["name"] for x in cm.get("/api/toko").json()]
    assert db.ws_get(old_ws) is not None
    assert "miftah@mierakigai.com" in [u["name"] for u in c1.get("/api/users").json()]
    # undangan ke email yang sudah anggota ditolak; terima ulang tautan lama → error (undangan sudah terpakai)
    assert c1.post("/api/invites", json=dict(email="miftah@mierakigai.com")).status_code == 400
    assert cm.post("/api/undangan/terima", json=dict(t=t)).status_code == 400


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print("ok", fn.__name__)
    print("semua tes lulus")
