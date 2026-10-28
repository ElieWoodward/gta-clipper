"""Weekly long-form episode for YouTube: "GTA RP MOST VIEWED Twitch Clips of The Week! #N".

Template: the numbered weekly "MOST VIEWED Twitch Clips of The Week" series that independent channels run for other
games (Rivals Guides started theirs the week Marvel Rivals launched). The week's most-watched GTA clips on Twitch are
ranked by their Twitch view counts and counted down to #1. On top of the template, every clip shows the real chat
replay beside it and a hype meter (how fast chat was going compared with just before the moment), with stats cards
built from that data. That's what this channel adds that the source streams don't have.

No Claude calls: the ranking, stats and titles all come from Twitch data. From GTA 6's launch day the series switches
to "GTA 6 MOST VIEWED Twitch Clips of The Week!" starting again at #1.

Output: a 1920x1080 mp4 (8 to 12 minutes), a 1280x720 thumbnail, and an email with the download link, title,
description with chapters and streamer credits, and upload steps.
"""
import bisect
import json
import os
import re
import shutil
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .common import DATA, WORK, log, now_utc, run

STATE = DATA / "weekly.json"
W, H, FPS, PANEL_FPS = 1920, 1080, 30, 10
CLIP_X, CLIP_Y, CLIP_W, CLIP_H = 24, 126, 1472, 828
PANEL_X, PANEL_Y, PANEL_W, PANEL_H = 1520, 126, 376, 828
METER_X, METER_Y, METER_W, METER_H = 24, 972, 1872, 84
METER_BAR_X = 760       # the bar starts right of the stats text
BG = (14, 14, 18)
CARD = (26, 26, 33)
YELLOW = (255, 214, 10)
WHITE = (245, 245, 245)
GREY = (160, 160, 170)
RED = (235, 64, 52)
CHAT_DELAY = 4          # chat in a replay reacts a few seconds after the moment
DEFAULTS = {"enabled": True, "weekday": 6, "hour": 9, "target_minutes": 10, "min_minutes": 8, "max_clips": 18,
            "min_clip_seconds": 10, "languages": ["EN"], "keep_episodes": 2}
STOP = set("the a an and or to of in is it i you he she they we that this was for on at be so my me with just "
           "what are not no yes oh lol its it's im i'm".split())


def settings(cfg):
    return dict(DEFAULTS, **(cfg.get("weekly") or {}))


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def series(cfg):
    """(series name, Twitch categories): GTA 6 from launch day, GTA RP before."""
    L = cfg.get("_launch") or {}
    if L.get("on"):
        return "GTA 6", list(L.get("games") or ["Grand Theft Auto VI"])
    return "GTA RP", ["Grand Theft Auto V"]


def due(cfg, st=None, now=None):
    """True on the episode day (after the hour) if this week's episode hasn't been made yet."""
    from zoneinfo import ZoneInfo
    s = settings(cfg)
    if not s["enabled"]:
        return False
    local = (now or now_utc()).astimezone(ZoneInfo(cfg.get("timezone", "UTC")))
    st = st if st is not None else load()
    last = st.get("last_made")
    if local.weekday() != int(s["weekday"]) or local.hour < int(s["hour"]):
        return False
    fail = st.get("fail") or {}
    if fail.get("date") == local.date().isoformat() and int(fail.get("n", 0)) >= 3:
        return False   # tried three times today; the failure email already went out
    return not last or (local.date() - datetime.fromisoformat(last).date()).days >= 6


# ------------------------------------------------------------------ 1. the week's most-viewed clips
CRITERIA = ["{period: LAST_WEEK, sort: VIEWS_DESC}", "{filter: LAST_WEEK}"]


