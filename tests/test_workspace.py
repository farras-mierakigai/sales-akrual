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
    c.cookies.set(auth.COOKIE, tok)
    return status


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
    assert [i["email"] for i in c1.get("/api/invites").json()] == ["rina@gmail.com"]
    c3 = client()
    assert login_google(c3, "sub-rina", "rina@gmail.com", "Rina") == "joined"
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


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print("ok", fn.__name__)
    print("semua tes lulus")
