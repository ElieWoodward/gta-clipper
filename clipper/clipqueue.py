"""The posting queue (email mode): a few ready clips, an email at each posting slot, and spotting what got posted.

A clip is in the queue while its status is "approved". It leaves when it's seen on TikTok ("posted"), when Elie
replies "posted"/"skip" to its email, or when it's older than queue.max_age_hours ("expired").
"""
import difflib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from .common import log, now_utc, run

QUEUE_DEFAULTS = {"max_size": 5, "max_age_hours": 48}
REPLY_CHECK_MINUTES = 55   # Gmail is read about once an hour: logging in every 15 minutes gets an account flagged
MATCH_THRESHOLD = 0.6


def settings(cfg):
    return dict(QUEUE_DEFAULTS, **(cfg.get("queue") or {}))


def email_mode(cfg):
    return (cfg.get("posting") or {}).get("mode", "blotato") == "email"


def _created(c):
    return datetime.fromisoformat(c["created"])


def tiktok_paused(cfg):
    """The local date TikTok posting resumes (posting.tiktok_paused_until), while it's still ahead; else None."""
    from zoneinfo import ZoneInfo
    until = str((cfg.get("posting") or {}).get("tiktok_paused_until") or "")
    today = now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC"))).date().isoformat()
    return until if until and today < until else None


def dead_on_tiktok(state, last=3, min_views=10, min_hours=6):
    """Streamers whose last few TikTok posts all stalled (under min_views after min_hours). TikTok quietly stops
    showing clips of moments many other accounts already posted, which happens most with the biggest streamers."""
    by = {}
    now = now_utc()
    for c in state["clips"].values():
        p = (c.get("posts") or {}).get("tiktok")
        if not p or not p.get("posted_at") or p.get("views") is None:
            continue
        if now - datetime.fromisoformat(p["posted_at"]) < timedelta(hours=min_hours):
            continue
        by.setdefault((c.get("streamer") or "").lower(), []).append((p["posted_at"], int(p["views"])))
    out = set()
    for name, posts in by.items():
        recent = [v for _, v in sorted(posts)[-last:]]
        if len(recent) >= last and max(recent) < min_views:
            out.add(name)
    return out


def queue(state):
    """Clips waiting to be posted, best first. A clip from a paid campaign you joined counts one point higher; one
    from a streamer whose recent TikTok posts all stalled counts two points lower (it still goes out if it's all
    there is)."""
    paid = {(r.get("twitch_login") or "").lower() for r in (state.get("campaigns") or {}).values()
            if r.get("status") == "joined"}
    dead = set() if state.get("tiktok_paused") else dead_on_tiktok(state)   # YouTube-only days: no TikTok penalty
    q = [c for c in state["clips"].values() if c.get("status") == "approved"]
    def is_paid(c):
        return (c.get("streamer") or "").lower() in paid
    def rank(c):
        name = (c.get("streamer") or "").lower()
        return c["score"] + (1 if is_paid(c) else 0) - (2 if name in dead else 0)
    return sorted(q, key=lambda c: (rank(c), is_paid(c), bool(c.get("facecam")), c["created"]), reverse=True)


def room(cfg, state):
    return settings(cfg)["max_size"] - len(queue(state))


def expire(cfg, state, now=None):
    """Mark clips older than max_age_hours as expired. Returns the clips that just expired."""
    now = now or now_utc()
    cutoff = now - timedelta(hours=settings(cfg)["max_age_hours"])
    gone = []
    for c in queue(state):
        if _created(c) < cutoff:
            c["status"] = "expired"
            gone.append(c)
    return gone


def last_emailed(state):
    """ISO time of the most recent posting email (from the clips themselves, so every job agrees)."""
    times = [c["emailed_at"] for c in state["clips"].values() if c.get("emailed_at")]
    return max(times) if times else None


