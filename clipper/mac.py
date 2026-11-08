"""The Mac side: one always-on loop (started by launchd, restarted if it ever stops).

Every ~minute it reads Telegram (texts and button taps). Every ~30 minutes during waking hours it opens Content
Rewards in the bot's Chrome window and tells Elie on Telegram about anything that changed:
- an application approved (and what has to go in his bio for it), or still waiting
- a bio requirement he hasn't been told about yet
- a campaign that ended or dropped off his list (and which bio link he can now take out)
- drafts approved (time to post), rejected, or sent back with changes (with the reviewer's note)
- a joined campaign the clipper isn't putting at the front of the queue (with how to fix it)
Anything that breaks gets one Telegram message a day: what happened and the exact steps to fix it.

Text the bot "status" for a summary or "check" to run a check now. No Claude calls anywhere in here.

State lives in data/mac.json on the Mac (not committed). Secrets come from the .env file next to config.yaml.
"""
import hashlib
import json
import os
import random
import re
import subprocess
import time
import traceback
from datetime import datetime, timedelta

import yaml

from . import tg
from .common import DATA, ROOT, log, now_utc

STATE_FILE = DATA / "mac.json"
TZ = "America/Los_Angeles"
AWAKE = (8, 23)          # local hours the browser checks may run in
EVERY_MIN = 30           # minutes between Content Rewards checks (plus a random few)


def _add_tool_paths():
    import sys
    # ffmpeg (mac/enable-clipping.sh puts it in ~/.clipper/bin) and the tools installed with the bot's own Python,
    # like yt-dlp: the clipping jobs call them by name
    for tools in (os.path.dirname(sys.executable), os.path.expanduser("~/.clipper/bin")):
        if os.path.isdir(tools) and tools not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = tools + os.pathsep + os.environ.get("PATH", "")
    # The static ffmpeg build doesn't know where macOS keeps its trusted certificates, so every https stream
    # (Twitch's live and VOD playlists) failed with "certificate verify failed". OpenSSL reads SSL_CERT_FILE.
    if not os.environ.get("SSL_CERT_FILE"):
        bundle = "/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None
        if not bundle:
            try:
                import certifi
                bundle = certifi.where()
            except ImportError:
                bundle = None
        if bundle and sys.platform == "darwin":
            os.environ["SSL_CERT_FILE"] = bundle


def load_env(path=None):
    """KEY=value lines from .env into the environment (existing values win)."""
    p = path or (ROOT / ".env")
    _add_tool_paths()
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"campaigns": {}, "drafts": {}, "notes_seen": [], "tg_alerts": {}}


def save(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def local_now():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TZ))


def awake(now=None):
    h = (now or local_now()).hour
    return AWAKE[0] <= h < AWAKE[1]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def same_campaign(a, b):
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a in b or b in a)


# ------------------------------------------------------------- what the clipper (GitHub side) prioritizes
def bot_campaigns():
    """Campaign names the clipper currently puts first: config.yaml streamers with a campaign, plus campaigns marked
    joined in its state. Read from GitHub's main branch (where the running bot lives), falling back to local files."""
    def show(path):
        try:
            subprocess.run(["git", "-C", str(ROOT), "fetch", "-q", "origin", "main"], timeout=60, check=True,
                           capture_output=True)
            return subprocess.run(["git", "-C", str(ROOT), "show", f"origin/main:{path}"], timeout=30, check=True,
                                  capture_output=True, text=True).stdout
        except Exception:
            p = ROOT / path
            return p.read_text() if p.exists() else ""
    if clipping_on():   # the clipper runs here now: read its own files
        def show(path):  # noqa: F811
            p = ROOT / path
            return p.read_text() if p.exists() else ""
    names = []
    try:
        cfg = yaml.safe_load(show("config.yaml")) or {}
        names += [s["campaign"] for s in cfg.get("streamers") or [] if s.get("campaign") and s.get("enabled", True)]
    except Exception as e:
        log.info("couldn't read the clipper config: %s", e)
    try:
        state = json.loads(show("data/state.json") or "{}")
        names += [r["name"] for r in (state.get("campaigns") or {}).values() if r.get("status") == "joined"]
    except Exception as e:
        log.info("couldn't read the clipper state: %s", e)
    return names


# ------------------------------------------------------------- comparing checks and building messages
class Msg(str):
    """A message text that carries Telegram buttons (still a plain string everywhere else)."""
    rows = None


def _with_buttons(text, rows):
    m = Msg(text)
    m.rows = rows
    return m


