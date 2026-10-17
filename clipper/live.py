"""Live watcher: clip big moments while the stream is still going, and post the best ones right away.

Runs as its own GitHub Actions workflow (every 20 minutes, each run lasting up to ~5.8 hours while someone is live).
For each enabled Twitch streamer who is live on GTA it:
  1. reads chat anonymously and flags moments where chat suddenly explodes (laughing, "W", "NO WAY"...),
  2. polls the clips viewers are making on the live stream; several clips of the same moment is the strongest signal,
  3. a minute or two after the moment (so the payoff has happened and Twitch's in-progress VOD has caught up),
     cuts that stretch from the VOD, has Claude judge it like any other clip, renders it,
  4. posts it immediately if it scores high enough and today's limit allows, instead of waiting for a posting slot.
Everything it makes goes into data/live.json, which the other jobs fold into the main state.
"""
import os
import queue
import re
import socket
import ssl
import statistics
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

from . import fetch, highlights, jobs, twitch
from .common import log, load_config, load_state, save_state, run, WORK

LIVE_DEFAULTS = {
    "enabled": True,
    "chat_ratio": 3.0,          # chat must run this many times its recent normal rate...
    "chat_min_per_sec": 1.5,    # ...and at least this many weighted messages a second
    "min_viewer_clips": 2,      # this many new viewer clips of the same moment also counts as big
    "cooldown_minutes": 8,      # at most one live clip per streamer this often
    "max_per_stream": 6,        # live clips per stream (the full VOD run catches the rest later)
    "post_now_score": 8,        # live clips scoring this or more post right away
    "extra_posts_per_day": 1,   # live posts allowed on top of the normal daily ramp
    "vod_lag_seconds": 75,      # how far Twitch's in-progress VOD trails the live stream
    "max_minutes": 345,         # one watcher run; it then starts the next one itself (Heartbeat.hand_over)
}
CLIP_POLL_SECONDS = 90
TEST_MINUTES = 10


def _settings(cfg):
    return dict(LIVE_DEFAULTS, **(cfg.get("live") or {}))


# ------------------------------------------------------------------ is anyone live?
UNKNOWN = "unknown"   # Twitch didn't give a clear answer this time; keep doing whatever we were doing


def live_info(login):
    """{game, title, vod_id, vod_created} if live with a recording VOD, None if not, UNKNOWN if Twitch hiccuped."""
    q = ('query { user(login: "%s") { stream { id createdAt title game { name } archiveVideo { id createdAt status } } } }'
         % login.lower())
    data = twitch._gql({"query": q})
    user = (((data or {}).get("data") or {}).get("user")) if data else None
    if user is None and data and "archiveVideo" in str(data.get("errors")):   # field not available: plain query
        data = twitch._gql({"query": 'query { user(login: "%s") { stream { id createdAt title game { name } } } }'
                                     % login.lower()})
        user = (((data or {}).get("data") or {}).get("user")) if data else None
    if user is None:
        return UNKNOWN
    st = user.get("stream")
    if not st:
        return None if not (data or {}).get("errors") else UNKNOWN
    v = st.get("archiveVideo") or {}
    if not v.get("id"):   # older way: the newest past broadcast, if it's the one still recording
        q2 = ('query { user(login: "%s") { videos(first: 1, type: ARCHIVE, sort: TIME) '
              '{ edges { node { id status createdAt } } } } }' % login.lower())
        d2 = twitch._gql({"query": q2})
        edges = (((((d2 or {}).get("data") or {}).get("user") or {}).get("videos") or {}).get("edges")) or []
        if not edges:
            return UNKNOWN if (not d2 or d2.get("errors")) else None
        v = edges[0].get("node") or {}
        if v.get("status") != "RECORDING":
            log.info("%s is live but has no recording VOD (VODs off?); can't clip it live", login)
            return None
    return {"game": (st.get("game") or {}).get("name") or "", "title": st.get("title") or "",
            "vod_id": v["id"], "vod_created": _ts(v.get("createdAt") or st.get("createdAt"))}