def top_clips(games, n=60):
    """The most-viewed Twitch clips of the past week in these categories, best first."""
    from . import twitch
    out = []
    for game in games:
        for crit in CRITERIA:
            q = ('query { game(name: "%s") { clips(first: %d, criteria: %s) { edges { node { id slug title viewCount '
                 'durationSeconds createdAt videoOffsetSeconds video { id } broadcaster { login displayName '
                 'broadcastSettings { language } } } } } } }' % (game, n, crit))
            data = twitch._gql({"query": q}) or {}
            edges = ((((data.get("data") or {}).get("game") or {}).get("clips") or {}).get("edges")) or []
            if not edges:
                continue
            for e in edges:
                c = e.get("node") or {}
                b = c.get("broadcaster") or {}
                out.append({"slug": c.get("slug"), "title": (c.get("title") or "").strip(),
                            "views": int(c.get("viewCount") or 0), "duration": float(c.get("durationSeconds") or 0),
                            "created": c.get("createdAt"), "login": (b.get("login") or "").lower(),
                            "name": b.get("displayName") or b.get("login") or "?",
                            "lang": ((b.get("broadcastSettings") or {}).get("language") or "").upper(),
                            "vid": (c.get("video") or {}).get("id"), "offset": c.get("videoOffsetSeconds")})
            break
    out = [c for c in out if c["slug"]]
    return sorted({c["slug"]: c for c in out}.values(), key=lambda c: -c["views"])


def pick(clips, s):
    """Top clips by views until the episode is long enough, one per moment, then ordered as a countdown."""
    langs = {x.upper() for x in s["languages"] or []}
    chosen, seen = [], []
    total = 0.0
    for c in clips:
        if c["duration"] < s["min_clip_seconds"] or (langs and c["lang"] and c["lang"] not in langs):
            continue
        same = any(c["login"] == o["login"] and c["vid"] and c["vid"] == o["vid"] and o["offset"] is not None
                   and c["offset"] is not None and abs(c["offset"] - o["offset"]) < 60 for o in seen)
        if same or any(c["title"] and c["title"].lower() == o["title"].lower() and c["login"] == o["login"]
                       for o in seen):
            continue   # another clip of the same moment
        chosen.append(c)
        seen.append(c)
        total += c["duration"] + 2
        if total >= s["target_minutes"] * 60 or len(chosen) >= s["max_clips"]:
            break
    for i, c in enumerate(chosen):
        c["rank"] = i + 1
    return list(reversed(chosen))   # countdown: lowest rank first, #1 last