def compare(st, camps, drafts, prioritized):
    """Return the Telegram messages for what changed since the last check, and update st in place."""
    msgs = []
    book = st.setdefault("campaigns", {})
    live_names = set()
    for c in camps:
        name = c["name"]
        live_names.add(name)
        old = book.get(name) or {}
        rec = dict(old, name=name, url=c.get("url") or old.get("url"), status=c.get("status"),
                   approved=bool(c.get("approved") or c.get("status") == "approved"), bio=c.get("bio") or [],
                   ended=bool(c.get("ended")), requirements=c.get("requirements") or [],
                   budget=[c.get("budget_used"), c.get("budget_total")], seen=now_utc().isoformat())
        link = f"\n{rec['url']}" if rec.get("url") else ""
        if rec["approved"] and not old.get("approved"):
            text = f"✅ You're approved for {name}. The bot will keep its clips at the front of the queue.{link}"
            if rec["bio"]:
                text += "\n\nThis campaign needs this in your TikTok and YouTube bios:\n" + "\n".join(rec["bio"])
                rec["bio_told"] = rec["bio"]
            msgs.append(text)
        elif not rec["approved"] and rec["status"] in ("pending", "applied", "under review") and not old:
            msgs.append(f"⏳ Applied to {name}. I'll tell you when they approve you.{link}")
        if rec["approved"] and rec["bio"] and rec.get("bio_told") != rec["bio"]:
            msgs.append(f"✏️ {name} needs this in your TikTok and YouTube bios:\n" + "\n".join(rec["bio"]) +
                        "\n\nAdd it now, or clips for this campaign can be rejected.")
            rec["bio_told"] = rec["bio"]
        if rec["ended"] and not old.get("ended"):
            msgs.append(_ended_text(name, rec, book))
        if (rec["approved"] and not rec["ended"] and not rec.get("bio_wait")
                and not any(same_campaign(name, p) for p in prioritized)):
            if old.get("priority_warned") != now_utc().date().isoformat():
                msgs.append(f"⚠️ You're in {name}, but the clipper isn't putting its streamer's clips first.\n\n"
                            "How to fix it: send Claude this message: \"add the campaign "
                            f"'{name}' to the clipper's config with its streamer\". Or on GitHub, open config.yaml and "
                            f"add the streamer under streamers: with  campaign: \"{name}\".{link}")
                rec["priority_warned"] = now_utc().date().isoformat()
        book[name] = rec
    if camps or st.get("listed_once"):
        for name, rec in book.items():
            if name not in live_names and not rec.get("ended") and rec.get("approved"):
                rec["ended"] = True
                msgs.append(_ended_text(name, rec, book, gone=True))
    st["listed_once"] = True

    counts = drafts.get("counts") or {}
    old_counts = st.get("drafts") or {}
    if old_counts:
        if counts.get("approved", 0) > old_counts.get("approved", 0):
            n = counts["approved"] - old_counts.get("approved", 0)
            msgs.append(f"🟢 {n} draft{'s' if n > 1 else ''} approved. Post {'them' if n > 1 else 'it'} on TikTok and "
                        "YouTube, then submit the live links on Content Rewards (Drafts > Approved).")
        if counts.get("rejected", 0) > old_counts.get("rejected", 0):
            n = counts["rejected"] - old_counts.get("rejected", 0)
            msgs.append(f"🔴 {n} draft{'s were' if n > 1 else ' was'} rejected. Check Drafts > Rejected for the reason.")
        if counts.get("posted", 0) > old_counts.get("posted", 0):
            n = counts["posted"] - old_counts.get("posted", 0)
            msgs.append(f"📤 {n} more draft{'s' if n > 1 else ''} marked posted on Content Rewards.")
    st["drafts"] = counts
    seen = st.setdefault("notes_seen", [])
    for n in drafts.get("needs_changes") or []:
        key = hashlib.sha1(f"{n.get('card')}|{n.get('when')}|{n.get('note')}".encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.append(key)
        text = (f"✂️ A draft{(' for ' + n['campaign']) if n.get('campaign') else ''} needs changes.\n\n"
                f"Reviewer ({n.get('who')}, {n.get('when')}): {n.get('note')}\n\n")
        if clipping_on() and not n.get("unread"):
            st.setdefault("fixes", {})[key] = {"key": key, "campaign": n.get("campaign"), "note": n.get("note"),
                                               "status": "new", "at": now_utc().isoformat()}
            msgs.append(_with_buttons(text + "Tap ✂️ Fix it and I'll change the clip to match the note and show it to "
                                      "you before sending. Or fix it yourself: Drafts > Needs changes > Upload revision.",
                                      [[("✂️ Fix it", f"fx:{key}")]]))
        else:
            msgs.append(text + "Fix the clip and use Upload revision on Content Rewards (Drafts > Needs changes).")
    del seen[:-200]
    return msgs


def _ended_text(name, rec, book, gone=False):
    why = "dropped off your campaign list" if gone else "has ended (or its budget ran out)"
    text = f"🏁 {name} {why}. The bot stops prioritizing its clips."
    still = {b for r in book.values() if r is not rec and r.get("approved") and not r.get("ended")
             for b in r.get("bio") or []}
    drop = [b for b in rec.get("bio") or [] if b not in still]
    if drop:
        text += "\n\nYou can take this out of your TikTok and YouTube bios now:\n" + "\n".join(drop)
    return text


def summary(st):
    camps = [r for r in (st.get("campaigns") or {}).values() if r.get("approved") and not r.get("ended")]
    lines = ["📊 Status"]
    lines += [f"• {r['name']}" + (f" (bio: {', '.join(r['bio'])})" if r.get("bio") else "") for r in camps] or \
        ["• No active campaigns"]
    d = st.get("drafts") or {}
    if d:
        lines.append("Drafts: " + ", ".join(f"{k} {v}" for k, v in d.items()))
    if st.get("last_sync"):
        try:
            t = datetime.fromisoformat(st["last_sync"]).astimezone(local_now().tzinfo)
            lines.append(f"Last Content Rewards check: {t:%b %-d, %-I:%M %p}")
        except (ValueError, TypeError):
            lines.append(f"Last Content Rewards check: {st['last_sync'][:16].replace('T', ' ')} UTC")
    b = st.get("backup") or {}
    if b.get("at"):
        try:
            bt = datetime.fromisoformat(b["at"]).astimezone(local_now().tzinfo)
            lines.append(f"Last backup: {bt:%b %-d, %-I:%M %p}" + ("" if b.get("ok") else " (failed)"))
        except (ValueError, TypeError):
            pass
    if not clipping_on():   # only while the GitHub bot still does the clipping
        ok = (st.get("relay") or {}).get("ok")
        lines.append(f"Watching the GitHub bot: {'yes, last read ' + ok[11:16] + ' UTC' if ok else 'not yet'}")
    else:
        lines.append("Clipping: on this Mac")
    return "\n".join(lines)


# ------------------------------------------------------------- one check, the loop, and problems
FIX_LOGIN = ("1. On the Mac, open Terminal.\n"
             "2. Paste: cd ~/projects/gta-clipper && .venv/bin/python -m clipper cr_login\n"
             "3. Log in to Content Rewards in the Chrome window that opens, then close it.")
FIX_CHROME = ("1. Make sure Google Chrome is in the Mac's Applications folder and opens normally.\n"
              "2. In Terminal: cd ~/projects/gta-clipper && .venv/bin/python -m playwright install-deps 2>/dev/null; "
              ".venv/bin/python -m clipper mac_once")


def check(st):
    from . import contentrewards as cr, crflow
    entries = crflow.load()
    with cr.Site() as s:
        camps = s.campaigns()
        drafts = s.drafts()
        cards = s.approved() if any(e.get("status") in ("submitted", "approved", "posted") for e in entries) else []
    msgs = compare(st, camps, drafts, bot_campaigns())
    waiting = any(e.get("status") == "submitted" for e in entries)
    newly = crflow.match_approvals(entries, cards) if cards else []
    if newly:
        crflow.save(entries)
    if newly or waiting:   # the bot's own drafts get a full post package instead of the generic "approved" note
        msgs = [m for m in msgs if not m.startswith("🟢")]
    st["last_sync"] = now_utc().isoformat()
    st["_approved_now"] = [e["token"] for e in newly]
    return msgs


def run_check(st, force=False):
    from . import contentrewards as cr
    try:
        msgs = check(st)
    except cr.NotLoggedIn:
        tg.problem(st, "cr_login", "The bot's Chrome window isn't logged in to Content Rewards, so it can't check "
                   "campaigns or drafts.", FIX_LOGIN)
        return
    except Exception as e:
        text = f"{type(e).__name__}: {str(e)[:300]}"
        log.error("check failed: %s\n%s", text, traceback.format_exc())
        if re.search(r"chrome|executable|browser", text, re.I):
            tg.problem(st, "chrome", f"The bot couldn't open Chrome ({text}).", FIX_CHROME)
        else:
            tg.problem(st, "check:" + type(e).__name__, f"The Content Rewards check failed: {text}",
                       "Usually it fixes itself on the next check (30 minutes). If this keeps coming back, send this "
                       "message to Claude and it will look at the cause.")
        return
    for m in msgs:
        tg.send(m, getattr(m, "rows", None))
        time.sleep(1)
    st.pop("_approved_now", None)
    send_packages_due(st)
    if force and not msgs:
        tg.send("Checked Content Rewards: nothing new.\n\n" + summary(st))


def handle(st, kind, data, message_id, text):
    low = (data or "").lower()
    if kind == "tap" and data.startswith("jf:keep:"):
        from . import mediaclean
        try:
            mediaclean.keep(data.split(":", 2)[2])
            title = (text or "").split(" gets deleted tomorrow")[0].lstrip("🗑 ").strip() or "It"
            tg.edit(message_id, f"❤️ Kept: {title}. It's a favorite in Jellyfin now, so it won't be deleted.")
        except Exception as e:
            tg.send(f"Couldn't mark it as kept ({str(e)[:150]}). In the Jellyfin app, tap the heart on it instead.")
        return
    if kind == "tap" and re.match(r"^fx[sm]?:", data or ""):
        what, key = data.split(":", 1)
        fix = (st.get("fixes") or {}).get(key)
        if not fix:
            tg.edit(message_id, f"{(text or '')[:3500]}\n\n(That fix is no longer open.)")
            return
        if what == "fx" and fix.get("status") in ("new", "failed", "cant"):
            fix["status"] = "todo"
            tg.edit(message_id, f"{(text or '')[:3500]}\n\n✂️ Working on it. I'll send you the new version first.")
        elif what == "fxs" and fix.get("status") == "ready":
            fix["status"] = "upload"
            tg.edit(message_id, f"{(text or '')[:3500]}\n\n📤 Sending it…")
        elif what == "fxm":
            fix["status"] = "manual"
            tg.edit(message_id, f"{(text or '')[:3500]}\n\n✋ OK, it's yours: Drafts > Needs changes > Upload revision.")
        return
    if kind == "tap" and data.startswith("cj:"):
        _, what, token = (data.split(":", 2) + ["", ""])[:3]
        finish_join(st, token, message_id, text)
        return
    if kind == "tap" and data.startswith("r:"):
        from . import mail, crflow
        _, verdict, token = (data.split(":", 2) + ["", ""])[:3]
        if verdict == "join" and clipping_on() and start_join(st, token, message_id, text):
            return
        mail.queue_reply(f"tg:{token}", verdict)
        entries = crflow.load()
        e = crflow.by_token(entries, token)
        if e and e.get("status") == "approved" and verdict in ("posted", "skip"):
            if verdict == "posted":
                e.update(status="posted", posted_at=now_utc().isoformat())
                crflow.save(entries)
                tg.edit(message_id, f"{(text or '')[:3500]}\n\n✅ Marked posted")
                tg.send(f"🔎 Looking for your posts to submit to {e.get('campaign')}. Give it a few minutes. "
                        "You can also paste the TikTok or YouTube link here and I'll submit it right away.")
                st["hunt_next"] = 0
            else:
                e["status"] = "skipped"
                crflow.save(entries)
                tg.edit(message_id, f"{(text or '')[:3500]}\n\n⏭ Skipped")
            return
        done = {"posted": "✅ Marked posted", "skip": "⏭ Skipped (a replacement comes at the next slot)",
                "next": "➕ Another clip is on its way", "join": "✅ Joined: its clips go first now. Apply with the "
                "link above if you haven't yet; I'll tell you when they approve you.", "skip_c": ""}.get(verdict, "Got it")
        tg.edit(message_id, f"{(text or '')[:3500]}\n\n{done}")
        return
    if kind == "text" and re.search(r"https?://\S*(tiktok\.com|youtube\.com|youtu\.be)/", data or "", re.I):
        submit_pasted_link(st, re.search(r"https?://\S+", data).group(0))
        return
    if kind == "text":
        if clipping_on() and low not in ("status", "/status", "check", "/check", "cleanup", "/cleanup", "backup", "/backup",
                                         "earnings", "/earnings", "money",
                                         "/start", "start", "help", "/help"):
            from . import mail
            verdict = mail.reply_verdict(data or "")
            if verdict in ("pause", "resume", "next") or (verdict or "").startswith("launch:"):
                mail.queue_reply("", verdict)
                tg.send({"pause": "⏸ Posting messages paused (clips keep collecting). Text resume to restart.",
                         "resume": "▶️ Posting messages back on.", "next": "➕ Sending another clip shortly."}
                        .get(verdict, f"📅 Got it: {verdict}"), silent=True)
                return
        if low in ("status", "/status"):
            tg.send(summary(st))
        elif low in ("check", "/check"):
            tg.send("Checking Content Rewards now…", silent=True)
            run_check(st, force=True)
        elif low in ("earnings", "/earnings", "money"):
            tg.send("Reading the Earnings page…", silent=True)
            run_earnings(st, force=True)
        elif low in ("backup", "/backup"):
            r = run_backup(st)
            if r["ok"]:
                tg.send(f"💾 Backed up {r['size_mb']} MB to the Work SSD: {', '.join(r['copied'])}."
                        + (f"\nNot found (skipped): {', '.join(r['missing'])}" if r["missing"] else "")
                        + (f"\nNotes: {'; '.join(r['problems'])}" if r["problems"] else ""))
        elif low in ("cleanup", "/cleanup"):
            st.pop("jf_key_asked", None)
            run_cleanup(st)
            tg.send("Clean-up done.", silent=True)
        elif low in ("/start", "start", "help", "/help"):
            tg.send("I'm the Wanted Level clip bot. I'll message you about campaigns, your bio, drafts and problems.\n"
                    "Text \"status\" for a summary, \"check\" to look at Content Rewards right now, or \"earnings\" for "
                    "what you've made.")


FIX_JF_KEY = ("1. On the Mac, open http://localhost:8096 and log in.\n"
              "2. Click the menu (☰), then Dashboard, then API Keys, then the + button. Name it clipbot.\n"
              "3. Copy the key. In Terminal paste: open -e ~/projects/gta-clipper/.env\n"
              "4. Add a new line: JELLYFIN_API_KEY=the-key  then save. The bot picks it up within a day "
              "(or text it \"cleanup\" after restarting it).")


def run_backup(st):
    """Nightly (3 AM+): copy the things that can't be rebuilt onto the Work SSD. Quiet unless it fails."""
    from . import backup
    r = backup.run()
    st["backup"] = {"at": now_utc().isoformat(), "ok": r["ok"], "size_mb": r.get("size_mb"), "missing": r["missing"]}
    if not r["ok"]:
        tg.problem(st, "backup", f"Last night's backup didn't finish ({'; '.join(r['problems'])[:200]}).",
                   "1. Check the Work SSD is plugged in (it shows as Work in Finder).\n"
                   "2. Text \"backup\" to the bot to try again.")
    return r


def run_cleanup(st):
    """Delete titles watched 3+ days ago (warn the day before). Problems come back as one message a day."""
    from . import mediaclean
    load_env()
    if not mediaclean.configured():
        if not st.get("jf_key_asked"):
            st["jf_key_asked"] = now_utc().isoformat()
            tg.send("🧹 To turn on auto-delete for watched movies and shows (3 days after watching, no messages; "
                    "anything you heart in Jellyfin is kept), Jellyfin needs to give the bot a key:\n\n" + FIX_JF_KEY)
        return
    try:
        mediaclean.run(st, lambda text, rows: tg.send(text, rows))
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg:
            fix = "The Jellyfin key in the .env file was rejected. Make a new one:\n" + FIX_JF_KEY
        elif "Connection" in msg or "refused" in msg.lower() or "Max retries" in msg:
            fix = ("Jellyfin isn't running. Open Jellyfin from Applications on the Mac (and in its menu bar icon, "
                   "make sure it opens at login).")
        else:
            fix = "If this repeats tomorrow, send this message to Claude."
        tg.problem(st, "jellyfin", f"The watched-title clean-up failed: {msg[:250]}", fix)


# ------------------------------------------------------------- one-tap fix for "needs changes"
def process_fixes(st, jobs):
    """Run the fixes Elie tapped: start the revise job, pick up its result, upload the revision when he says so."""
    from . import crflow, fixer
    fixes = st.get("fixes") or {}
    for key, fix in list(fixes.items()):
        status = fix.get("status")
        if status == "todo":
            if jobs is None or jobs.busy():
                continue   # one job at a time (they share the clip state)
            entries = crflow.load()
            cands = [e for e in entries if e.get("status") == "submitted"
                     and (not fix.get("campaign") or same_campaign(e.get("campaign"), fix["campaign"]))]
            if not cands:
                fix["status"] = "cant"
                tg.send("✋ I couldn't tell which of my uploads that note is about, so this one needs fixing by hand: "
                        "Drafts > Needs changes > Upload revision.")
                continue
            e = sorted(cands, key=lambda x: x.get("submitted_at") or "")[0]
            fixer.JOB.write_text(json.dumps({"key": key, "clip_id": e["id"], "token": e["token"], "note": fix["note"],
                                             "campaign": e.get("campaign"), "status": "running"}))
            fix.update(status="running", token=e["token"], started=now_utc().isoformat())
            jobs.cur = jobs._start("revise")
        elif status == "running":
            try:
                job = json.loads(fixer.JOB.read_text())
            except (OSError, ValueError):
                job = {}
            if job.get("key") == key and job.get("status") in ("ready", "cant", "failed"):
                fix["status"] = job["status"]
                fix["file"] = job.get("file")
            elif jobs is not None and not jobs.busy():   # the job ended without a result: it crashed
                fix["status"] = "failed"
                tg.send("⚠️ Making the fixed clip failed. Tap ✂️ Fix it again to retry, or fix it by hand: "
                        "Drafts > Needs changes > Upload revision. (Details are in the Mac's logs/revise.log.)")
        elif status == "upload":
            from . import contentrewards as cr
            try:
                with cr.Site() as s:
                    s.upload_revision(fix.get("campaign"), fix["file"])
            except cr.NotLoggedIn:
                tg.problem(st, "cr_login", "The bot's Chrome window isn't logged in to Content Rewards, so it couldn't "
                           "send the revision.", FIX_LOGIN)
                return
            except Exception as ex:
                fix["status"] = "manual"
                if os.path.exists(fix.get("file") or ""):
                    tg.send_video(fix["file"], "✂️ The fixed clip")
                tg.send(f"⚠️ Content Rewards didn't take the revision ({str(ex)[:150]}). Upload the video above "
                        "yourself: Drafts > Needs changes > See revisions > Upload revision.")
                continue
            fix["status"] = "sent"
            tg.send(f"📤 Revision sent to {fix.get('campaign')}. I'll send the post package once they approve it.")
    if len(fixes) > 50:
        for k in sorted(fixes, key=lambda k: fixes[k].get("at", ""))[:-50]:
            fixes.pop(k)


# ------------------------------------------------------------- Monday earnings report
def earnings_text(e, last=None, claude=None, budget=None, times=None):
    total = round((e.get("queued") or 0) + (e.get("arriving") or 0) + (e.get("received") or 0), 2)
    lines = [f"💵 Earnings: ${total:.2f} so far"]
    if last is not None:
        diff = total - last
        lines[0] += f" ({'+' if diff >= 0 else '-'}${abs(diff):.2f} since last Monday)"
    by = {}
    for it in e.get("items") or []:
        by.setdefault(it["campaign"], [0.0, 0])
        by[it["campaign"]][0] += it.get("amount") or 0
        by[it["campaign"]][1] += 1
    for name, (amt, n) in sorted(by.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"• {name}: ${amt:.2f} from {n} clip{'s' if n != 1 else ''}")
    lines.append("")
    lines.append(f"Still counting views: ${e.get('queued') or 0:.2f}")
    lines.append(f"Approved, on the way: ${e.get('arriving') or 0:.2f}")
    lines.append(f"Paid to you: ${e.get('received') or 0:.2f}  |  Balance to withdraw: ${e.get('balance') or 0:.2f}")
    dates = [it["pays"] for it in e.get("items") or [] if it.get("pays") and it["section"] in ("queued", "arriving")]
    if dates:
        lines.append(f"Next payout date: {dates[0]}")
    if claude is not None:
        lines.append("")
        lines.append(f"Claude API, last 30 days: ${claude:.2f}" + (f" of ${budget:.2f}" if budget else ""))
    if times:
        lines.append("")
        lines += times
    return "\n".join(lines)


def run_earnings(st, force=False):
    """Monday 9 AM+ (or the "earnings" text): read Content Rewards' Earnings page and send the report."""
    from . import contentrewards as cr
    from .common import load_config, spend_last_days
    week = local_now().strftime("%G-W%V")
    if not force and (local_now().weekday() != 0 or local_now().hour < 9 or st.get("earn_week") == week):
        return
    try:
        with cr.Site() as s:
            e = s.earnings()
    except cr.NotLoggedIn:
        tg.problem(st, "cr_login", "The bot's Chrome window isn't logged in to Content Rewards, so it couldn't read "
                   "your earnings.", FIX_LOGIN)
        return
    except Exception as ex:
        log.warning("earnings read failed: %s", ex)
        if force:
            tg.send(f"Couldn't read the Earnings page ({str(ex)[:150]}). I'll try again next Monday.")
        return
    times = None
    try:
        main = json.loads((DATA / "state.json").read_text())
        claude = spend_last_days(main, 30)
        from . import timing
        cfg = load_config()
        tz = cfg.get("timezone", "America/Los_Angeles")
        fav = (main.get("timing_favorite") or {}).get("time") or timing.settings(cfg).get("favorite")
        times = ([f"🕐 Posting times (YouTube views {timing.settings(cfg)['measure_hours']}h after posting):"] +
                 [ln.lstrip("- ") for ln in timing.report(cfg, main, tz)] +
                 ([f"Daily slot: {fav} (moves by itself if another time clearly beats it)"] if fav else []))
    except Exception:
        claude = None
    budget = ((load_config().get("limits") or {}).get("claude_usd_per_month"))
    total = round((e.get("queued") or 0) + (e.get("arriving") or 0) + (e.get("received") or 0), 2)
    tg.send(earnings_text(e, (st.get("earn_last") or {}).get("total"), claude, budget, times))
    if not force:
        st["earn_week"] = week
    st["earn_last"] = {"total": total, "at": now_utc().isoformat()}


# ------------------------------------------------------------- joining a campaign when Elie taps Join
JOINS = DATA / "cr_joins.json"


def joins_load():
    try:
        return json.loads(JOINS.read_text())
    except (OSError, ValueError):
        return []


def joins_save(items):
    JOINS.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOINS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[-100:], indent=2))
    tmp.replace(JOINS)