def emailed_on(state, day, tz):
    """How many posting emails went out on a local date. Skipped clips don't count, so a skip gets replaced."""
    from zoneinfo import ZoneInfo
    n = 0
    for c in state["clips"].values():
        if c.get("emailed_at") and c.get("status") != "skipped" and not c.get("extra"):
            if datetime.fromisoformat(c["emailed_at"]).astimezone(ZoneInfo(tz)).date().isoformat() == day:
                n += 1
    return n


def pick_for_email(state):
    """Best clip made since the last email; else the best clip still waiting. Never one already emailed."""
    waiting = [c for c in queue(state) if not c.get("emailed_at")]
    if not waiting:
        return None
    last = last_emailed(state)
    if last:
        fresh = [c for c in waiting if c["created"] > last]
        if fresh:
            return fresh[0]
    return waiting[0]


# ------------------------------------------------------------------ the package
def streamer_channel(cfg, name):
    for s in cfg.get("streamers", []):
        if s.get("name") == name:
            return s.get("channel") or name.lower()
    return name.lower()


def _ts(seconds):
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60}m{s % 60}s"


def package(cfg, clip, queue_now, state=None):
    """Everything to paste into TikTok and YouTube for one clip: {subject, blocks: [(label, text)]}."""
    from . import jobs
    tiktok_caption = jobs._caption(cfg, clip, "tiktok")
    channel = streamer_channel(cfg, clip["streamer"])
    title = clip["title"].strip()
    yt_title = (title if "#shorts" in title.lower() else f"{title} #shorts")[:100]
    hashtags = " ".join(dict.fromkeys(list(cfg["posting"]["hashtags"]) + ["#" + re.sub(r"[^a-z0-9]", "", channel)]))
    yt_desc = (f"{clip['caption']}\n\nStreamer: {clip['streamer']} (https://twitch.tv/{channel})\n\n{hashtags} #shorts")
    source = clip.get("vod_url") or ""
    if source and "twitch.tv/videos/" in source:
        source = f"{source}?t={_ts(clip['start'])}"
    waiting = "\n".join(f"- {c['hook']} ({c['score']}/10, {int((now_utc() - _created(c)).total_seconds() // 3600)}h old)"
                        for c in queue_now if c["id"] != clip["id"]) or "- nothing else right now"
    from . import campaigns
    rec = campaigns.campaign_for(state, clip["streamer"]) if state is not None else None
    blocks = [
        ("TikTok caption", tiktok_caption),
        ("YouTube title", yt_title),
        ("YouTube description", yt_desc),
        ("What's in it", f"{clip.get('why') or clip['hook']}\nScore {clip['score']}/10, {clip['duration']:.0f} seconds, "
                         f"on-screen hook: {clip['hook']}"),
        ("Source", source or "(manual upload)"),
        ("Still in the queue", waiting),
    ]
    paused = tiktok_paused(cfg)
    if paused:
        blocks = [b for b in blocks if b[0] != "TikTok caption"]
        back = datetime.fromisoformat(paused).strftime("%A %b %-d")
        blocks.insert(0, ("YouTube only", f"TikTok is taking a few days off, so post this one on YouTube only. "
                          f"TikTok comes back {back}, and the emails will have the TikTok caption again."))
    if rec and rec.get("status") == "joined":
        blocks.insert(0, ("Paid campaign", f"{rec['name']} ({rec.get('rate') or '?'} per 1K views).\nFollow the brief's "
                          f"caption/hashtag rules (on the board), post, then submit your link at "
                          f"{rec.get('join_url') or rec['url']}. You'll also get a Claim email with the exact link once "
                          "the post is seen."))
    elif rec:
        blocks.insert(0, ("Paid campaign (not joined yet)", f"{rec['name']} pays {rec.get('rate') or '?'} per 1K views "
                          f"for clips of this streamer. Join at {rec.get('join_url') or rec['url']} BEFORE posting, then "
                          "follow the brief's caption rules and submit your link to get paid for this one."))
    return {"subject": f"Post now{' (YouTube)' if paused else ''}: {clip['hook']} ({clip['score']}/10)", "blocks": blocks,
            "tiktok_caption": tiktok_caption, "yt_title": yt_title}


