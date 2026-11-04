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
from datetime import datetime

import yaml

from . import tg
from .common import DATA, ROOT, log, now_utc

STATE_FILE = DATA / "mac.json"
TZ = "America/Los_Angeles"
AWAKE = (8, 23)          # local hours the browser checks may run in
EVERY_MIN = 30           # minutes between Content Rewards checks (plus a random few)


def load_env(path=None):
    """KEY=value lines from .env into the environment (existing values win)."""
    p = path or (ROOT / ".env")
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
        if rec["approved"] and not rec["ended"] and not any(same_campaign(name, p) for p in prioritized):
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
        msgs.append(f"✂️ A draft{(' for ' + n['campaign']) if n.get('campaign') else ''} needs changes.\n\n"
                    f"Reviewer ({n.get('who')}, {n.get('when')}): {n.get('note')}\n\n"
                    "Fix the clip and use Upload revision on Content Rewards (Drafts > Needs changes).")
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
        lines.append(f"Last check: {st['last_sync'][:16].replace('T', ' ')} UTC")
    return "\n".join(lines)


# ------------------------------------------------------------- one check, the loop, and problems
FIX_LOGIN = ("1. On the Mac, open Terminal.\n"
             "2. Paste: cd ~/projects/gta-clipper && .venv/bin/python -m clipper cr_login\n"
             "3. Log in to Content Rewards in the Chrome window that opens, then close it.")
FIX_CHROME = ("1. Make sure Google Chrome is in the Mac's Applications folder and opens normally.\n"
              "2. In Terminal: cd ~/projects/gta-clipper && .venv/bin/python -m playwright install-deps 2>/dev/null; "
              ".venv/bin/python -m clipper mac_once")


def check(st):
    from . import contentrewards as cr
    with cr.Site() as s:
        camps = s.campaigns()
        drafts = s.drafts()
    msgs = compare(st, camps, drafts, bot_campaigns())
    st["last_sync"] = now_utc().isoformat()
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
        tg.send(m)
        time.sleep(1)
    if force and not msgs:
        tg.send("Checked Content Rewards: nothing new.\n\n" + summary(st))


def handle(st, kind, data, message_id, text):
    low = (data or "").lower()
    if kind == "text":
        if low in ("status", "/status"):
            tg.send(summary(st))
        elif low in ("check", "/check"):
            tg.send("Checking Content Rewards now…", silent=True)
            run_check(st, force=True)
        elif low in ("/start", "start", "help", "/help"):
            tg.send("I'm the Wanted Level clip bot. I'll message you about campaigns, your bio, drafts and problems.\n"
                    "Text \"status\" for a summary or \"check\" to look at Content Rewards right now.")


def loop():
    load_env()
    if not tg.configured():
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in the .env file next to config.yaml")
    st = load()
    nxt = 0.0
    tg.send("🟢 Clip bot is running on the Mac. Text \"status\" any time.", silent=True)
    while True:
        try:
            for kind, data, mid, text in tg.poll(wait=50):
                handle(st, kind, data, mid, text)
                save(st)
        except Exception as e:
            log.warning("telegram poll failed: %s", e)
            time.sleep(30)
        if time.time() >= nxt and awake():
            run_check(st)
            save(st)
            nxt = time.time() + (EVERY_MIN + random.uniform(0, 8)) * 60


def once():
    """One check now, printed and sent (for testing): python -m clipper mac_once"""
    load_env()
    st = load()
    run_check(st, force=True)
    save(st)
    print(summary(st))


def tg_test():
    load_env()
    if not tg.configured():
        raise SystemExit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing from .env")
    tg.send("👋 Test from the Mac: the clip bot can reach you.", rows=[[("Works", "test:ok")]])
    print("Sent. Check Telegram.")