# ------------------------------------------------------------------ 2. chat replay and stats
def chat_window(vid, start, stop, max_pages=300):
    """[(seconds, name, color, [(text, emote_id)])] for a VOD window. [] if the VOD or its chat is gone."""
    from . import twitch
    msgs, offset, seen = [], max(0, int(start)), set()
    for _ in range(max_pages):
        data = twitch._gql({"operationName": "VideoCommentsByOffsetOrCursor",
                            "variables": {"videoID": str(vid), "contentOffsetSeconds": offset},
                            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": twitch.CHAT_HASH}}})
        if isinstance(data, list):
            data = data[0] if data else None
        com = ((((data or {}).get("data") or {}).get("video") or {}).get("comments")) or {}
        edges = com.get("edges") or []
        if not edges:
            break
        last = offset
        for e in edges:
            n = e.get("node") or {}
            t = n.get("contentOffsetSeconds")
            key = n.get("id") or (t, str(n.get("message"))[:60])
            if t is None or key in seen:
                continue
            seen.add(key)
            last = max(last, int(t))
            if start <= t < stop:
                m = n.get("message") or {}
                frags = [(f.get("text") or "", ((f.get("emote") or {}).get("emoteID"))) for f in m.get("fragments") or []]
                name = ((n.get("commenter") or {}).get("displayName")) or "viewer"
                msgs.append((float(t), name, m.get("userColor") or "", frags))
        if last >= stop or not (com.get("pageInfo") or {}).get("hasNextPage"):
            break
        offset = last if last > offset else offset + 1
    return sorted(msgs, key=lambda m: m[0])


def stats(msgs, clip_start, dur, before=90):
    """Hype numbers for one clip: per-second chat rate during the clip vs the minute and a half before it."""
    import numpy as np
    rel = [m[0] - clip_start for m in msgs]
    base = [r for r in rel if -before <= r < 0]
    base_rate = max(0.5, len(base) / before)
    secs = int(dur) + 1
    during = np.zeros(secs)
    for r in rel:
        i = int(r - CHAT_DELAY)
        if 0 <= i < secs:
            during[i] += 1
    smooth = np.convolve(during, np.ones(3) / 3, mode="same")
    words = Counter()   # short messages count whole ("NO WAY"), longer ones by word
    for t, _, _, frags in msgs:
        if clip_start <= t - CHAT_DELAY < clip_start + dur:
            text = " ".join(f[0] for f in frags).strip()
            toks = re.findall(r"[A-Za-z0-9']{2,20}", text)
            if 0 < len(toks) <= 3:
                words[" ".join(toks).upper()] += 1
            else:
                for w in toks:
                    if w.lower() not in STOP:
                        words[w.upper()] += 1
    top = words.most_common(1)
    return {"base_rate": base_rate, "curve": (smooth / base_rate).tolist(), "peak": float((smooth / base_rate).max()
            if secs else 0), "peak_per_min": int(smooth.max() * 60) if secs else 0,
            "top_word": top[0] if top else None, "messages": int(during.sum()), "have_chat": bool(msgs)}


# ------------------------------------------------------------------ 3. drawing
def _font(size, bold=False):
    from PIL import ImageFont
    from .render import FONTS_DIR
    path = FONTS_DIR / ("Anton-Regular.ttf" if bold else "TikTokSans.ttf")
    return ImageFont.truetype(str(path), size)


def _clean(t):
    from .render import strip_emoji
    return "".join(ch for ch in strip_emoji(t) if ord(ch) < 0x2E80).strip()


def _fit(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _views(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e4 else f"{n:,}"


def _hex(c, default=(180, 140, 255)):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", c or "")
    if not m:
        return default
    v = m.group(1)
    rgb = tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    return rgb if sum(rgb) > 200 else default   # too dark to read on the dark panel


def background(clip, st, series_name, path):
    """The static frame around one clip: rank and title bar, chat panel header, stats labels."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([CLIP_X - 4, CLIP_Y - 4, CLIP_X + CLIP_W + 4, CLIP_Y + CLIP_H + 4], 10, fill=CARD)
    d.rounded_rectangle([PANEL_X, PANEL_Y, PANEL_X + PANEL_W, PANEL_Y + PANEL_H], 12, fill=CARD)
    d.rounded_rectangle([METER_X, METER_Y, METER_X + METER_W, METER_Y + METER_H], 12, fill=CARD)
    rank = f"#{clip['rank']}"
    big = _font(92, bold=True)
    d.text((CLIP_X, 8), rank, font=big, fill=YELLOW)
    x = CLIP_X + d.textlength(rank, font=big) + 26
    d.text((x, 18), _fit(d, clip["name"].upper(), _font(40, bold=True), 900), font=_font(40, bold=True), fill=WHITE)
    d.text((x, 68), _fit(d, _clean(clip["title"]) or " ", _font(30), 1050), font=_font(30), fill=GREY)
    views = f"{clip['views']:,} views on Twitch"
    f = _font(34, bold=True)
    tw = d.textlength(views, font=f)
    d.rounded_rectangle([W - 24 - tw - 36, 30, W - 24, 92], 31, fill=(145, 70, 255))
    d.text((W - 24 - tw - 18, 38), views, font=f, fill=WHITE)
    d.text((PANEL_X + 18, PANEL_Y + 14), "LIVE CHAT REPLAY", font=_font(26, bold=True), fill=YELLOW)
    d.line([PANEL_X + 18, PANEL_Y + 56, PANEL_X + PANEL_W - 18, PANEL_Y + 56], fill=(60, 60, 70), width=2)
    d.text((METER_X + 20, METER_Y + 10), "CHAT HYPE", font=_font(28, bold=True), fill=YELLOW)
    if st["have_chat"]:
        extra = [f"peak {st['peak_per_min']:,} messages/min"]
        if st["top_word"]:
            extra.append(f'top reaction "{_clean(st["top_word"][0])}" x{st["top_word"][1]}')
        d.text((METER_X + 20, METER_Y + 46), "  ·  ".join(extra), font=_font(24), fill=GREY)
    else:
        d.text((METER_X + 20, METER_Y + 46), "chat replay unavailable for this clip", font=_font(24), fill=GREY)
    im.save(path)
    return path


class Emotes:
    """Native Twitch emote images (third-party emotes stay as text)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.cache = {}

    def get(self, eid, size=28):
        from PIL import Image
        if eid in self.cache:
            return self.cache[eid]
        img = None
        p = self.folder / f"{re.sub(r'[^A-Za-z0-9_]', '_', str(eid))}.png"
        try:
            if not p.exists():
                import requests
                r = requests.get(f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/static/dark/1.0", timeout=10)
                r.raise_for_status()
                p.write_bytes(r.content)
            img = Image.open(p).convert("RGBA")
            img = img.resize((max(1, int(img.width * size / img.height)), size))
        except Exception:
            img = None
        self.cache[eid] = img
        return img


def _layout_message(d, msg, emotes, width):
    """A chat message as lines of draw ops: [[("name"|"text"|"emote", value, x)]], wrapped to width."""
    _, name, color, frags = msg
    fn, ft = _font(22, bold=False), _font(22)
    lines, line, x = [], [], 0
    nm = _clean(name)[:20] + ":"
    line.append(("name", (nm, _hex(color)), x))
    x += d.textlength(nm + " ", font=fn)
    for text, eid in frags:
        img = emotes.get(eid) if eid else None
        if img is not None:
            if x + img.width > width:
                lines.append(line)
                line, x = [], 0
            line.append(("emote", img, x))
            x += img.width + 4
            continue
        for w in _clean(text).split():
            wl = d.textlength(w + " ", font=ft)
            if x + wl > width and line:
                lines.append(line)
                line, x = [], 0
            line.append(("text", w, x))
            x += wl
    if line:
        lines.append(line)
    return lines[:4]


def panel_frames(msgs, clip_start, dur, curve, folder, emotes):
    """PNG frames (PANEL_FPS) for the chat panel and the hype meter."""
    from PIL import Image, ImageDraw
    folder = Path(folder)
    (folder / "chat").mkdir(parents=True, exist_ok=True)
    (folder / "meter").mkdir(parents=True, exist_ok=True)
    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    inner = PANEL_W - 36
    # keep chat readable: at most ~6 new messages a second on screen
    shown, per_sec = [], Counter()
    for m in msgs:
        r = m[0] - clip_start
        if -2 <= r < dur + 2 and per_sec[int(r)] < 6:
            per_sec[int(r)] += 1
            shown.append((r, _layout_message(scratch, m, emotes, inner)))
    times = [s[0] for s in shown]
    lh, top, bottom = 30, PANEL_Y + 70, PANEL_Y + PANEL_H - 16
    ft, fn, fm = _font(22), _font(22), _font(40, bold=True)
    n = int(dur * PANEL_FPS) + 1
    for i in range(n):
        t = i / PANEL_FPS
        im = Image.new("RGBA", (PANEL_W, PANEL_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        y = bottom - PANEL_Y
        k = bisect.bisect_right(times, t)
        for _, lines in reversed(shown[max(0, k - 30):k]):
            if y - lh * len(lines) < top - PANEL_Y:
                break
            y -= lh * len(lines) + 6
            for j, ops in enumerate(lines):
                yy = y + j * lh
                for kind, val, x in ops:
                    if kind == "name":
                        d.text((18 + x, yy), val[0], font=fn, fill=val[1])
                    elif kind == "text":
                        d.text((18 + x, yy), val, font=ft, fill=WHITE)
                    else:
                        im.alpha_composite(val, (int(18 + x), int(yy)))
        im.save(folder / "chat" / f"{i:05d}.png")
        # hype meter: x times the normal chat speed
        mv = Image.new("RGBA", (METER_W - METER_BAR_X - 20, METER_H - 24), (0, 0, 0, 0))
        md = ImageDraw.Draw(mv)
        v = curve[min(len(curve) - 1, int(t))] if curve else 0
        frac = max(0.0, min(1.0, v / 10))
        bw = mv.width - 190
        md.rounded_rectangle([0, 14, bw, 46], 16, fill=(40, 40, 50))
        if frac > 0.01:
            col = YELLOW if v < 5 else RED
            md.rounded_rectangle([0, 14, max(32, int(bw * frac)), 46], 16, fill=col)
        md.text((bw + 20, 2), f"x{v:.1f}", font=fm, fill=WHITE if v < 5 else RED)
        mv.save(folder / "meter" / f"{i:05d}.png")
    return folder / "chat" / "%05d.png", folder / "meter" / "%05d.png"


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS), "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]


def segment(src, bg, chat_seq, meter_seq, dur, out, ff="ffmpeg"):
    """One ranked clip: the clip in its box, chat panel, hype meter, all over the static frame."""
    vf = (f"[1:v]scale={CLIP_W}:{CLIP_H}:force_original_aspect_ratio=decrease,"
          f"pad={CLIP_W}:{CLIP_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[c];"
          f"[0:v][c]overlay={CLIP_X}:{CLIP_Y}[a];[a][2:v]overlay={PANEL_X}:{PANEL_Y}[b];"
          f"[b][3:v]overlay={METER_X + METER_BAR_X}:{METER_Y + 12},fps={FPS},format=yuv420p[v]")
    run([ff, "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", bg, "-i", src,
         "-framerate", str(PANEL_FPS), "-i", chat_seq, "-framerate", str(PANEL_FPS), "-i", meter_seq,
         "-filter_complex", vf, "-map", "[v]", "-map", "1:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-t", f"{dur:.2f}", *ENC, out])
    return out


def still(img, dur, out, ff="ffmpeg"):
    """A card (PNG) as a short silent video segment."""
    run([ff, "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", img,
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.2f}", "-vf", "format=yuv420p",
         "-shortest", *ENC, out])
    return out


def frame_at(src, t, out, ff="ffmpeg"):
    run([ff, "-y", "-loglevel", "error", "-ss", f"{max(0, t):.2f}", "-i", src, "-frames:v", "1", out])
    return out


def _blurred(frame_png, darken=0.45):
    from PIL import Image, ImageEnhance, ImageFilter
    im = Image.open(frame_png).convert("RGB").resize((W, H))
    return ImageEnhance.Brightness(im.filter(ImageFilter.GaussianBlur(18))).enhance(darken)


def _centered(d, y, text, font, fill, stroke=0):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))


def rank_card(clip, frame_png, out):
    from PIL import ImageDraw
    im = _blurred(frame_png)
    d = ImageDraw.Draw(im)
    _centered(d, 250, f"#{clip['rank']}", _font(300, bold=True), YELLOW, stroke=6)
    _centered(d, 620, clip["name"].upper(), _font(84, bold=True), WHITE, stroke=3)
    _centered(d, 740, _fit(d, _clean(clip["title"]) or " ", _font(46), 1500), _font(46), WHITE)
    _centered(d, 820, f"{clip['views']:,} views on Twitch", _font(40, bold=True), (190, 160, 255))
    im.save(out)
    return out


def intro_card(series_name, episode, span, frame_png, out):
    from PIL import ImageDraw
    im = _blurred(frame_png, 0.35)
    d = ImageDraw.Draw(im)
    _centered(d, 200, series_name.upper(), _font(150, bold=True), YELLOW, stroke=5)
    _centered(d, 400, "MOST VIEWED TWITCH CLIPS", _font(110, bold=True), WHITE, stroke=4)
    _centered(d, 540, "OF THE WEEK", _font(110, bold=True), WHITE, stroke=4)
    _centered(d, 720, f"EPISODE #{episode}  ·  {span}", _font(54, bold=True), (190, 160, 255))
    _centered(d, 820, "ranked by Twitch views, with the live chat replay", _font(40), GREY)
    im.save(out)
    return out


def outro_card(series_name, episode, frame_png, out):
    from PIL import ImageDraw
    im = _blurred(frame_png, 0.3)
    d = ImageDraw.Draw(im)
    _centered(d, 300, "THAT'S THE WEEK", _font(140, bold=True), YELLOW, stroke=5)
    _centered(d, 500, f"Episode #{episode + 1} drops next Sunday", _font(64, bold=True), WHITE, stroke=3)
    _centered(d, 620, f"Subscribe so you don't miss the week's best {series_name} moments", _font(44), GREY)
    im.save(out)
    return out


def thumbnail(clip, frame_png, series_name, out):
    """1280x720: the #1 moment, a huge #1, the streamer, and the series line."""
    from PIL import Image, ImageDraw, ImageEnhance
    im = Image.open(frame_png).convert("RGB").resize((1280, 720))
    im = ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(1.35)).enhance(1.15)
    shade = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for x in range(560):
        sd.line([x, 0, x, 720], fill=(0, 0, 0, int(200 * (1 - x / 560))))
    im = Image.alpha_composite(im.convert("RGBA"), shade).convert("RGB")
    d = ImageDraw.Draw(im)
    d.text((40, 10), "#1", font=_font(330, bold=True), fill=YELLOW, stroke_width=8, stroke_fill=(0, 0, 0))
    d.text((48, 430), _fit(d, clip["name"].upper(), _font(90, bold=True), 700), font=_font(90, bold=True),
           fill=WHITE, stroke_width=5, stroke_fill=(0, 0, 0))
    d.rounded_rectangle([40, 590, 760, 680], 14, fill=(145, 70, 255))
    d.text((60, 596), f"{series_name.upper()} MOST VIEWED", font=_font(62, bold=True), fill=WHITE)
    im.save(out, quality=92)
    return out


# ------------------------------------------------------------------ 4. text
def _clock(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def texts(series_name, episode, span, chapters, clips):
    title = f"{series_name} MOST VIEWED Twitch Clips of The Week! #{episode}"[:100]
    credits, seen = [], set()
    for c in sorted(clips, key=lambda c: c["rank"]):
        if c["login"] not in seen:
            seen.add(c["login"])
            credits.append(f"{c['name']}: https://twitch.tv/{c['login']}")
    desc = "\n".join([
        f"The most-watched {series_name} clips on Twitch this week ({span}), counted down to #1 by their Twitch "
        "view counts, with the live chat replay and how hard chat went for each moment.", "",
        *[f"{_clock(t)} {label}" for t, label in chapters], "",
        "Streamers in this episode (go follow them):", *credits, "",
        "A new episode every Sunday.", "",
        "#gta #gtarp #twitch #gta6" if series_name != "GTA 6" else "#gta6 #gtavi #gta #twitch"])
    return title, desc[:4900]


# ------------------------------------------------------------------ 5. build
def build(cfg, out_dir, test=False, ff=None):
    """Make the episode. Returns a dict with paths, title, description, clips. Raises if too few clips."""
    from .clipqueue import _ffmpeg
    ff = ff or _ffmpeg()
    s = settings(cfg)
    st = load()
    name, games = series(cfg)
    episode = int((st.get("series") or {}).get(name, {}).get("episode", 0)) + 1
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = top_clips(games)
    picked = pick(clips, dict(s, target_minutes=1.2, max_clips=3) if test else s)
    if len(picked) < (2 if test else 5):
        raise RuntimeError(f"only {len(picked)} usable clips this week in {', '.join(games)}")
    from zoneinfo import ZoneInfo
    end = now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC"))).date()
    span = f"{end - timedelta(days=6):%b %-d} to {end:%b %-d}"
    emotes = Emotes(out_dir / "emotes")
    segs, chapters, t = [], [], 0.0
    for c in picked:
        d = out_dir / f"r{c['rank']:02d}"
        d.mkdir(exist_ok=True)
        src = d / "clip.mp4"
        try:
            run(["yt-dlp", "-q", "--no-warnings", "-f", "best[ext=mp4]/best", "-o", str(src),
                 f"https://clips.twitch.tv/{c['slug']}"])
        except Exception as e:
            log.warning("couldn't download clip %s: %s", c["slug"], e)
            continue
        msgs = []
        if c["vid"] and c["offset"] is not None:
            try:
                msgs = chat_window(c["vid"], c["offset"] - 90, c["offset"] + c["duration"] + CHAT_DELAY + 2)
            except Exception as e:
                log.info("no chat for %s: %s", c["slug"], e)
        c["stats"] = stats(msgs, float(c["offset"] or 0), c["duration"])
        frame_at(src, min(c["duration"] / 2, 3), d / "frame.png", ff)
        c["frame"] = str(d / "frame.png")
        card = still(rank_card(c, d / "frame.png", d / "card.png"), 1.6, d / "card.mp4", ff)
        bg = background(c, c["stats"], name, d / "bg.png")
        chat_seq, meter_seq = panel_frames(msgs, float(c["offset"] or 0), c["duration"], c["stats"]["curve"], d, emotes)
        seg = segment(src, bg, chat_seq, meter_seq, c["duration"], d / "seg.mp4", ff)
        label = f"#{c['rank']} {c['name']}: {_clean(c['title'])[:60] or 'clip'}"
        chapters.append((t, label))
        segs += [card, seg]
        t += 1.6 + c["duration"]
    if len(chapters) < (2 if test else 5):
        raise RuntimeError("too few clips downloaded")
    top = next(c for c in sorted(picked, key=lambda c: c["rank"]) if c.get("frame"))   # #1, or the best we got
    intro = still(intro_card(name, episode, span, top["frame"], out_dir / "intro.png"), 3.5, out_dir / "intro.mp4", ff)
    outro = still(outro_card(name, episode, top["frame"], out_dir / "outro.png"), 4.0, out_dir / "outro.mp4", ff)
    # YouTube chapters: the first starts at 0:00 (it includes the intro) and each is at least 10 seconds
    chapters = [(0.0 if i == 0 else ct + 3.5, lab) for i, (ct, lab) in enumerate(chapters)]
    lst = out_dir / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in [intro, *segs, outro]))
    video = out_dir / f"weekly_{name.replace(' ', '').lower()}_{episode}.mp4"
    run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
         "-movflags", "+faststart", video])
    thumb = thumbnail(top, top["frame"], name, out_dir / f"thumb_{episode}.jpg")
    title, desc = texts(name, episode, span, chapters, picked)
    return {"video": video, "thumb": thumb, "title": title, "description": desc, "series": name,
            "episode": episode, "seconds": t + 7.5, "clips": [{k: c[k] for k in ("rank", "name", "title", "views")}
                                                              for c in sorted(picked, key=lambda c: c["rank"])]}