def render_email(pkg, download_url=None, attached=True):
    lines = []
    for label, text in pkg["blocks"]:
        lines += [f"=== {label.upper()} ===", text, ""]
    note = ("The video is attached." if attached else "The video was too big to attach.") + \
        (f" Backup download: {download_url}" if download_url else "")
    lines += [note, "", 'Reply "posted" once it\'s up, or "skip" to drop it (a skipped clip gets replaced). It also '
                        "notices on its own when it shows up on your TikTok.",
              'Other replies: "next" sends another clip now, "pause" stops posting emails, "resume" starts them again.']
    text = "\n".join(lines)

    def esc(t):
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html = ['<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;color:#111;max-width:560px">']
    for label, text_ in pkg["blocks"]:
        html.append(f'<p style="margin:18px 0 4px;font-size:12px;letter-spacing:.06em;color:#666">{esc(label.upper())}</p>'
                    f'<div style="background:#f3f3f3;border-radius:8px;padding:12px;white-space:pre-wrap;'
                    f'user-select:all">{esc(text_)}</div>')
    html.append(f'<p style="margin-top:20px;color:#444">{esc(note)}</p>'
                f'<p style="color:#444">Reply <b>posted</b> once it\'s up, or <b>skip</b> to drop it (it gets replaced). '
                f'Also: <b>next</b> sends another clip now, <b>pause</b> / <b>resume</b> the posting emails.</p></div>')
    return text, "".join(html)


def _ffmpeg():
    """ffmpeg from the system, or the pip one (imageio-ffmpeg) on runs that skip the apt install."""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _duration(src, ff):
    import shutil
    if shutil.which("ffprobe"):
        return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src]).strip())
    import subprocess
    err = subprocess.run([ff, "-i", str(src)], capture_output=True, text=True).stderr
    h, m, sec = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def fit_for_email(src, dest, limit):
    """Copy of the clip under `limit` bytes (re-encoded at a lower bitrate if needed, still 1080x1920)."""
    import os
    import shutil
    if os.path.getsize(src) <= limit:
        shutil.copy(src, dest)
        return dest
    ff = _ffmpeg()
    dur = _duration(src, ff)
    for factor in (0.92, 0.8, 0.65):
        kbps = max(800, int(limit * 8 / dur / 1000 * factor) - 128)
        run([ff, "-y", "-loglevel", "error", "-i", src, "-c:v", "libx264", "-preset", "medium",
             "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", dest])
        if os.path.getsize(dest) <= limit:
            return dest
    raise RuntimeError(f"couldn't get the clip under {limit // 1_000_000} MB")


# ------------------------------------------------------------------ did Elie post it?
def _norm(text):
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = re.sub(r"#\w+", " ", text)              # hashtags
    text = re.sub(r"streamer:.*", " ", text)        # the credit line
    text = re.sub(r"https?://\S+", " ", text)
    text = "".join(ch for ch in text if ch.isalnum() or ch.isspace())
    return " ".join(text.split())


def similarity(ours, theirs):
    a, b = _norm(ours), _norm(theirs)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_uploads(state, uploads, platform):
    """Mark emailed clips as posted when an upload matches them. uploads: [{text, timestamp, duration, url}].
    Returns the clips that matched."""
    hits = []
    week_ago = now_utc() - timedelta(days=7)
    for c in state["clips"].values():
        if c.get("status") not in ("approved", "posted") or not c.get("emailed_at") or platform in c.get("posts", {}):
            continue
        if _created(c) < week_ago:
            continue
        sent = datetime.fromisoformat(c["emailed_at"]) - timedelta(minutes=10)
        best, best_u = 0.0, None
        for u in uploads:
            if upload_time(u) and upload_time(u) < sent:
                continue
            score = max(similarity(c.get("caption", ""), u.get("text", "")),
                        similarity(c.get("title", ""), u.get("text", "")),
                        similarity(c.get("hook", ""), u.get("text", "")))
            if u.get("duration") and c.get("duration") and abs(float(u["duration"]) - float(c["duration"])) <= 1.5:
                score += 0.1
            if score > best:
                best, best_u = score, u
        if best >= MATCH_THRESHOLD:
            posted = upload_time(best_u) or now_utc()
            c.setdefault("posts", {})[platform] = {"status": "published", "url": best_u.get("url"), "seen_at": now_utc().isoformat(),
                                                  "date": posted.date().isoformat(), "posted_at": posted.isoformat(),
                                                  "match": round(best, 2), "views": None, "history": []}
            hits.append(c)
    return hits