def _clipper_campaign(token):
    """The clipper's record of the campaign whose Telegram message carried this token."""
    try:
        state = json.loads((DATA / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return next((r for r in (state.get("campaigns") or {}).values()
                 if r.get("email_message_id") == f"tg:{token}"), None)


def start_join(st, token, message_id, text):
    """Join tapped: queue the join for the loop (it opens Chrome, so it isn't done inside the tap). False if the
    campaign isn't known, so the old "mark it joined" behaviour runs instead."""
    rec = _clipper_campaign(token)
    if not rec or not (rec.get("join_url") or rec.get("url")):
        return False
    items = [j for j in joins_load() if j.get("token") != token]
    items.append({"token": token, "name": rec.get("name"), "link": rec.get("join_url") or rec.get("url"),
                  "status": "todo", "tries": 0, "message_id": message_id, "at": now_utc().isoformat()})
    joins_save(items)
    tg.edit(message_id, f"{(text or '')[:3500]}\n\n⏳ Joining it on Content Rewards now…")
    return True


def _mark_joined(token):
    from . import mail
    mail.queue_reply(f"tg:{token}", "join")   # the clipper puts this streamer's clips first from its next run


def process_joins(st):
    """Join queued campaigns on Content Rewards. If the campaign needs something in Elie's bio, he gets the text and
    an "✅ Added" button; the clipper only starts treating it as joined once he taps it."""
    from . import contentrewards as cr
    items = joins_load()
    todo = [j for j in items if j.get("status") == "todo"]
    if not todo:
        return
    for j in todo[:2]:
        try:
            with cr.Site() as s:
                res = s.join(j["link"])
        except cr.NotLoggedIn:
            tg.problem(st, "cr_login", "The bot's Chrome window isn't logged in to Content Rewards, so it couldn't "
                       "join the campaign you tapped.", FIX_LOGIN)
            return
        except Exception as ex:
            j["tries"] = j.get("tries", 0) + 1
            log.warning("join failed (%s): %s", j.get("name"), ex)
            if j["tries"] >= 2:
                j["status"] = "manual"
                tg.send(f"⚠️ I couldn't join {j.get('name')} by myself ({str(ex)[:150]}).\n\n"
                        f"1. Open {j['link']}\n2. Click Join Campaign.\n3. Tap ✅ Joined below.",
                        [[("✅ Joined", f"cj:done:{j['token']}")]])
            joins_save(items)
            continue
        j["cr_url"], j["cr_name"] = res.get("url"), res.get("name") or j.get("name")
        if res.get("elsewhere"):
            j["status"] = "manual"
            tg.send(f"🔗 {j.get('name')} isn't on Content Rewards, so I can't join it for you.\n\n"
                    f"1. Open {res.get('url')}\n2. Join it there (sign in with the channel Gmail).\n"
                    "3. Tap ✅ Joined below.", [[("✅ Joined", f"cj:done:{j['token']}")]])
        elif not res.get("joined"):
            j["status"] = "manual"
            tg.send(f"📝 {j.get('name')} asked for more than a Join click, so I stopped without sending anything. "
                    f"It said: {res.get('form')}\n\n1. Open {res.get('url')}\n2. Click Join Campaign and fill it in.\n"
                    "3. Tap ✅ Joined below.", [[("✅ Joined", f"cj:done:{j['token']}")]])
        else:
            bio = res.get("bio") or []
            book = st.setdefault("campaigns", {})
            name = j["cr_name"]
            book[name] = dict(book.get(name) or {}, name=name, url=res.get("url"), approved=True, bio=bio,
                              bio_told=bio, bio_wait=bool(bio), status="joined", ended=False,
                              seen=now_utc().isoformat())
            extra = ("\n\nIts clips go to the brand for approval first. I upload them as drafts and send you the "
                     "post package once they're approved." if res.get("preapproval") else "")
            if bio:
                j["status"] = "bio_wait"
                tg.send(f"✅ Joined {name} on Content Rewards.\n\nOne thing before its clips count: put this in your "
                        "TikTok and YouTube bios:\n" + "\n".join(bio) +
                        "\n\nTap ✅ Added once it's in both, and I'll start putting its clips first." + extra,
                        [[("✅ Added", f"cj:added:{j['token']}")]])
            else:
                j["status"] = "done"
                _mark_joined(j["token"])
                tg.send(f"✅ Joined {name} on Content Rewards. Its clips go to the front of the queue now.\n"
                        f"{res.get('url')}{extra}")
        joins_save(items)


def finish_join(st, token, message_id, text):
    """✅ Added (bio done) or ✅ Joined (he joined by hand): the campaign counts as joined from now on."""
    items = joins_load()
    j = next((x for x in items if x.get("token") == token), None)
    _mark_joined(token)
    if j:
        j["status"] = "done"
        joins_save(items)
        rec = (st.get("campaigns") or {}).get(j.get("cr_name") or "")
        if rec:
            rec["bio_wait"] = False
    name = (j or {}).get("cr_name") or (j or {}).get("name") or "the campaign"
    tg.edit(message_id, f"{(text or '')[:3500]}\n\n✅ All set: {name}'s clips go to the front of the queue now.")


def _campaign_url(st, name):
    for rec in (st.get("campaigns") or {}).values():
        if rec.get("url") and same_campaign(rec.get("name"), name):
            return rec["url"]
    for j in joins_load():
        if j.get("cr_url") and (same_campaign(j.get("cr_name"), name) or same_campaign(j.get("name"), name)):
            return j["cr_url"]
    return None


def _auto_youtube(st, e):
    """Approved campaign clip: post it on YouTube through Blotato and submit that link right away (Content Rewards
    takes links for 30 minutes). Returns True if YouTube is done. Never raises."""
    from . import jobs, services, crflow
    from .common import load_config
    cfg = load_config()
    if "youtube" not in jobs.auto_platforms(cfg) or "youtube" not in (e.get("platforms") or []) \
            or "youtube" in (e.get("links") or {}) or not os.path.exists(e.get("file") or ""):
        return False
    try:
        title = next((t for lbl, t in e.get("blocks") or [] if lbl == "YouTube title"), e.get("title") or e.get("hook"))
        desc = next((t for lbl, t in e.get("blocks") or [] if lbl == "YouTube description"), e.get("caption") or "")
        status, url = services.blotato_post_file("youtube", e["file"], desc, title=title,
                                                 ai_label=(cfg.get("posting") or {}).get("label_ai_generated"))
    except Exception as ex:
        tg.problem(st, "blotato", f"Blotato didn't post \"{e.get('hook')}\" on YouTube ({str(ex)[:150]}). "
                   "It's in your post package instead.",
                   "1. In Blotato, check Accounts shows your YouTube channel connected.\n"
                   "2. Check BLOTATO_API_KEY in ~/projects/gta-clipper/.env matches Settings > API in Blotato.")
        return False
    if status == "failed":
        tg.problem(st, "blotato", f"Blotato couldn't post on YouTube: {str(url)[:200]}. It's in your post package "
                   "instead.", "Open Blotato and check the post's error, then reconnect YouTube under Accounts.")
        return False
    e["yt_auto_at"] = now_utc().isoformat()
    if url:
        _submit(st, e, "youtube", url)
    else:   # still processing: the 2-minute check picks it from Content Rewards' list
        e.setdefault("links", {})
        e["yt_pending"] = True
    entries = crflow.load()
    for x in entries:
        if x.get("token") == e.get("token"):
            x.update({k: e[k] for k in ("links", "yt_auto_at") if k in e}, yt_pending=e.get("yt_pending"))
    crflow.save(entries)
    return True


def send_post_package(e, manual=False, st=None):
    """The approved (or hand-submit) post package: video, info, then each field alone, buttons on the last.
    With YouTube on Blotato, YouTube is posted (and its link submitted) first and the package is TikTok only."""
    from . import crflow, relay
    if not manual and st is not None and _auto_youtube(st, e):
        e = dict(e, blocks=[b for b in e.get("blocks") or [] if not b[0].startswith("YouTube")]
                 + [("YouTube: already posted", "The bot posted this on YouTube and submitted the link. "
                     "Just post it on TikTok, then tap ✅ Posted.")])
        if "tiktok" not in (e.get("platforms") or []):
            return
    info, fields = crflow.post_parts(e, manual=manual)
    caption = f"📲 Post now: {e.get('hook')}"
    if os.path.exists(e.get("file") or ""):
        tg.send_video(e["file"], caption)
    for chunk in relay._chunks(info):
        tg.send(chunk)
    rows = [[("✅ Posted", f"r:posted:{e['token']}"), ("⏭ Skip", f"r:skip:{e['token']}")]]
    relay.send_fields(fields, rows)


STALE_DAYS = 3   # a campaign clip that waited this long for a daily place is dropped


def _local_day(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(local_now().tzinfo).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _today_count(entries, campaign, field):
    today = local_now().date().isoformat()
    return sum(1 for e in entries if same_campaign(e.get("campaign"), campaign) and _local_day(e.get(field)) == today
               and e.get("status") not in ("queued", "dropped"))


def send_packages_due(st):
    """Post packages for approved drafts: at most clip.campaign.max_per_day per campaign per day, best score
    first; the rest wait for tomorrow."""
    from . import crflow
    from .common import load_config
    entries = crflow.load()
    if not st.get("pkg_migrated"):   # approvals from before the daily limit were already sent
        old = (now_utc() - timedelta(minutes=10)).isoformat()
        for e in entries:
            if e.get("status") in ("approved", "posted", "done", "manual") and not e.get("packaged_at") \
                    and (e.get("approved_at") or "") < old:
                e["packaged_at"] = e.get("approved_at") or now_utc().isoformat()
        st["pkg_migrated"] = True
        crflow.save(entries)
    cap = crflow.per_day(load_config())
    sent = 0
    for e in crflow.best_first([e for e in entries if e.get("status") == "approved" and not e.get("packaged_at")]):
        if _today_count(entries, e.get("campaign"), "packaged_at") >= cap:
            continue
        e["packaged_at"] = now_utc().isoformat()
        crflow.save(entries)
        send_post_package(e, st=st)
        sent += 1
    return sent


def process_drafts(st):
    """Upload queued campaign clips as Content Rewards drafts (during waking hours, at a person's pace), at most
    clip.campaign.max_per_day per campaign per day, best score first."""
    from . import contentrewards as cr, crflow
    from .common import load_config
    cap = crflow.per_day(load_config())
    entries = crflow.load()
    cutoff = (now_utc() - timedelta(days=STALE_DAYS)).isoformat()
    stale = [e for e in entries if e.get("status") == "queued" and (e.get("queued_at") or "") < cutoff]
    for e in stale:   # never got a daily place; newer, better clips took them
        e["status"] = "dropped"
        try:
            os.remove(e["file"])
        except (OSError, KeyError):
            pass
    if stale:
        crflow.save(entries)
    todo = []
    for e in crflow.best_first([e for e in entries if e.get("status") == "queued"]):
        if _today_count(entries, e.get("campaign"), "submitted_at") + \
                sum(1 for t in todo if same_campaign(t.get("campaign"), e.get("campaign"))) < cap:
            todo.append(e)
    for e in todo[:2]:
        url = _campaign_url(st, e.get("campaign"))
        if not url:
            e["tries"] = e.get("tries", 0) + 1
            if e["tries"] >= 3:   # the campaign page isn't known (not joined?): hand it to Elie
                e["status"] = "manual_draft"
                tg.send(f"⚠️ I couldn't find the {e.get('campaign')} campaign page on Content Rewards, so this clip "
                        "needs its draft submitted by hand:")
                send_post_package(e, manual=True)
            crflow.save(entries)
            continue
        try:
            with cr.Site() as s:
                s.submit_draft(url, e["file"], platforms=e.get("platforms") or ("tiktok", "youtube"))
        except cr.NotLoggedIn:
            tg.problem(st, "cr_login", "The bot's Chrome window isn't logged in to Content Rewards, so it can't "
                       "upload campaign drafts.", FIX_LOGIN)
            return
        except Exception as ex:
            e["tries"] = e.get("tries", 0) + 1
            log.warning("draft upload failed (%s): %s", e["id"], ex)
            if e["tries"] >= 3:
                e["status"] = "manual_draft"
                tg.send(f"⚠️ Uploading the draft to {e.get('campaign')} failed 3 times ({str(ex)[:150]}). "
                        "Submit this one by hand:")
                send_post_package(e, manual=True)
            crflow.save(entries)
            continue
        e.update(status="submitted", submitted_at=now_utc().isoformat())
        crflow.save(entries)
        tg.send(f"📤 Sent to {e.get('campaign')} for approval: {e.get('hook')}\n\n"
                "I'll send you the post package as soon as they approve it.", silent=True)


def _uploads(platforms_):
    from . import clipqueue
    from .common import load_config
    acct = (load_config().get("accounts") or {})
    out = {}
    if "tiktok" in platforms_ and acct.get("tiktok"):
        try:
            out["tiktok"] = clipqueue.tiktok_uploads(acct["tiktok"], 10)
        except Exception as ex:
            log.info("tiktok read failed: %s", ex)
    if "youtube" in platforms_ and acct.get("youtube"):
        try:
            out["youtube"] = clipqueue.youtube_shorts(acct["youtube"], 10)
        except Exception as ex:
            log.info("youtube read failed: %s", ex)
    return out


def _submit(st, e, platform, url):
    from . import contentrewards as cr
    try:
        with cr.Site() as s:
            s.submit_link(e.get("campaign"), e.get("duration"), url)
    except Exception as ex:
        tg.send(f"⚠️ Content Rewards didn't take your {platform} link ({str(ex)[:200]}).\n\nSubmit it yourself: "
                f"Drafts > Approved > Submit link, and paste:\n{url}")
        e.setdefault("links", {})[platform] = "failed:" + url
        return False
    e.setdefault("links", {})[platform] = url
    tg.send(f"💰 Submitted your {platform} post to {e.get('campaign')}:\n{url}")
    return True


def _finish_if_done(e):
    if all(p in (e.get("links") or {}) for p in e.get("platforms") or []):
        e["status"] = "done"


def _pick_platform(e, label):
    from .clipqueue import similarity
    from .contentrewards import parse_pick_label
    t = parse_pick_label(label)["text"]
    yt = similarity(e.get("title", ""), t)
    tt = similarity(e.get("caption", ""), t)
    left = [p for p in e.get("platforms") or [] if p not in (e.get("links") or {})]
    want = "youtube" if yt > tt else "tiktok"
    return want if want in left else (left[0] if left else want)


def hunt(st):
    """Every 2 minutes after Posted: submit the new posts before the 30-minute cutoff. First from Content Rewards'
    own list of your recent posts (it shows only the ones still inside the window); if that finds nothing, by
    reading your TikTok/YouTube pages for the links."""
    from . import crflow, contentrewards as cr
    entries = crflow.load()
    active = crflow.hunting(entries)
    changed = False
    if active:
        try:
            with cr.Site() as s:
                for e in active:
                    for _ in range(len(e.get("platforms") or [])):
                        if all(p in (e.get("links") or {}) for p in e.get("platforms") or []):
                            break
                        label = s.submit_pick(e.get("campaign"), e.get("duration"),
                                              [e.get("caption"), e.get("title"), e.get("hook")])
                        if not label:
                            break
                        platform = _pick_platform(e, label)
                        e.setdefault("links", {})[platform] = "picked:" + label[:120]
                        changed = True
                        tg.send(f"💰 Submitted your {platform} post to {e.get('campaign')}: "
                                f"\"{cr.parse_pick_label(label)['text'][:80]}\"")
                    _finish_if_done(e)
        except cr.NotLoggedIn:
            tg.problem(st, "cr_login", "The bot's Chrome window isn't logged in to Content Rewards, so it couldn't "
                       "submit your post.", FIX_LOGIN)
        except Exception as ex:
            log.warning("submitting from the post list failed: %s", ex)
        still = [e for e in active if e.get("status") == "posted"]
        if still:
            ups = _uploads({p for e in still for p in e.get("platforms") or []})
            for e in still:
                for platform, url in crflow.find_posts(e, ups).items():
                    _submit(st, e, platform, url)
                    changed = True
                _finish_if_done(e)
    for e in crflow.expired(entries):
        missing = [p for p in e.get("platforms") or [] if p not in (e.get("links") or {})]
        e["status"] = "manual"
        changed = True
        tg.send(f"⏰ I couldn't submit your {' and '.join(missing)} post for \"{e.get('hook')}\" in time. Paste the "
                "link here right now and I'll submit it, or do it yourself: Drafts > Approved > Submit link. "
                "Content Rewards only takes it within 30 minutes of posting.")
    if changed:
        crflow.save(entries)


def submit_pasted_link(st, url):
    from . import crflow
    platform = crflow.platform_of(url)
    entries = crflow.load()
    waiting = [e for e in entries if e.get("status") in ("posted", "manual", "approved")
               and platform in (e.get("platforms") or []) and platform not in (e.get("links") or {})]
    if not waiting:
        tg.send("I don't have an approved campaign draft waiting for that link.")
        return
    e = sorted(waiting, key=lambda e: e.get("approved_at") or "")[-1]
    if _submit(st, e, platform, url):
        _finish_if_done(e)
    crflow.save(entries)


RELAY_EVERY = 3 * 60      # seconds between reads of the GitHub bot's state
UPDATE_EVERY = 30 * 60    # seconds between checks for a newer version of this code


def self_update():
    """Pull the newest Mac code; if it changed, install any new packages and exit so launchd restarts us on it.
    Returns quietly if there's nothing new or git can't reach GitHub right now."""
    def git(*a):
        return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, timeout=120)
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "mac-server"
    before = git("rev-parse", "HEAD").stdout.strip()
    if git("fetch", "-q", "origin", branch).returncode != 0:
        return False
    if git("rev-parse", f"origin/{branch}").stdout.strip() == before:
        return False
    old_req = git("show", f"{before}:requirements.txt").stdout
    if git("merge", "--ff-only", "-q", f"origin/{branch}").returncode != 0:
        log.warning("couldn't fast-forward to the new code (local changes?); staying on this version")
        return False
    if (ROOT / "requirements.txt").read_text() != old_req:
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt"),
                        "playwright"], timeout=900)
    log.info("updated to %s; restarting", git("rev-parse", "--short", "HEAD").stdout.strip())
    return True