def top_gta_channel(game="Grand Theft Auto V", n=15):
    """Most-watched channel live on GTA right now that has a recording VOD (for live:auto tests)."""
    q = ('query { game(name: "%s") { streams(first: %d) { edges { node { viewersCount broadcaster { login '
         'videos(first: 1, type: ARCHIVE, sort: TIME) { edges { node { status } } } } } } } } }' % (game, n))
    edges = (((((twitch._gql({"query": q}) or {}).get("data") or {}).get("game") or {}).get("streams") or {}).get("edges")) or []
    for e in edges:
        b = (e.get("node") or {}).get("broadcaster") or {}
        vids = ((b.get("videos") or {}).get("edges") or [])
        if vids and (vids[0].get("node") or {}).get("status") == "RECORDING":
            return b["login"]
    return None


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ------------------------------------------------------------------ chat
class Chat(threading.Thread):
    """Anonymous read-only Twitch chat. msgs holds (unix_time, text)."""

    def __init__(self, login):
        super().__init__(daemon=True)
        self.login = login.lower()
        self.msgs = deque(maxlen=300000)
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                self._session()
            except Exception as e:
                log.info("chat %s reconnecting: %s", self.login, e)
                time.sleep(5)

    def _session(self):
        raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=300)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
        nick = f"justinfan{int(time.time()) % 100000}"
        sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.login}\r\n".encode())
        buf = b""
        while not self.stop:
            data = sock.recv(65536)
            if not data:
                raise ConnectionError("closed")
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for line in lines:
                text = line.decode("utf-8", "replace")
                if text.startswith("PING"):
                    sock.sendall(text.replace("PING", "PONG", 1).encode() + b"\r\n")
                    continue
                m = re.search(r" PRIVMSG #\S+ :(.*)$", text)
                if m:
                    self.msgs.append((time.time(), m.group(1)))


def _weight(text):
    if twitch.RAID.search(text):
        return 0
    return 2 if twitch.LAUGH.search(text) else 1