def _vid_key(url):
    """The part of a post URL that identifies the video (TikTok id or YouTube id)."""
    m = re.search(r"/video/(\d+)|[?&]v=([\w-]{11})|/shorts/([\w-]{11})", url or "")
    return next((g for g in m.groups() if g), None) if m else (url or None)


def upload_time(u):
    """When a video went up (UTC). TikTok listings often leave the timestamp out, but a TikTok video id carries its
    upload time in its top 32 bits, so that is used instead of guessing."""
    if u.get("timestamp"):
        return datetime.fromtimestamp(u["timestamp"], timezone.utc)
    m = re.search(r"/video/(\d{15,})", u.get("url") or "")
    if m:
        t = int(m.group(1)) >> 32
        if 1_500_000_000 < t < 4_000_000_000:
            return datetime.fromtimestamp(t, timezone.utc)
    return None


def update_stats(state, uploads, platform, tz="UTC"):
    """Copy view/like/comment/share counts from the page onto the clips we know were posted. Returns how many."""
    from zoneinfo import ZoneInfo
    by_key = {_vid_key(u.get("url")): u for u in uploads if u.get("url")}
    n = 0
    for c in state["clips"].values():
        p = (c.get("posts") or {}).get(platform)
        if not p or not p.get("url"):
            continue
        u = by_key.get(_vid_key(p["url"]))
        if not u or u.get("views") is None:
            continue
        p.update(views=int(u["views"]), likes=u.get("likes"), comments=u.get("comments"), shares=u.get("shares"))
        real = upload_time(u)
        if real and (not p.get("posted_at") or abs(datetime.fromisoformat(p["posted_at"]) - real) > timedelta(minutes=2)):
            p["posted_at"], p["date"] = real.isoformat(), real.date().isoformat()   # fix a time guessed from when we saw it
            p.pop("hour", None)
        if not p.get("hour") and p.get("posted_at"):
            p["hour"] = datetime.fromisoformat(p["posted_at"]).astimezone(ZoneInfo(tz)).strftime("%H:00")
        hist = p.setdefault("history", [])
        if not hist or hist[-1][1] != p["views"]:
            p["history"] = (hist + [[now_utc().isoformat(), p["views"]]])[-60:]
        n += 1
    return n


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def tiktok_uploads(handle, n=30):
    """Latest TikTok uploads via yt-dlp: [{text, timestamp, duration, url, views, likes, comments, shares}].
    Raises if TikTok blocks the request."""
    errors = []
    for extra in (["--impersonate", "chrome"], []):
        try:
            raw = run(["yt-dlp", *extra, "--flat-playlist", "-J", "--playlist-end", str(n),
                       f"https://www.tiktok.com/@{handle}"])
            break
        except RuntimeError as e:
            if "does not have any videos" in str(e):
                return []   # a brand new account: readable, just empty
            errors.append(str(e)[-200:])
    else:
        try:
            return tikwm_uploads(handle, n)   # TikTok often blocks GitHub's servers; this public reader usually doesn't
        except Exception as e:
            errors.append(f"tikwm: {e}")
            raise RuntimeError(" | ".join(errors))
    out = []
    for e in json.loads(raw).get("entries") or []:
        out.append({"text": " ".join(x for x in (e.get("title"), e.get("description")) if x),
                    "timestamp": e.get("timestamp"), "duration": e.get("duration"),
                    "url": e.get("url") or e.get("webpage_url"), "views": _int(e.get("view_count")),
                    "likes": _int(e.get("like_count")), "comments": _int(e.get("comment_count")),
                    "shares": _int(e.get("repost_count"))})
    return out


