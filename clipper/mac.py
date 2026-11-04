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
    tools = os.path.expanduser("~/.clipper/bin")   # ffmpeg for the clipping jobs (mac/enable-clipping.sh)
    if os.path.isdir(tools) and tools not in os.environ.get("PATH", ""):
        os.environ["PATH"] = tools + os.pathsep + os.environ.get("PATH", "")


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
        lines.append(f"Last Content Rewards check: {st['last_sync'][:16].replace('T', ' ')} UTC")
    ok = (st.get("relay") or {}).get("ok")
    lines.append(f"Watching the GitHub bot: {'yes, last read ' + ok[11:16] + ' UTC' if ok else 'not yet'}")
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
    if kind == "tap" and data.startswith("jf:keep:"):
        from . import mediaclean
        try:
            mediaclean.keep(data.split(":", 2)[2])
            title = (text or "").split(" gets deleted tomorrow")[0].lstrip("🗑 ").strip() or "It"
            tg.edit(message_id, f"❤️ Kept: {title}. It's a favorite in Jellyfin now, so it won't be deleted.")
        except Exception as e:
            tg.send(f"Couldn't mark it as kept ({str(e)[:150]}). In the Jellyfin app, tap the heart on it instead.")
        return
    if kind == "tap" and data.startswith("r:"):
        from . import mail
        _, verdict, token = (data.split(":", 2) + ["", ""])[:3]
        mail.queue_reply(f"tg:{token}", verdict)
        done = {"posted": "✅ Marked posted", "skip": "⏭ Skipped (a replacement comes at the next slot)",
                "next": "➕ Another clip is on its way", "join": "✅ Joined: its clips go first now. Apply with the "
                "link above if you haven't yet; I'll tell you when they approve you.", "skip_c": ""}.get(verdict, "Got it")
        tg.edit(message_id, f"{(text or '')[:3500]}\n\n{done}")
        return
    if kind == "text":
        if clipping_on() and low not in ("status", "/status", "check", "/check", "cleanup", "/cleanup",
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
        elif low in ("cleanup", "/cleanup"):
            st.pop("jf_key_asked", None)
            run_cleanup(st)
            tg.send("Clean-up done.", silent=True)
        elif low in ("/start", "start", "help", "/help"):
            tg.send("I'm the Wanted Level clip bot. I'll message you about campaigns, your bio, drafts and problems.\n"
                    "Text \"status\" for a summary or \"check\" to look at Content Rewards right now.")


FIX_JF_KEY = ("1. On the Mac, open http://localhost:8096 and log in.\n"
              "2. Click the menu (☰), then Dashboard, then API Keys, then the + button. Name it clipbot.\n"
              "3. Copy the key. In Terminal paste: open -e ~/projects/gta-clipper/.env\n"
              "4. Add a new line: JELLYFIN_API_KEY=the-key  then save. The bot picks it up within a day "
              "(or text it \"cleanup\" after restarting it).")


def run_cleanup(st):
    """Delete titles watched 3+ days ago (warn the day before). Problems come back as one message a day."""
    from . import mediaclean
    load_env()
    if not mediaclean.configured():
        if not st.get("jf_key_asked"):
            st["jf_key_asked"] = now_utc().isoformat()
            tg.send("🧹 To turn on auto-delete for watched movies and shows, Jellyfin needs to give the bot a key:\n\n"
                    + FIX_JF_KEY)
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
                try:
                    from . import weekly
                    from .common import load_config
                    if weekly.due(load_config()) and last.get("weekly_try") != f"{today}:{lt.hour}":
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
        elif now >= nxt_relay:   # posting clips, live links, campaigns and problems from the bot on GitHub
            nxt_relay = now + RELAY_EVERY
            try:
                relay.run(st)
            except Exception as e:
                log.warning("relay round failed: %s", e)
            save(st)
        if now >= nxt and awake():
            run_check(st)
            save(st)
            nxt = time.time() + (EVERY_MIN + random.uniform(0, 8)) * 60
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


def tg_test():
    load_env()
    if not tg.configured():
        raise SystemExit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing from .env")
    tg.send("👋 Test from the Mac: the clip bot can reach you.", rows=[[("Works", "test:ok")]])
    print("Sent. Check Telegram.")
