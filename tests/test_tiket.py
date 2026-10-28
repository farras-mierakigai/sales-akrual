"""Tes tiket & masukan: buat tiket dengan lampiran (paste screenshot), isolasi antar workspace, balasan superadmin, status, validasi file.

Jalankan dari root repo:  python3 tests/test_tiket.py   (atau pytest). Pakai SQLite sementara.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "db" in sys.modules:          # dijalankan pytest bersama test_workspace.py: pakai database sementara yang sudah disiapkan di sana
    import db
    TMP = db.DATA
else:
    TMP = tempfile.mkdtemp(prefix="sa-tiket-")
    os.environ.update(SA_MODE="server", SA_DATA_DIR=TMP, SA_DB_URL="sqlite:///" + os.path.join(TMP, "t.db"),
                      SA_ADMIN_USER="farras", SA_ADMIN_PASS="rahasia-tim-2026", SA_SECRET="x" * 32,
                      SA_GOOGLE_CLIENT_ID="id", SA_GOOGLE_CLIENT_SECRET="secret", SA_BASE_URL="https://apps.test")

import db, auth, app as appmod   # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

auth.bootstrap_from_env()     # admin pertama (superadmin) hanya dibuat selama belum ada pengguna lain

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # cukup header-nya; validasi hanya melihat tanda tangan file
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def client():
    return TestClient(appmod.app, base_url="https://apps.test")


def farras():
    c = client()
    assert c.post("/api/login", json=dict(username="farras", password="rahasia-tim-2026")).status_code == 200
    return c


def google(sub, email, name):
    c = client()
    tok, status = auth.google_login(dict(sub=sub, email=email, email_verified=True, name=name, picture=""))
    assert tok, status
    c.cookies.set(auth.COOKIE, tok)
    return c


def test_create_ticket_with_attachments_and_isolation():
    budi = google("sub-budi", "budi@gmail.com", "Budi")
    r = budi.post("/api/tiket", data=dict(judul="  Angka refund   Shopee beda ", isi="Lihat screenshot", kategori="bug", toko="Tokoku"),
                  files=[("files", ("Screenshot 2026-10-28.png", PNG, "image/png")), ("files", ("foto.jpg", JPG, "application/octet-stream"))])
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    lst = budi.get("/api/tiket").json()
    assert len(lst) == 1 and lst[0]["id"] == tid and lst[0]["judul"] == "Angka refund Shopee beda" and lst[0]["files"] == 2
    assert lst[0]["status"] == "open" and lst[0]["messages"] == 1 and lst[0]["last_by"] == "Budi"
    t = budi.get(f"/api/tiket/{tid}").json()
    assert t["kategori"] == "bug" and t["toko"] == "Tokoku" and t["created_by"] == "budi@gmail.com"
    m = t["messages"][0]
    assert m["isi"] == "Lihat screenshot" and m["support"] is False
    assert [(f["name"], f["mime"]) for f in m["files"]] == [("Screenshot 2026-10-28.png", "image/png"), ("foto.jpg", "image/jpeg")]
    # file tersimpan di <DATA>/_tiket/<id>/ dan bisa diunduh pemiliknya
    fid = m["files"][0]["id"]
    assert os.path.isdir(os.path.join(TMP, "_tiket", str(tid)))
    r = budi.get(f"/api/tiket/file/{fid}")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"].startswith("image/png")
    assert "inline" in r.headers["content-disposition"]
    # workspace lain tidak melihat tiket, file, maupun bisa membalas (404, bukan 403)
    cici = google("sub-cici", "cici@gmail.com", "Cici")
    assert cici.get("/api/tiket").json() == []
    assert cici.get(f"/api/tiket/{tid}").status_code == 404
    assert cici.get(f"/api/tiket/file/{fid}").status_code == 404
    assert cici.post(f"/api/tiket/{tid}/balas", data=dict(isi="halo")).status_code == 404
    # anggota workspace yang sama melihatnya
    db.set_ws(t["workspace_id"])
    auth.add_user("rina", "rina-kunci-rahasia-2026", "user")
    db.set_ws(1)
    rina = client()
    assert rina.post("/api/login", json=dict(username="rina", password="rina-kunci-rahasia-2026")).status_code == 200
    assert [x["id"] for x in rina.get("/api/tiket").json()] == [tid]


def test_superadmin_sees_all_and_replies_as_support():
    f = farras()
    budi = google("sub-budi", "budi@gmail.com", "Budi")
    tid = budi.get("/api/tiket").json()[0]["id"]
    allt = f.get("/api/tiket").json()
    assert any(t["id"] == tid and t["workspace"] == "Budi" for t in allt)
    r = f.post(f"/api/tiket/{tid}/balas", data=dict(isi="Sudah kami perbaiki, coba Hitung ulang."),
               files=[("files", ("bukti.png", PNG, "image/png"))])
    assert r.status_code == 200, r.text
    t = budi.get(f"/api/tiket/{tid}").json()
    assert t["status"] == "answered"
    assert t["messages"][-1]["support"] is True and t["messages"][-1]["author_name"] == "Tim Sales Akrual"
    assert t["messages"][-1]["files"][0]["name"] == "bukti.png"
    # balasan pengguna membuka tiket lagi; balasan tanpa teks tapi ada lampiran boleh
    assert budi.post(f"/api/tiket/{tid}/balas", data=dict(isi="")).status_code == 400
    assert budi.post(f"/api/tiket/{tid}/balas", data=dict(isi=""), files=[("files", ("lagi.png", PNG, "image/png"))]).status_code == 200
    assert budi.get(f"/api/tiket/{tid}").json()["status"] == "open"
    # status: pengguna boleh selesai / buka lagi, tidak boleh 'answered'; superadmin boleh
    assert budi.post(f"/api/tiket/{tid}/status", json=dict(status="answered")).status_code == 400
    assert budi.post(f"/api/tiket/{tid}/status", json=dict(status="closed")).status_code == 200
    assert budi.get(f"/api/tiket/{tid}").json()["status"] == "closed"
    assert f.post(f"/api/tiket/{tid}/status", json=dict(status="answered")).status_code == 200
    # tiket buatan superadmin sendiri: balasannya bukan "support"
    r = f.post("/api/tiket", data=dict(judul="Catatan internal", isi="cek", kategori="lain"))
    own = r.json()["id"]
    f.post(f"/api/tiket/{own}/balas", data=dict(isi="tambahan"))
    assert all(m["support"] is False for m in f.get(f"/api/tiket/{own}").json()["messages"])
    # hapus: hanya superadmin; folder lampiran ikut hilang
    assert budi.delete(f"/api/tiket/{tid}").status_code == 403
    assert f.delete(f"/api/tiket/{tid}").status_code == 200
    assert budi.get(f"/api/tiket/{tid}").status_code == 404
    assert not os.path.isdir(os.path.join(TMP, "_tiket", str(tid)))


def test_z_attachment_validation():
    budi = google("sub-budi", "budi@gmail.com", "Budi")
    r = budi.post("/api/tiket", data=dict(judul="file aneh"), files=[("files", ("virus.png", b"MZ\x90\x00" + b"\x00" * 32, "image/png"))])
    assert r.status_code == 400 and "hanya gambar" in r.json()["detail"]
    assert all(t["judul"] != "file aneh" for t in budi.get("/api/tiket").json())   # tiket gagal tidak tersisa
    r = budi.post("/api/tiket", data=dict(judul="kebanyakan"), files=[("files", (f"{i}.png", PNG, "image/png")) for i in range(6)])
    assert r.status_code == 400 and "maksimal" in r.json()["detail"]
    assert budi.post("/api/tiket", data=dict(judul="   ")).status_code == 400
    r = budi.post("/api/tiket", data=dict(judul="pdf"), files=[("files", ("laporan.pdf", b"%PDF-1.4\n" + b"\x00" * 16, "application/pdf"))])
    assert r.status_code == 200
    fid = budi.get(f"/api/tiket/{r.json()['id']}").json()["messages"][0]["files"][0]["id"]
    h = budi.get(f"/api/tiket/file/{fid}").headers
    assert h["content-type"].startswith("application/pdf") and "attachment" in h["content-disposition"]
    # belum login → 401
    assert client().get("/api/tiket").status_code == 401


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print("ok", fn.__name__)
    print("semua tes lulus")