def tikwm_uploads(handle, n=30):
    """Public TikTok post list and stats via tikwm.com (free, no key)."""
    r = requests.get("https://www.tikwm.com/api/user/posts", params={"unique_id": handle, "count": min(n, 35), "cursor": 0},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") not in (0, None):
        raise RuntimeError(data.get("msg") or f"code {data.get('code')}")
    out = []
    for v in ((data.get("data") or {}).get("videos") or []):
        vid = v.get("video_id") or v.get("aweme_id")
        out.append({"text": v.get("title") or "", "timestamp": v.get("create_time"), "duration": v.get("duration"),
                    "url": f"https://www.tiktok.com/@{handle}/video/{vid}", "views": _int(v.get("play_count")),
                    "likes": _int(v.get("digg_count")), "comments": _int(v.get("comment_count")),
                    "shares": _int(v.get("share_count"))})
    return out


def youtube_channel_id(handle):
    r = requests.get(f"https://www.youtube.com/@{handle}", timeout=30, headers={"User-Agent": "Mozilla/5.0",
                                                                               "Accept-Language": "en"})
    r.raise_for_status()
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', r.text) or \
        re.search(r'<link rel="canonical" href="https://www.youtube.com/channel/(UC[\w-]{22})"', r.text)
    if not m:
        raise RuntimeError(f"couldn't find the channel id for @{handle}")
    return m.group(1)


def youtube_shorts(handle, n=30):
    """Backup when the RSS feed is down: the channel's Shorts tab via yt-dlp (titles, links, view counts)."""
    raw = run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", str(n), f"https://www.youtube.com/@{handle}/shorts"])
    out = []
    for e in json.loads(raw).get("entries") or []:
        vid = e.get("id")
        out.append({"text": e.get("title") or "", "url": f"https://www.youtube.com/shorts/{vid}",
                    "timestamp": e.get("timestamp"), "views": _int(e.get("view_count")), "likes": None})
    return out


def youtube_uploads(channel_id, handle=None):
    """Latest uploads from the channel's RSS feed (with one retry); falls back to the Shorts tab if the feed is down."""
    import time
    r = None
    for attempt in range(2):
        try:
            r = requests.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, timeout=30)
            r.raise_for_status()
            break
        except Exception as e:
            r = None
            if attempt == 0:
                time.sleep(10)
            elif handle:
                log.info("YouTube feed failed (%s); reading the Shorts tab instead", e)
                return youtube_shorts(handle)
            else:
                raise
    ns = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", ns):
        title = e.findtext("a:title", "", ns)
        desc = e.findtext("m:group/m:description", "", ns)
        pub = e.findtext("a:published", "", ns)
        link = e.find("a:link", ns)
        stats = e.find("m:group/m:community/m:statistics", ns)
        rating = e.find("m:group/m:community/m:starRating", ns)
        out.append({"text": f"{title} {desc}", "url": link.get("href") if link is not None else None,
                    "timestamp": datetime.fromisoformat(pub).timestamp() if pub else None,
                    "views": _int(stats.get("views")) if stats is not None else None,
                    "likes": _int(rating.get("count")) if rating is not None else None})
    return out


def claim_email(cfg, state, clip, platform):
    """If the clip belongs to a joined campaign, email the exact link to submit for payment (once per platform)."""
    from . import campaigns, mail
    rec = campaigns.campaign_for(state, clip.get("streamer"), joined_only=True)
    url = (clip.get("posts", {}).get(platform) or {}).get("url")
    if not rec or not url or clip.setdefault("claim_sent", {}).get(platform) or not mail.configured():
        return
    text = "\n".join([f"Your {platform} post of \"{clip['hook']}\" is live. Submit it to get paid:", "",
                      f"1. Open the campaign: {rec.get('join_url') or rec['url']}",
                      "2. Tap Submit (or Add submission).", f"3. Paste this link: {url}", "",
                      "Views only count after the board approves the submission, so do it today."])
    try:
        mail.send(f"Claim: {clip['hook'][:60]} ({rec['name'][:40]})", text)
        clip["claim_sent"][platform] = now_utc().isoformat()
    except Exception as e:
        log.info("couldn't send the claim email: %s", e)