def chat_spike(msgs, now, ratio=3.0, min_rate=1.5, window=15, history=600, level_only=False):
    """How many times above normal chat is right now (None if it isn't spiking or there's too little history).
    level_only: return the ratio even when it isn't a spike (tests use it to find the liveliest moment)."""
    recent = sum(_weight(t) for ts, t in msgs if now - window <= ts <= now) / window
    older = [(ts, t) for ts, t in msgs if now - history <= ts < now - window]
    if not older or now - older[0][0] < 180:
        return None
    buckets = {}
    for ts, t in older:
        buckets.setdefault(int(ts // window), 0)
        buckets[int(ts // window)] += _weight(t)
    span = int((now - window - older[0][0]) // window) + 1
    rates = [buckets.get(int(older[0][0] // window) + i, 0) / window for i in range(span)]
    base = max(statistics.median(rates), 0.2)
    if level_only:
        return recent / base if recent > 0 else None
    if recent >= ratio * base and recent >= min_rate:
        return recent / base
    return None


def viewer_clip_cluster(clips, min_count=2, span=90):
    """Given new viewer clips [{offset, duration}], the time of the moment several of them captured, or None."""
    pts = sorted(c["offset"] + 0.7 * c["duration"] for c in clips)
    best = None
    for i in range(len(pts)):
        group = [p for p in pts[i:] if p - pts[i] <= span]
        if len(group) >= min_count and (best is None or len(group) > best[1]):
            best = (statistics.median(group), len(group))
    return best


# ------------------------------------------------------------------ cutting from the in-progress VOD
def _duration(path):
    try:
        return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())
    except Exception:
        return 0.0


def cut_live(vod, a, b, dest):
    """[a, b] seconds of a VOD that is still recording, as mp4. yt-dlp first; if it balks at a live VOD, ffmpeg on
    the playlist directly."""
    try:
        fetch.download_section(vod, a, b, dest)
        if dest.exists() and _duration(dest) >= 0.8 * (b - a):
            return dest
    except Exception as e:
        log.info("yt-dlp section of live VOD failed (%s); using the playlist directly", e)
    url = run(["yt-dlp", "-g", "-f", "best[height<=1080]/best", vod["url"]]).strip().splitlines()[0]
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(int(a)), "-i", url, "-t", str(int(b - a)),
         "-c", "copy", "-movflags", "+faststart", dest])
    if not dest.exists() or _duration(dest) < 0.8 * (b - a):
        raise RuntimeError(f"couldn't cut {a}-{b}s from the live VOD (got {_duration(dest):.0f}s)")
    return dest


# ------------------------------------------------------------------ one moment -> one clip
def make_live_clip(cfg, state, vod, t, label, chat_msgs, client=None):
    """Cut, judge, render one flagged moment (t = seconds into the VOD). Returns the clip dict or None."""
    a = max(0, int(t - highlights.WINDOW_BEFORE - 10))
    b = int(t + highlights.WINDOW_AFTER + 10)
    d = WORK / f"live_{vod['id'].split(':')[-1]}_{a}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        src = cut_live(vod, a, b, d / "moment_src.mp4")
        wav = d / "moment.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
        words = [dict(w, s=w["s"] + a, e=w["e"] + a)
                 for w in highlights.transcribe(wav, cfg["models"]["whisper"], start=0.0, end=b - a)]
        offs = vod["created_ts"]
        chat = [(ts - offs - twitch.CHAT_DELAY, txt) for ts, txt in chat_msgs]
        window = {"id": 0, "start": a, "end": b, "signals": [label],
                  "chat": twitch.chat_sample(chat, a, b), "spike": 0.0,
                  "text": highlights.text_with_marks(words, a, b)}
        judged = highlights.judge(cfg, vod, [window], client, max_picks=1)
        picks = highlights.keepers(cfg, judged, keep_all=jobs.DRY_RUN)
        picks = highlights.edit_hooks(cfg, picks, [window], client) if picks else []
        if judged and not picks:
            print(f"::notice::live moment at {t / 3600:.2f}h ({label}): score {judged[0]['score']}/10, below the bar",
                  flush=True)
            return None
        if not picks:
            print(f"::notice::live moment at {t / 3600:.2f}h ({label}): Claude found nothing clip-worthy", flush=True)
            if not jobs.DRY_RUN:
                return None
            # test run: render it anyway so the cutting and rendering get checked
            picks = [{"start": max(a, t - 25), "end": min(b, t + 10), "score": 0, "hook": "TEST: nothing clip-worthy here",
                      "punch_text": "", "punch_at": 0, "title": "test", "caption": "test", "tags": [], "why": "test render",
                      "commentary": []}]
        p = picks[0]
        print(f"::notice::live moment at {t / 3600:.2f}h ({label}): score {p['score']}/10 | {p['hook']}", flush=True)
        if int(p["score"]) < cfg["clip"]["min_score"] and not jobs.DRY_RUN:
            return None
        if jobs.overlaps_existing(state, vod["id"], p["start"], p["end"]):
            return None
        clip = jobs.make_clip(cfg, state, vod, p, words, d, src=src, src_offset=p["start"] - a)
        clip["live"] = True
        clip["live_signal"] = label
        save_state(state)
        return clip
    finally:
        if jobs.DRY_RUN:
            jobs.export_test_clips(d)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def maybe_post_now(cfg, state, clip):
    s = _settings(cfg)
    from . import clipqueue
    if clipqueue.email_mode(cfg):
        # A big live moment is emailed right away (being first matters), within the daily limit plus the extra.
        if jobs.DRY_RUN or clip["status"] != "approved" or clip["score"] < s["post_now_score"] or clip.get("emailed_at"):
            return
        if (state.get("email") or {}).get("paused"):
            return   # you paused the posting emails
        if jobs.days_since_start(cfg) < 0 and not jobs.FORCE:
            return
        today = jobs.now_local(cfg).date().isoformat()
        if clipqueue.emailed_on(state, today, cfg.get("timezone", "UTC")) >= jobs._quota(cfg) + s["extra_posts_per_day"]:
            log.info("daily email limit reached; the live clip waits for the next slot")
            return
        jobs.send_package(cfg, state, clip)
        save_state(state)
        return
    if jobs.DRY_RUN or clip["status"] != "approved" or clip["score"] < s["post_now_score"]:
        return
    if jobs.days_since_start(cfg) < 0 and not jobs.FORCE:
        return
    limit = jobs._quota(cfg) + s["extra_posts_per_day"]
    for platform in cfg["posting"]["platforms"]:
        if platform in clip["posts"]:
            continue
        if jobs.posted_today(cfg, state, platform) >= limit:
            log.info("%s: daily limit reached; live clip waits for tomorrow's slots", platform)
            continue
        jobs.post_clip(cfg, state, clip, platform)


# ------------------------------------------------------------------ heartbeat
class Heartbeat:
    """GitHub skips or delays many scheduled runs, so this always-running job starts the others itself: the posting
    check every 15 minutes, the stream check every hour, the daily campaign scan and the Monday report when they're
    overdue. Before GitHub's 6-hour limit it starts the next copy of itself. The cron schedules stay as a backup."""

    POST_EVERY, PROCESS_EVERY = 15 * 60, 60 * 60

    def __init__(self, cfg):
        self.cfg = cfg
        now = time.time()
        self.next_post, self.next_process, self.next_daily = now + 60, now + 300, now + 600
        self.started = {}   # job -> when this heartbeat started it (the state file here may lag behind)

    def dispatch(self, workflow, job=None):
        if os.environ.get("GITHUB_ACTIONS") != "true" or jobs.DRY_RUN:
            return False
        import requests
        body = {"ref": os.environ.get("GITHUB_REF_NAME") or "main"}
        if job:
            body["inputs"] = {"job": job}
        try:
            r = requests.post(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/"
                              f"{workflow}/dispatches", json=body, timeout=30,
                              headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                       "Accept": "application/vnd.github+json"})
            if r.status_code >= 300:
                log.warning("couldn't start %s %s: %s %s", workflow, job or "", r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:
            log.warning("couldn't start %s %s: %s", workflow, job or "", e)
            return False

    def tick(self, now):
        if now >= self.next_post:
            self.next_post = now + self.POST_EVERY
            self.dispatch("clipper.yml", "post")
        if now >= self.next_process:
            self.next_process = now + self.PROCESS_EVERY
            self.dispatch("clipper.yml", "process")
        if now >= self.next_daily:
            self.next_daily = now + 3600
            self._catch_up()

    def _catch_up(self):
        """Run the daily campaign scan / Monday report if their scheduled runs were skipped."""
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        st = load_state()
        last = (st.get("last_run") or {})
        now = datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo(self.cfg.get("timezone", "UTC")))
        def stale(job, gap):
            seen = [datetime.fromisoformat(t) for t in (last.get(job), self.started.get(job)) if t]
            return not seen or now - max(seen) > gap
        if local.hour >= 7 and stale("analytics", timedelta(hours=20)):
            self.started["analytics"] = now.isoformat()
            self.dispatch("clipper.yml", "analytics")
        if local.weekday() == 0 and local.hour >= 8 and stale("learn", timedelta(days=6)):
            self.started["learn"] = now.isoformat()
            self.dispatch("clipper.yml", "learn")

    def hand_over(self):
        """Start the next heartbeat (it queues behind this run and takes over when this one ends)."""
        if self.dispatch("live.yml"):
            log.info("next heartbeat started")


# ------------------------------------------------------------------ syncing with the other jobs
def git_sync(message):
    """Commit data/live.json and pull the other jobs' latest state (posts made today, etc.)."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or jobs.DRY_RUN:
        return
    def g(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)
    g("config", "user.name", "clipper-bot")
    g("config", "user.email", "clipper-bot@users.noreply.github.com")
    g("add", "data/live.json")
    if g("diff", "--cached", "--quiet").returncode != 0:
        g("commit", "-qm", message)
    for _ in range(3):
        if g("pull", "--rebase", "--autostash", "-q").returncode == 0 and g("push", "-q").returncode == 0:
            return
        time.sleep(5)
    log.warning("couldn't push live state this time; will retry on the next sync")


# ------------------------------------------------------------------ the loop
def watch():
    os.environ["CLIPPER_LIVE"] = "1"
    cfg = load_config()
    s = _settings(cfg)
    test = jobs.TEST_SOURCE.startswith("live:")
    if test:
        ch = jobs.TEST_SOURCE.split(":", 1)[1]
        if ch == "auto":
            ch = top_gta_channel()
            if not ch:
                print("::warning::nobody with a VOD is live on GTA right now; nothing to test", flush=True)
                return
            print(f"::notice::testing on {ch}, the most-watched GTA stream right now", flush=True)
        streamers = [{"name": ch, "platform": "twitch", "channel": ch}]
        minutes = TEST_MINUTES
    else:
        if not s["enabled"] or not jobs._posting_ready(cfg):
            return
        streamers = [x for x in cfg.get("streamers", []) if x.get("enabled", True) and x["platform"] == "twitch"]
        minutes = s["max_minutes"]
    game_re = (cfg["fetch"].get("game_match") or "grand theft auto|gta")
    deadline = time.time() + minutes * 60
    watching = {}     # login -> {vod, chat, seen_clips, last_t, made, best}
    ended = {}        # streams that stopped while moments were still waiting to be cut
    pending = queue.Queue()   # (ready_at_wall, login, t_offset, label); a worker cuts them so watching never pauses
    busy = threading.Event()
    worker = threading.Thread(target=_worker, args=(cfg, pending, watching, busy, ended), daemon=True)
    worker.start()
    next_check = 0
    test_give_up = time.time() + 300   # a test keeps asking Twitch for 5 minutes before deciding nobody's live
    next_pull = time.time() + 600
    allowed = True
    beat = None if test else Heartbeat(cfg)
    while time.time() < deadline:
        now = time.time()
        if beat:
            beat.tick(now)
        if now >= next_check:   # who's live (every 2 minutes), and is there room in the queue / budget
            next_check = now + 120
            if now >= next_pull:
                next_pull = now + 600
                git_sync("live: sync")   # pick up posts, expiries and spend from the other jobs
            was = allowed
            allowed = test or jobs.claude_allowed(cfg, load_state(), "; still watching chat for free", quiet=not was)
            for st in streamers:
                login = st["channel"]
                info = live_info(login)
                if info == UNKNOWN:
                    continue   # a Twitch hiccup isn't a reason to stop (or start) watching
                on_gta = info and (test or re.search(game_re, info["game"], re.I))
                if login in watching:
                    w = watching[login]
                    w["misses"] = 0 if on_gta else w.get("misses", 0) + 1
                    if not on_gta and w["misses"] < 2:
                        continue   # only stop after two clear "not live on GTA" answers in a row
                if on_gta and login not in watching:
                    vod = {"id": f"twitch:v{info['vod_id']}", "url": f"https://www.twitch.tv/videos/{info['vod_id']}",
                           "title": info["title"], "game": info["game"], "streamer": st, "created_ts": info["vod_created"]}
                    chat = Chat(login)
                    chat.start()
                    watching[login] = {"vod": vod, "chat": chat, "seen": None, "last_t": -1e9, "made": 0,
                                       "best": None, "next_clips": 0}
                    print(f"::notice::watching {st['name']} live: {info['title'][:80]} ({info['game']})", flush=True)
                elif not on_gta and login in watching:
                    gone = watching.pop(login)
                    gone["chat"].stop = True
                    ended[login] = gone   # moments already flagged on this stream still get cut
                    log.info("%s is no longer live on GTA", login)
            if not watching and not test:
                log.info("nobody is live on GTA right now")
            if not watching and test and pending.empty() and not busy.is_set() and time.time() > test_give_up:
                print(f"::warning::{streamers[0]['channel']} isn't live on GTA right now; nothing to test", flush=True)
                return
        for login, w in watching.items():
            vod = w["vod"]
            offset_now = now - vod["created_ts"]
            msgs = list(w["chat"].msgs)
            # 1. chat explosion
            r = chat_spike(msgs, now, s["chat_ratio"], s["chat_min_per_sec"])
            if test:
                lvl = chat_spike(msgs, now, level_only=True)
                if lvl and (w["best"] is None or lvl > w["best"][0]):
                    w["best"] = (lvl, offset_now - 7 - twitch.CHAT_DELAY)
            if r:
                t = offset_now - 7 - twitch.CHAT_DELAY
                if w["best"] is None or r > w["best"][0]:
                    w["best"] = (r, t)
                _flag(w, pending, login, t, f"chat exploded live ({r:.1f}x normal)", s, now, allowed)
            # 2. viewers clipping the same moment
            if now >= w["next_clips"]:
                w["next_clips"] = now + CLIP_POLL_SECONDS
                try:
                    clips = twitch.viewer_clips(login, twitch.video_id(vod), periods=("LAST_DAY",))
                except Exception as e:
                    clips = []
                    log.info("viewer clips poll failed: %s", e)
                keys = {(round(c["offset"]), c["title"]) for c in clips}
                if w["seen"] is None:
                    w["seen"] = keys     # ignore clips from before the watcher started
                else:
                    new = [c for c in clips if (round(c["offset"]), c["title"]) not in w["seen"]
                           and c["offset"] > offset_now - 900]
                    w["seen"] |= keys
                    hit = viewer_clip_cluster(new, s["min_viewer_clips"])
                    if hit:
                        _flag(w, pending, login, hit[0], f"{hit[1]} viewers clipped this live", s, now, allowed)
        if test and time.time() > deadline - 300:
            for login, w in watching.items():   # test: no big moment yet, so take the liveliest one seen
                if w["made"] == 0 and w["best"]:
                    _flag(w, pending, login, w["best"][1], f"liveliest chat moment ({w['best'][0]:.1f}x)", s, now)
        time.sleep(5)
    # let moments already flagged finish (up to 8 minutes), then stop
    end = time.time() + 480
    while (not pending.empty() or busy.is_set()) and time.time() < end:
        time.sleep(5)
    for w in watching.values():
        w["chat"].stop = True
    if beat:
        beat.hand_over()
    if test:
        _test_summary()


def _worker(cfg, pending, watching, busy, ended=None):
    """3. Cut each flagged moment once its payoff should be in the VOD."""
    while True:
        ready, login, t, label = pending.get()
        busy.set()
        try:
            time.sleep(max(0, ready - time.time()))
            w = watching.get(login) or (ended or {}).get(login)
            if not w:
                continue
            state = load_state()
            if not jobs.claude_allowed(cfg, state, "; skipping a flagged moment"):
                continue
            clip = make_live_clip(cfg, state, w["vod"], t, label, list(w["chat"].msgs))
            if clip:
                maybe_post_now(cfg, state, clip)
                git_sync(f"live clip: {clip['hook'][:60]}")
        except Exception as e:
            log.exception("live clip at %s failed: %s", t, e)
            print(f"::warning::live clip failed: {str(e)[:200]}", flush=True)
        finally:
            busy.clear()


def _flag(w, pending, login, t, label, s, now, allowed=True):
    if not allowed or w["made"] >= s["max_per_stream"] or t - w["last_t"] < s["cooldown_minutes"] * 60:
        return
    w["last_t"] = t
    w["made"] += 1
    ready = w["vod"]["created_ts"] + t + highlights.WINDOW_AFTER + 10 + s["vod_lag_seconds"]
    pending.put((max(ready, now), login, t, label))
    print(f"::notice::{login}: big moment at {t / 3600:.2f}h ({label}); cutting it in "
          f"{max(0, ready - now):.0f}s", flush=True)


def _test_summary():
    import json
    out = WORK.parent / "test-output"
    out.mkdir(exist_ok=True)
    clips = [c for c in load_state()["clips"].values() if c.get("live")]
    (out / "summary.json").write_text(json.dumps({"source": jobs.TEST_SOURCE, "title": "live test", "clips": [
        {k: c.get(k) for k in ("score", "hook", "hook_score", "hook_options", "punch_text", "punch_at", "title",
                               "caption", "duration", "why", "status", "facecam", "layout", "framing", "said", "file",
                               "start", "live_signal")} for c in clips]}, indent=2))