def clipping_on():
    """MAC_RUNS_CLIPPER=1 in .env: the Mac runs the clipping jobs itself (GitHub's are paused)."""
    return os.environ.get("MAC_RUNS_CLIPPER") == "1"


class Jobs:
    """GitHub's schedule, run on the Mac: posting check every 15 min, stream check hourly (it posts too), campaign
    scan daily from 7 AM, report Mondays from 8 AM, the long video when weekly.due() says so, and the live watcher
    always on. Main jobs run one at a time (they share data/state.json); the live watcher writes its own file."""

    def __init__(self):
        import sys
        self.py, self.cur, self.live = sys.executable, None, None
        now = time.time()
        self.next_post, self.next_process, self.next_daily = now + 60, now + 180, now + 300
        self.pidfile = DATA / "live.pid"
        try:   # a live watcher left over from before a restart: stop it so two never run at once
            import signal
            os.kill(int(self.pidfile.read_text()), signal.SIGTERM)
            time.sleep(5)
        except (OSError, ValueError):
            pass

    def busy(self):
        return self.cur is not None and self.cur.poll() is None

    def stop_live(self):
        if self.live is not None and self.live.poll() is None:
            self.live.terminate()
            try:
                self.live.wait(30)
            except subprocess.TimeoutExpired:
                self.live.kill()

    def _start(self, job):
        log.info("starting job %s", job)
        logf = open(DATA.parent / "logs" / f"{job}.log", "ab") if (DATA.parent / "logs").exists() else None
        return subprocess.Popen([self.py, "-m", "clipper", job], cwd=str(ROOT), stdout=logf or subprocess.DEVNULL,
                                stderr=subprocess.STDOUT)

    def drive_ok(self, st):
        work = os.environ.get("CLIPPER_WORK", "")
        if work.startswith("/Volumes/") and not os.path.isdir("/Volumes/" + work.split("/")[2]):
            tg.problem(st, "work_drive", f"The Work SSD isn't connected ({work}), so clipping is paused.",
                       "Plug the Work SSD back into the Mac. Clipping picks up again within a few minutes.")
            return False
        return True

    def tick(self, st):
        now = time.time()
        if not self.drive_ok(st):
            return
        if self.live is None or self.live.poll() is not None:   # the live watcher ends every ~6 hours: restart it
            self.live = self._start("live")
            self.pidfile.write_text(str(self.live.pid))
        if self.cur is not None and self.cur.poll() is None:
            return   # a main job is still running
        self.cur = None
        lt = local_now()
        last = st.setdefault("jobs_last", {})
        today = lt.date().isoformat()
        if now >= self.next_process:
            self.next_process, self.next_post = now + 3600, now + 15 * 60
            self.cur = self._start("process")
        elif now >= self.next_post:
            self.next_post = now + 15 * 60
            self.cur = self._start("post")
        elif now >= self.next_daily:
            self.next_daily = now + 600
            if lt.hour >= 7 and last.get("analytics") != today:
                last["analytics"] = today
                self.cur = self._start("analytics")
            elif lt.weekday() == 0 and lt.hour >= 8 and last.get("learn") != today:
                last["learn"] = today
                self.cur = self._start("learn")
            else:
                try:   # the Sunday long video stays on GitHub (its email has a download link; Telegram's limit is 50 MB)
                    from . import weekly
                    from .common import load_config
                    if os.environ.get("MAC_RUNS_WEEKLY") == "1" and weekly.due(load_config()) and \
                            last.get("weekly_try") != f"{today}:{lt.hour}":
                        last["weekly_try"] = f"{today}:{lt.hour}"
                        self.cur = self._start("weekly")
                except Exception as e:
                    log.warning("weekly check failed: %s", e)


