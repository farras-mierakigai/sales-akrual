"""Kirim email transaksional (undangan anggota, dst).

Dua mode, dipilih otomatis:
1. SMTP relay  — SA_SMTP_USER + SA_SMTP_PASS terisi (mis. Gmail App Password). Host/port: SA_SMTP_HOST (smtp.gmail.com),
   SA_SMTP_PORT (587 STARTTLS / 465 SSL).
2. Kirim langsung — tanpa akun pihak ketiga: server ini sendiri yang mengantar ke MX penerima (port 25), pesan ditandatangani
   DKIM. Butuh: SA_MAIL_FROM (mis. noreply@mierakigai.com), kunci DKIM di <DATA>/.dkim/<selector>.key (dibuat otomatis kalau
   belum ada), dan DNS domain pengirim: TXT <selector>._domainkey = dkim_txt(), SPF memuat ip4 server, DMARC. SA_MAIL_HELO =
   nama host yang cocok dengan PTR IP server (FCrDNS), SA_DKIM_SELECTOR (default "akrual").
Tidak ada yang terisi = email mati; undangan tetap bisa dibagikan lewat tautan.
"""
import os
import html
import smtplib
import logging
import email.utils
from email.message import EmailMessage

log = logging.getLogger("sa.mail")
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.expanduser(os.environ.get("SA_DATA_DIR") or os.path.join(ROOT, "data")))


def _env(k, d=""):
    return os.environ.get(k, d).strip() or d


def smtp_enabled():
    return bool(_env("SA_SMTP_USER") and _env("SA_SMTP_PASS"))


def from_addr():
    return _env("SA_MAIL_FROM") or _env("SA_SMTP_USER")


def from_domain():
    return from_addr().rsplit("@", 1)[-1].lower() if "@" in from_addr() else ""


def selector():
    return _env("SA_DKIM_SELECTOR", "akrual")


def dkim_key_path():
    return os.path.join(DATA, ".dkim", f"{selector()}.key")


def direct_enabled():
    return bool(from_domain()) and not smtp_enabled()


def mode():
    return "smtp" if smtp_enabled() else ("direct" if direct_enabled() else "off")


def enabled():
    return mode() != "off"


# ---------------------------------------------------------------- DKIM

def dkim_key():
    """PEM kunci privat DKIM; dibuat sekali (RSA 2048) kalau belum ada. → bytes atau None kalau gagal."""
    p = dkim_key_path()
    if os.path.isfile(p):
        return open(p, "rb").read()
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(pem)
        os.chmod(p, 0o600)
        log.warning("kunci DKIM baru dibuat di %s — publikasikan TXT %s._domainkey.%s = %s", p, selector(), from_domain(), dkim_txt())
        return pem
    except Exception as e:  # noqa: BLE001
        log.warning("gagal membuat kunci DKIM: %s", e)
        return None


def dkim_txt():
    """Isi record TXT <selector>._domainkey.<domain> untuk kunci yang dipakai."""
    import base64
    from cryptography.hazmat.primitives import serialization
    pem = dkim_key()
    if not pem:
        return ""
    k = serialization.load_pem_private_key(pem, password=None)
    der = k.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "v=DKIM1; k=rsa; p=" + base64.b64encode(der).decode()


def dns_records(server_ip=""):
    """Record DNS yang harus ada di domain pengirim (buat ditampilkan ke superadmin / dicocokkan)."""
    d = from_domain()
    return [dict(type="TXT", name=f"{selector()}._domainkey.{d}", content=dkim_txt()),
            dict(type="TXT", name=d, content=f"v=spf1 ... ip4:{server_ip or '<IP server>'} ~all  (tambahkan ip4 server ke SPF yang sudah ada)"),
            dict(type="TXT", name=f"_dmarc.{d}", content="v=DMARC1; p=quarantine (atau none)")]


# ---------------------------------------------------------------- pengiriman

def _build(to, subject, text, html_body):
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr(("Sales Akrual", from_addr()))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=from_domain() or None)
    if _env("SA_MAIL_REPLY_TO"):
        msg["Reply-To"] = _env("SA_MAIL_REPLY_TO")
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def _send_smtp(msg, to):
    host = _env("SA_SMTP_HOST", "smtp.gmail.com")
    port = int(_env("SA_SMTP_PORT", "587"))
    user, pw = _env("SA_SMTP_USER"), _env("SA_SMTP_PASS").replace(" ", "")   # App Password Google ditampilkan dengan spasi
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
        return True, "terkirim"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP menolak login — pakai App Password Google (bukan password akun) dan pastikan 2FA aktif"
    except Exception as e:  # noqa: BLE001
        return False, f"gagal kirim: {e.__class__.__name__}: {e}"


def _mx_hosts(domain):
    try:
        import dns.resolver
        rr = dns.resolver.resolve(domain, "MX", lifetime=10)
        hosts = sorted((r.preference, str(r.exchange).rstrip(".")) for r in rr)
        return [h for _, h in hosts if h] or [domain]
    except Exception:  # noqa: BLE001
        return [domain]


