"""Kirim email lewat SMTP (default Gmail / Google Workspace).

Env: SA_SMTP_USER (alamat Gmail pengirim), SA_SMTP_PASS (App Password 16 huruf, bukan password akun),
     SA_SMTP_HOST (default smtp.gmail.com), SA_SMTP_PORT (default 587, STARTTLS), SA_MAIL_FROM (default = SA_SMTP_USER).
Kosongkan SA_SMTP_USER/PASS = fitur email mati; undangan tetap bisa dibagikan lewat tautan.
"""
import os
import html
import smtplib
import logging
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("sa.mail")


def enabled():
    return bool(os.environ.get("SA_SMTP_USER", "").strip() and os.environ.get("SA_SMTP_PASS", "").strip())


def _from():
    user = os.environ.get("SA_SMTP_USER", "").strip()
    addr = os.environ.get("SA_MAIL_FROM", "").strip() or user
    return formataddr(("Sales Akrual", addr))


def send(to, subject, text, html_body=None):
    """Kirim satu email. → (True, 'terkirim') atau (False, alasan). Tidak melempar exception."""
    if not enabled():
        return False, "email belum dikonfigurasi (SA_SMTP_USER / SA_SMTP_PASS)"
    host = os.environ.get("SA_SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    port = int(os.environ.get("SA_SMTP_PORT", "587") or 587)
    user = os.environ["SA_SMTP_USER"].strip()
    pw = os.environ["SA_SMTP_PASS"].strip().replace(" ", "")   # App Password Google ditampilkan dengan spasi
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = _from(), to, subject
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
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
        log.warning("gagal kirim email ke %s: %s", to, e)
        return False, f"gagal kirim: {e.__class__.__name__}: {e}"


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
<div style="display:flex;align-items:center;gap:10px"><span style="display:inline-block;width:34px;height:34px;line-height:34px;text-align:center;border-radius:8px;background:#340E7C;color:#fff;font-weight:800;font-size:12px">MKG</span>
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