def loop():
    load_env()
    if not tg.configured():
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in the .env file next to config.yaml")
    from . import relay
    st = load()
    nxt, nxt_relay, nxt_update = 0.0, 0.0, time.time() + UPDATE_EVERY
    jobs = None
    (DATA.parent / "logs").mkdir(exist_ok=True)
    if not st.get("started_once"):
        tg.send("🟢 Clip bot is running on the Mac. Text \"status\" any time.", silent=True)
        st["started_once"] = now_utc().isoformat()
        save(st)
    while True:
        try:
            for kind, data, mid, text in tg.poll(wait=50):
                handle(st, kind, data, mid, text)
                save(st)
        except Exception as e:
            log.warning("telegram poll failed: %s", e)
            time.sleep(30)
        now = time.time()
        if clipping_on():
            if jobs is None:
                jobs = Jobs()
            try:
                jobs.tick(st)
            except Exception as e:
                log.warning("job scheduler hiccup: %s", e)
            try:
                process_fixes(st, jobs)
            except Exception as e:
                log.warning("fix step failed: %s", e)
            try:
                process_joins(st)
            except Exception as e:
                log.warning("campaign join step failed: %s", e)
            try:
                if awake():
                    process_drafts(st)
                if time.time() >= st.get("hunt_next", 0):
                    st["hunt_next"] = time.time() + 120
                    hunt(st)
            except Exception as e:
                log.warning("campaign draft step failed: %s", e)
                tg.problem(st, "crflow", f"The campaign draft step hit an error: {str(e)[:200]}",
                           "It retries on its own. If this repeats tomorrow, send this message to Claude.")
            save(st)
        elif now >= nxt_relay:   # posting clips, live links, campaigns and problems from the bot on GitHub
            nxt_relay = now + RELAY_EVERY
            try:
                relay.run(st)
            except Exception as e:
                log.warning("relay round failed: %s", e)
            save(st)
        if awake():
            try:
                run_earnings(st)
            except Exception as e:
                log.warning("earnings report failed: %s", e)
        if now >= nxt and awake():
            run_check(st)
            save(st)
            nxt = time.time() + (EVERY_MIN + random.uniform(0, 8)) * 60
        if local_now().hour >= 3 and st.get("backup_day") != local_now().date().isoformat():   # nightly, 3 AM+
            st["backup_day"] = local_now().date().isoformat()
            try:
                run_backup(st)
            except Exception as e:
                log.warning("backup step failed: %s", e)
            save(st)
        if local_now().hour >= 10 and st.get("jf_day") != local_now().date().isoformat():   # once a day, 10 AM+
            st["jf_day"] = local_now().date().isoformat()
            run_cleanup(st)
            save(st)
        if now >= nxt_update and not (jobs and jobs.busy()):   # never restart in the middle of a clipping job
            nxt_update = now + UPDATE_EVERY
            try:
                if self_update():
                    save(st)
                    if jobs:
                        jobs.stop_live()
                    raise SystemExit(0)   # launchd starts the new version within a minute
            except SystemExit:
                raise
            except Exception as e:
                log.warning("update check failed: %s", e)


