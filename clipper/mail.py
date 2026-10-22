"""Gmail over SMTP/IMAP with an app password: send posting packages and alerts, read "posted" / "skip" replies.

Secrets: GMAIL_ADDRESS (the channel Gmail that sends), GMAIL_APP_PASSWORD (16-character app password),
NOTIFY_EMAIL (where the emails go). None of these are ever written to the repo.
"""
import email
import imaplib
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formatdate

import json
from datetime import timedelta

from .common import DATA, log, now_utc, require_env

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
IMAP_HOST = "imap.gmail.com"
ATTACH_LIMIT = 24 * 1024 * 1024   # Gmail allows 25 MB; stay under it


def configured():
    return all(os.environ.get(k) for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL"))


# If Gmail refuses a login (account locked, password revoked, Google blocking the sign-in), the bot stops trying for
# a while instead of logging in again every few minutes, which is what gets an account flagged. The wait doubles
# with each failure (1h, 2h, 4h ... 12h) and clears on the first login that works.
BACKOFF = DATA / "mail_backoff.json"


class MailDown(RuntimeError):
    pass


def down_until():
    """When Gmail logins may be tried again (UTC ISO string), or None if they can be tried now."""
    try:
        rec = json.loads(BACKOFF.read_text())
    except (OSError, ValueError):
        return None
    return rec.get("until") if rec.get("until", "") > now_utc().isoformat() else None


def _guard(force=False):
    until = down_until()
    if until and not force:
        raise MailDown(f"Gmail logins are paused until {until[:16]} UTC after a refused login")


def _failed(e):
    try:
        n = int(json.loads(BACKOFF.read_text()).get("fails", 0)) + 1
    except (OSError, ValueError):
        n = 1
    until = now_utc() + timedelta(hours=min(12, 2 ** (n - 1)))
    BACKOFF.parent.mkdir(parents=True, exist_ok=True)
    BACKOFF.write_text(json.dumps({"fails": n, "until": until.isoformat(), "error": str(e)[:200],
                                   "since": now_utc().isoformat()}))
    print(f"::warning::Gmail refused the login ({type(e).__name__}: {str(e)[:120]}); no more Gmail logins until "
          f"{until:%H:%M} UTC", flush=True)


def _ok():
    if BACKOFF.exists():
        BACKOFF.unlink()


def _login(make, force=False):
    _guard(force)
    try:
        conn = make()
    except (smtplib.SMTPException, imaplib.IMAP4.error, OSError) as e:
        _failed(e)
        raise MailDown(str(e)) from e
    _ok()
    return conn


def _login_smtp(force=False):
    def make():
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=60)
        s.login(require_env("GMAIL_ADDRESS"), require_env("GMAIL_APP_PASSWORD").replace(" ", ""))
        return s
    return _login(make, force)


def _login_imap(force=False):
    def make():
        m = imaplib.IMAP4_SSL(IMAP_HOST, ssl_context=ssl.create_default_context(), timeout=60)
        m.login(require_env("GMAIL_ADDRESS"), require_env("GMAIL_APP_PASSWORD").replace(" ", ""))
        return m
    return _login(make, force)


def check_login():
    """Log in to SMTP and IMAP and log straight out (preflight). Tries even during a pause."""
    _login_smtp(force=True).quit()
    m = _login_imap(force=True)
    m.logout()


def send(subject, text, html=None, attachment=None, attachment_name=None):
    """Send to NOTIFY_EMAIL. Returns the Message-ID (replies point back to it)."""
    sender = require_env("GMAIL_ADDRESS")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Wanted Level bot <{sender}>"
    msg["To"] = require_env("NOTIFY_EMAIL")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype="video", subtype="mp4",
                               filename=attachment_name or os.path.basename(str(attachment)))
    s = _login_smtp()
    try:
        s.send_message(msg)
    finally:
        s.quit()
    log.info("emailed: %s", subject)
    return msg["Message-ID"]


def _body_text(m):
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    return m.get_payload(decode=True).decode(m.get_content_charset() or "utf-8", "replace")


def reply_verdict(text):
    """'posted', 'skip' or None from the first real line of a reply (quoted text below is ignored)."""
    for line in text.splitlines():
        line = line.strip().lower()
        if not line or line.startswith(">"):
            continue
        if re.match(r"^(on .+ wrote:|-{2,}|sent from)", line):
            return None
        m = re.match(r"^(?:launch|release|gta ?6?)\D{0,12}(\d{4}-\d{1,2}-\d{1,2})", line)
        if m:
            y, mo, d = m.group(1).split("-")
            return f"launch:{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        if re.match(r"^(pause|stop|vacation)\b", line):
            return "pause"
        if re.match(r"^(resume|unpause|start|back)\b", line):
            return "resume"
        if re.match(r"^(next|another|more|send another)\b", line):
            return "next"
        if re.match(r"^(join|joined|in|sign me up)\b", line):
            return "join"
        if re.match(r"^(posted|post|done|uploaded|yes)\b", line):
            return "posted"
        if re.match(r"^(skip|skipped|no|pass|nah)\b", line):
            return "skip"
        return None
    return None


def read_replies(since_days=7):
    """[(in_reply_to_message_id, 'posted'|'skip')] from unread replies to the bot's emails; marks them read."""
    out = []
    m = _login_imap()
    try:
        m.select("INBOX")
        nums = []
        for subj in ('"Post now"', '"Campaign:"', '"Claim:"', '"Weekly:"', '"Clip bot"'):
            typ, data = m.search(None, "UNSEEN", "SUBJECT", subj)
            nums += data[0].split() if data and data[0] else []
        for num in dict.fromkeys(nums):
            typ, parts = m.fetch(num, "(RFC822)")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            refs = (msg.get("References") or "").split()
            ref = (msg.get("In-Reply-To") or "").strip() or (refs[-1] if refs else "")
            verdict = reply_verdict(_body_text(msg))
            if ref and verdict:
                out.append((ref.strip(), verdict))
            m.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out
