"""Campaign clips on the Mac, end to end (Content Rewards pre-approval campaigns like Lysium):

1. The post job reaches a campaign clip's slot -> enqueue(): the clip goes into data/cr_queue.json instead of to
   Elie. The Mac loop uploads it to the campaign as a draft and texts "sent for approval".
2. The Mac's Content Rewards check sees the draft approved -> Elie gets the post package on Telegram, with Posted.
3. He taps Posted -> the Mac looks for the new TikTok/YouTube posts every 2 minutes and submits each link on
   Content Rewards (it only takes links within 30 minutes of posting). He can also paste a link into the chat.

The pure parts (matching approvals and posts) are here; the browser work is in contentrewards.py and the loop in
mac.py. No Claude calls.
"""
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta

from .common import DATA, WORK, log, now_utc

QUEUE = DATA / "cr_queue.json"
HUNT_MINUTES = 25          # Content Rewards takes links up to 30 minutes after posting
MATCH = 0.6                # how alike a post's caption/title must be to ours


def load():
    try:
        return json.loads(QUEUE.read_text())
    except (OSError, ValueError):
        return []


def save(entries):
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries[-200:], indent=2))
    tmp.replace(QUEUE)


def campaign_name(cfg, clip):
    for s in cfg.get("streamers") or []:
        if s.get("campaign") and (s.get("name") or "").lower() == (clip.get("streamer") or "").lower():
            return s["campaign"]
    return None


def applies(cfg, clip):
    """True when this clip should go to Content Rewards as a draft first (Mac + Telegram mode, campaign clip)."""
    from . import mail
    return (os.environ.get("MAC_RUNS_CLIPPER") == "1" and mail.telegram_mode() and bool(clip.get("preapproval"))
            and bool(campaign_name(cfg, clip)))


def platforms(cfg):
    from . import clipqueue
    out = [p for p in (cfg.get("posting") or {}).get("platforms") or ["tiktok", "youtube"] if p in ("tiktok", "youtube")]
    if clipqueue.tiktok_paused(cfg) or clipqueue.tiktok_daily(cfg):
        out = [p for p in out if p != "tiktok"]
    return out


def enqueue(cfg, clip, src, pkg):
    """Called by the post job instead of sending the package. Returns the message id it records on the clip."""
    from .common import work_ready
    if not work_ready():
        raise RuntimeError("the Work SSD isn't mounted; the campaign clip stays in the queue")
    d = WORK / "cr_drafts"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{clip['id']}.mp4"
    if src and os.path.abspath(str(src)) != str(dest.resolve()):
        shutil.copy(src, dest)
    token = uuid.uuid4().hex[:10]
    try:   # the length Content Rewards shows is the file's (a sped-up render is shorter than the cut)
        from .clipqueue import _duration, _ffmpeg
        length = round(_duration(str(dest), _ffmpeg()), 1)
    except Exception:
        length = clip.get("duration")
    entries = [e for e in load() if e.get("id") != clip["id"]]
    entries.append({"id": clip["id"], "token": token, "file": str(dest), "campaign": campaign_name(cfg, clip),
                    "duration": length, "hook": clip.get("hook"), "subject": pkg["subject"],
                    "score": clip.get("score"), "size": os.path.getsize(dest) if dest.exists() else None,
                    "caption": clip.get("caption") or "", "title": clip.get("title") or "",
                    "blocks": pkg["blocks"], "platforms": platforms(cfg), "status": "queued",
                    "queued_at": now_utc().isoformat(), "tries": 0, "links": {}})
    save(entries)
    log.info("campaign clip %s queued as a Content Rewards draft", clip["id"])
    return f"tg:{token}"


def post_parts(entry, manual=False):
    """(info text, fields to paste) for the Telegram post package. manual=True keeps the "submit it as a draft
    yourself" steps (used when the Mac couldn't upload the draft)."""
    from .relay import split_blocks
    blocks = [b for b in entry.get("blocks") or [] if manual or b[0] != "Campaign approval first"]
    from .clipqueue import tiktok_daily
    from .common import load_config
    if tiktok_daily(load_config()):   # TikTok only gets the bot's one daily post
        blocks = [b for b in blocks if b[0] != "TikTok caption"]
    info, fields = split_blocks(blocks)
    head = (f"📲 {entry.get('subject')}" if manual else
            f"✅ {entry.get('campaign')} approved your draft. Post it now:\n\n📲 {entry.get('subject')}")
    yt_done = any(b[0] == "YouTube: already posted" for b in entry.get("blocks") or [])
    tail = (("Tap ✅ Posted right after you post it on TikTok, and I'll submit the link on Content Rewards for you "
             if yt_done else
             "Tap ✅ Posted right after you post it on both, and I'll submit the links on Content Rewards for you ")
            + "(it only takes them within 30 minutes of posting)." if not manual else
            "Submit the video as a draft on the campaign page first, then post it once it's approved.")
    order = ", then ".join(label for label, _ in fields)
    return f"{head}\n\n{info}\n\nNext messages, one per field: {order}.\n{tail}".replace("\n\n\n", "\n\n"), fields