# ------------------------------------------------------------------ 6. the job
def email_text(ep, url):
    return "\n".join([
        f"This week's long video is ready: {ep['title']} ({_clock(ep['seconds'])} long, {len(ep['clips'])} clips).", "",
        "To post it:",
        f"1. Download the video: {url}",
        "2. Go to studio.youtube.com and click Create (top right) > Upload videos, then pick the file.",
        "3. Title: copy the TITLE block below.",
        "4. Description: copy the DESCRIPTION block below. The timestamps in it become chapters on their own.",
        "5. Thumbnail: click Upload file under Thumbnail and pick the attached image. (Custom thumbnails need a "
        "phone-verified account: studio.youtube.com > Settings > Channel > Feature eligibility.)",
        "6. Audience: No, it's not made for kids. Altered content: No.",
        "7. Visibility: Public, then Publish.", "",
        "=== TITLE ===", ep["title"], "",
        "=== DESCRIPTION ===", ep["description"], "",
        "The download link stays up for two weeks."])


def job():
    """Weekly job: build this week's episode, store it, email it. TEST_SOURCE=weekly-preview makes a 3-clip
    preview and publishes stills to the test-previews branch instead of emailing."""
    from . import mail, services
    from .common import load_config
    cfg = load_config()
    test = os.environ.get("TEST_SOURCE", "").startswith("weekly")
    st = load()
    if not test and not due(cfg, st) and os.environ.get("FORCE") != "1":
        log.info("weekly episode isn't due")
        return
    out = WORK / "weekly"
    shutil.rmtree(out, ignore_errors=True)
    try:
        ep = build(cfg, out, test=test)
    except Exception as e:
        if test:
            raise
        today = now_utc().date().isoformat()
        fail = st.get("fail") or {}
        n = int(fail.get("n", 0)) + 1 if fail.get("date") == today else 1
        st["fail"] = {"date": today, "n": n, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        save(st)
        if n == 1 and mail.configured():
            try:
                mail.send("Clip bot: this week's long video didn't build",
                          f"The weekly video failed ({type(e).__name__}: {str(e)[:300]}). The bot tries twice more "
                          "today. Nothing for you to do unless this email comes again next week.")
            except Exception as e2:
                log.warning("couldn't send the failure email: %s", e2)
        raise
    print(f"::notice::weekly episode built: {ep['title']} ({_clock(ep['seconds'])}, {len(ep['clips'])} clips)",
          flush=True)
    if test:
        prev = Path("test-output")
        (prev / "previews").mkdir(parents=True, exist_ok=True)
        from .clipqueue import _ffmpeg
        ff = _ffmpeg()
        for i, frac in enumerate((0.02, 0.3, 0.6, 0.9)):
            frame_at(ep["video"], ep["seconds"] * frac, prev / "previews" / f"weekly_{i}.jpg", ff)
        shutil.copy(ep["thumb"], prev / "previews" / "weekly_thumb.jpg")
        (prev / "summary.json").write_text(json.dumps({k: v for k, v in ep.items() if k not in ("video", "thumb")},
                                                      indent=1, default=str))
        return ep
    _, url = services.release_upload(ep["video"], ep["video"].name)
    mail.send(f"Weekly video ready: {ep['title']}", email_text(ep, url), attachment=ep["thumb"],
              attachment_name=ep["thumb"].name, maintype="image", subtype="jpeg")
    ser = st.setdefault("series", {}).setdefault(ep["series"], {})
    ser.update(episode=ep["episode"], last=now_utc().date().isoformat())
    st["last_made"] = now_utc().isoformat()
    st.pop("fail", None)
    hist = st.setdefault("episodes", [])
    hist.append({"series": ep["series"], "episode": ep["episode"], "title": ep["title"], "made": st["last_made"],
                 "asset": ep["video"].name, "clips": ep["clips"]})
    keep = int(settings(cfg)["keep_episodes"])
    for old in hist[:-keep]:   # old episode files come off the release after two weeks
        if old.get("asset") and not old.get("deleted"):
            try:
                rel = services.queue_release()
                for a in rel.get("assets") or []:
                    if a["name"] == old["asset"]:
                        services.release_delete(a["id"])
                old["deleted"] = True
            except Exception as e:
                log.info("couldn't delete old episode: %s", e)
    st["episodes"] = hist[-20:]
    save(st)
    print(f"::notice::emailed the weekly episode: {ep['title']}", flush=True)
    return ep