def _send_direct(msg, to):
    """Antar langsung ke MX penerima dengan tanda tangan DKIM (tanpa relay)."""
    pem = dkim_key()
    if not pem:
        return False, "kunci DKIM tidak tersedia"
    import dkim
    raw = msg.as_bytes()
    try:
        sig = dkim.sign(raw, selector().encode(), from_domain().encode(), pem,
                        include_headers=[b"from", b"to", b"subject", b"date", b"message-id"])
    except Exception as e:  # noqa: BLE001
        return False, f"gagal tanda tangan DKIM: {e}"
    raw = sig + raw
    helo = _env("SA_MAIL_HELO") or (_env("SA_BASE_URL").split("//")[-1].split("/")[0] or None)
    last = "MX penerima tidak bisa dihubungi"
    for host in _mx_hosts(to.rsplit("@", 1)[-1])[:3]:
        try:
            with smtplib.SMTP(host, 25, local_hostname=helo, timeout=30) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls()
                    s.ehlo()
                s.sendmail(from_addr(), [to], raw)
            return True, "terkirim"
        except smtplib.SMTPRecipientsRefused as e:
            return False, f"alamat ditolak penerima: {list(e.recipients.values())[0][1].decode(errors='ignore')[:160]}"
        except smtplib.SMTPResponseException as e:
            last = f"{e.smtp_code} {e.smtp_error.decode(errors='ignore')[:160]}"
            if 500 <= e.smtp_code < 600:
                return False, last
        except (OSError, smtplib.SMTPException) as e:
            last = f"{e.__class__.__name__}: {e}"
    log.warning("gagal kirim langsung ke %s: %s", to, last)
    return False, last


def send(to, subject, text, html_body=None):
    """Kirim satu email. → (True, 'terkirim') atau (False, alasan). Tidak melempar exception."""
    m = mode()
    if m == "off":
        return False, "email belum dikonfigurasi (SA_MAIL_FROM untuk kirim langsung, atau SA_SMTP_USER/PASS)"
    msg = _build(to, subject, text, html_body)
    return _send_smtp(msg, to) if m == "smtp" else _send_direct(msg, to)


def send_invite(to, ws_name, inviter, role, link, days):
    """Email undangan workspace dengan tautan terima."""
    peran = "admin (bisa kelola anggota & hapus toko)" if role == "admin" else "anggota"
    subject = f"Undangan bergabung ke workspace {ws_name} — Sales Akrual"
    text = (f"Halo,\n\n{inviter} mengundang Anda bergabung ke workspace \"{ws_name}\" di Sales Akrual sebagai {peran}.\n\n"
            f"Terima undangan di sini (berlaku {days} hari):\n{link}\n\n"
            f"Masuklah dengan akun Google {to}. Kalau Anda tidak mengenal pengundang ini, abaikan email ini.\n\n— Sales Akrual · Mierakigai")
    h = html.escape
    html_body = f"""<!doctype html><html><body style="margin:0;background:#F6F3FF;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1E1E1E">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="520" style="max-width:520px;background:#fff;border:1px solid #1E1E1E;border-radius:10px" cellspacing="0" cellpadding="0"><tr><td style="padding:28px 28px 24px">
<div><span style="display:inline-block;width:34px;height:34px;line-height:34px;text-align:center;border-radius:8px;background:#340E7C;color:#fff;font-weight:800;font-size:12px">MKG</span>
<span style="font-weight:800;font-size:17px;margin-left:10px;vertical-align:top;line-height:34px">Sales Akrual</span></div>
<h1 style="font-size:20px;margin:22px 0 8px">Undangan ke workspace {h(ws_name)}</h1>
<p style="font-size:14px;line-height:1.55;color:#5B5B6B;margin:0 0 18px"><b style="color:#1E1E1E">{h(inviter)}</b> mengundang Anda bergabung ke workspace
<b style="color:#1E1E1E">{h(ws_name)}</b> sebagai <b style="color:#1E1E1E">{h(peran)}</b>.</p>
<a href="{h(link)}" style="display:inline-block;background:#F4561E;color:#fff;text-decoration:none;font-weight:700;padding:12px 20px;border-radius:6px;font-size:14px">Terima undangan</a>
<p style="font-size:12px;line-height:1.5;color:#8A8A99;margin:18px 0 0">Tautan berlaku {days} hari. Masuklah dengan akun Google <b>{h(to)}</b>.
Kalau tombol tidak bisa diklik, salin tautan ini:<br><span style="word-break:break-all;color:#5E1AF1">{h(link)}</span></p>
<p style="font-size:12px;color:#8A8A99;margin:14px 0 0">Tidak mengenal pengundang ini? Abaikan email ini.</p>
</td></tr></table>
<p style="font-size:11px;color:#8A8A99;margin:14px 0 0">Sales Akrual · Mierakigai</p>
</td></tr></table></body></html>"""
    return send(to, subject, text, html_body)