def match_approvals(entries, cards):
    """Mark submitted drafts approved when an approved card shows up for them. Cards already accounted for by drafts
    approved earlier (still waiting for links) are skipped. Returns the newly approved entries."""
    from .contentrewards import pick_card
    free = [c for c in cards if c.get("posted", 0) < c.get("of", 1)]
    for e in entries:   # cards that belong to drafts we already know are approved
        if e.get("status") in ("approved", "posted", "manual"):
            i = pick_card(free, e.get("campaign"), e.get("duration"))
            if i is not None:
                free.pop(i)
    newly = []
    for e in sorted((e for e in entries if e.get("status") == "submitted"), key=lambda e: e.get("submitted_at") or ""):
        i = pick_card(free, e.get("campaign"), e.get("duration"))
        if i is None:
            continue
        free.pop(i)
        e["status"], e["approved_at"] = "approved", now_utc().isoformat()
        newly.append(e)
    return newly


def per_day(cfg):
    """Most clips per campaign per day (drafts sent for approval, and post packages sent to Elie)."""
    return int(((cfg.get("clip") or {}).get("campaign") or {}).get("max_per_day") or 3)


def daily_total(cfg):
    return int((cfg.get("posting") or {}).get("daily_total") or 0)


def _local_day(iso, tz):
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).date().isoformat()
    except (TypeError, ValueError):
        return ""


def claimed_today(cfg, entries=None):
    """Today's places that campaign clips take or will take: packages sent today, approved ones waiting, and drafts
    sent today still waiting for the brand. Regular posting times only get what's left of posting.daily_total."""
    tz = cfg.get("timezone", "UTC")
    today = _local_day(now_utc().isoformat(), tz)
    entries = load() if entries is None else entries
    n = 0
    for e in entries:
        if _local_day(e.get("packaged_at"), tz) == today:
            n += 1
        elif e.get("status") == "approved" and not e.get("packaged_at"):
            n += 1
        elif e.get("status") == "submitted" and _local_day(e.get("submitted_at"), tz) == today:
            n += 1
    return n


def best_first(entries):
    return sorted(entries, key=lambda e: (-(e.get("score") or 0), e.get("queued_at") or ""))


def file_size(e):
    try:
        return os.path.getsize(e["file"])
    except (OSError, KeyError, TypeError):
        return e.get("size")


def match_drafts(entries, cards):
    """Tie the Approved cards to our clips by Content Rewards' draft id, learning the id the first time from the
    video's exact size (the file we uploaded). Marks newly approved clips and syncs how many accounts each draft
    has been posted on. Returns (newly approved, mismatches): mismatches are drafts showing more posts than links
    we know of, i.e. something was attached to them by mistake or by hand."""
    newly, odd = [], []
    for c in cards:
        if not c.get("id"):
            continue
        e = next((x for x in entries if x.get("draft_id") == c["id"]), None)
        if e is None and c.get("size"):
            e = next((x for x in entries if not x.get("draft_id") and file_size(x) == c["size"]), None)
            if e is not None:
                e["draft_id"] = c["id"]
        if e is None:
            continue
        e["cr_posted"] = [c.get("posted", 0), c.get("of", 0)]
        known = len([v for v in (e.get("links") or {}).values() if v and not str(v).startswith("failed:")])
        if c.get("posted", 0) > known:
            odd.append(e)
        if e.get("status") in ("submitted", "manual_draft", "manual"):
            e["status"], e["approved_at"] = "approved", e.get("approved_at") or now_utc().isoformat()
            newly.append(e)
    return newly, odd


def by_token(entries, token):
    return next((e for e in entries if e.get("token") == token), None)


def platform_of(url):
    if re.search(r"tiktok\.com/", url or "", re.I):
        return "tiktok"
    if re.search(r"youtube\.com/|youtu\.be/", url or "", re.I):
        return "youtube"
    return None


def find_posts(entry, uploads, now=None):
    """{platform: url} for posts that match this entry and went up after it was approved.
    uploads: {platform: [{text, url, timestamp}]}."""
    from .clipqueue import similarity, upload_time
    now = now or now_utc()
    since = datetime.fromisoformat(entry.get("approved_at") or entry.get("posted_at") or now.isoformat()) \
        - timedelta(minutes=5)
    found = {}
    for platform, ups in (uploads or {}).items():
        if platform in (entry.get("links") or {}) or platform not in entry.get("platforms", []):
            continue
        best, url = 0.0, None
        for u in ups or []:
            t = upload_time(u)
            if t and t < since:
                continue
            score = max(similarity(entry.get("caption", ""), u.get("text", "")),
                        similarity(entry.get("title", ""), u.get("text", "")),
                        similarity(entry.get("hook", ""), u.get("text", "")))
            if score > best:
                best, url = score, u.get("url")
        if url and best >= MATCH:
            found[platform] = url
    return found


def hunting(entries, now=None):
    """Entries whose Posted was tapped and whose links aren't all in yet, still inside the window."""
    now = now or now_utc()
    out = []
    for e in entries:
        if e.get("status") != "posted":
            continue
        if now - datetime.fromisoformat(e["posted_at"]) <= timedelta(minutes=HUNT_MINUTES):
            out.append(e)
    return out


WATCH_HOURS = 24   # after the post package goes out, look for the posts this long even without a Posted tap


def watching(entries, now=None):
    """Approved drafts whose package went out recently: Elie may post without tapping Posted, so look anyway."""
    now = now or now_utc()
    out = []
    for e in entries:
        if e.get("status") == "approved" and e.get("packaged_at") and \
                now - datetime.fromisoformat(e["packaged_at"]) <= timedelta(hours=WATCH_HOURS):
            out.append(e)
    return out


def expired(entries, now=None):
    now = now or now_utc()
    return [e for e in entries if e.get("status") == "posted"
            and now - datetime.fromisoformat(e["posted_at"]) > timedelta(minutes=HUNT_MINUTES)]