def once():
    """One check now, printed and sent (for testing): python -m clipper mac_once"""
    load_env()
    st = load()
    run_check(st, force=True)
    save(st)
    print(summary(st))


def cr_dryrun():
    """Practice run on the real account, cancelled before anything is sent: python -m clipper cr_dryrun
    Fills the "Submit link" form on the first approved draft and a draft form on the campaign, then cancels."""
    from . import contentrewards as cr, crflow
    from .common import load_config
    load_env()
    st = load()
    cfg = load_config()
    name = next((x["campaign"] for x in cfg.get("streamers") or [] if x.get("campaign") and x.get("preapproval")), None)
    url = _campaign_url(st, name)
    import tempfile
    from pathlib import Path
    test = Path(tempfile.gettempdir()) / "practice_clip.mp4"   # a 3-second blank vertical video, never uploaded
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(test)], check=False)
    clips = [test] if test.exists() else []
    with cr.Site() as s:
        cards = s.approved()
        print(f"Approved drafts on the page: {cards}", flush=True)
        if cards:
            s.submit_link(cards[0]["campaign"], cards[0]["duration"], "https://www.tiktok.com/@practice/video/1",
                          dry_run=True)
            print("OK: filled the Submit link form (link + requirements box) and cancelled it.", flush=True)
            labels = s.pick_labels(cards[0]["campaign"], cards[0]["duration"])
            print(f"Posts the form offers right now: {labels or 'none (nothing posted in the last 30 minutes)'}",
                  flush=True)
        else:
            print("No approved draft right now, so the link form wasn't tried.", flush=True)
        if url and clips:
            s.submit_draft(url, clips[-1], platforms=crflow.platforms(cfg), dry_run=True)
            print(f"OK: filled a draft form on {name} with a blank practice video and cancelled it.", flush=True)
        else:
            print(f"Skipped the draft form (campaign page known: {bool(url)}, practice video made: {bool(clips)}).",
                  flush=True)
    print("Practice run finished. Nothing was submitted.", flush=True)


def tg_test():
    load_env()
    if not tg.configured():
        raise SystemExit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing from .env")
    tg.send("👋 Test from the Mac: the clip bot can reach you.", rows=[[("Works", "test:ok")]])
    print("Sent. Check Telegram.")