def refresh_posted(cfg, state):
    """Check TikTok, YouTube and email replies; take posted/skipped clips out of the queue. Returns notes."""
    from . import mail
    acct = cfg.get("accounts") or {}
    notes = []
    tt = acct.get("tiktok")
    if tt:
        for attempt in (1, 2):
            try:
                ups = tiktok_uploads(tt)
                for c in match_uploads(state, ups, "tiktok"):
                    if c["status"] == "approved":
                        c["status"] = "posted"
                    notes.append(f"seen on TikTok: {c['hook']}")
                    claim_email(cfg, state, c, "tiktok")
                update_stats(state, ups, "tiktok", cfg.get("timezone", "UTC"))
                state.setdefault("health", {})["tiktok_check"] = {"ok": now_utc().isoformat()}
                break
            except Exception as e:
                if attempt == 2:
                    log.info("TikTok check failed (%s); relying on email replies", e)
                    state.setdefault("health", {})["tiktok_check"] = {"error": str(e)[:200], "at": now_utc().isoformat()}
                else:
                    import time
                    time.sleep(20)
    yt = acct.get("youtube")
    if yt:
        try:
            ids = state.setdefault("accounts", {})
            if not ids.get("youtube_channel_id"):
                ids["youtube_channel_id"] = youtube_channel_id(yt)
            yups = youtube_uploads(ids["youtube_channel_id"], yt)
            for c in match_uploads(state, yups, "youtube"):
                if c["status"] == "approved":   # posted on either platform: out of the queue
                    c["status"] = "posted"
                notes.append(f"seen on YouTube: {c['hook']}")
            update_stats(state, yups, "youtube", cfg.get("timezone", "UTC"))
            for c in state["clips"].values():
                if c.get("posts", {}).get("youtube") and not c.get("claim_sent", {}).get("youtube"):
                    claim_email(cfg, state, c, "youtube")
        except Exception as e:
            log.info("YouTube check failed: %s", e)
    em = state.setdefault("email", {})
    last = em.get("replies_at")
    due = not last or now_utc() - datetime.fromisoformat(last) >= timedelta(minutes=REPLY_CHECK_MINUTES)
    if mail.configured() and due and not mail.down_until():
        em["replies_at"] = now_utc().isoformat()
        try:
            from . import campaigns
            by_msg = {c.get("email_message_id"): c for c in state["clips"].values() if c.get("email_message_id")}
            for ref, verdict in mail.read_replies():
                em = state.setdefault("email", {})
                if verdict in ("pause", "resume"):
                    em["paused"] = verdict == "pause"
                    notes.append("you paused the posting emails" if em["paused"] else "you resumed the posting emails")
                    continue
                if verdict.startswith("launch:"):
                    from datetime import date as _date
                    try:
                        new = _date.fromisoformat(verdict.split(":", 1)[1]).isoformat()
                        state.setdefault("launch", {}).update(date=new, reminded=None)
                        notes.append(f"GTA 6 launch date set to {new}")
                    except ValueError:
                        pass
                    continue
                if verdict == "next":
                    em["send_next"] = True
                    notes.append("you asked for another clip now")
                    continue
                if campaigns.apply_reply(state, ref, verdict):
                    notes.append(f"campaign reply: {verdict}")
                    continue
                if verdict == "join":
                    continue
                c = by_msg.get(ref)
                if not c or c.get("status") != "approved":
                    continue
                c["status"] = "posted" if verdict == "posted" else "skipped"
                c["reply"] = verdict
                notes.append(f"you replied {verdict}: {c['hook']}")
        except Exception as e:
            log.info("reading email replies failed: %s", e)
    return notes
