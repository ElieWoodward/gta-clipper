"""Signals from Twitch itself: the moments viewers clipped, and where chat burst out laughing.

Both come from Twitch's public web API (the same one twitch.tv uses). If Twitch changes it, every function
here returns empty and the picker falls back to loudness alone, so a failure never stops a run.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from .common import log

GQL = "https://gql.twitch.tv/gql"
HEADERS = {"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko", "Content-Type": "text/plain;charset=UTF-8",
           "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
CHAT_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
CHAT_DELAY = 6          # chat reacts a few seconds after the moment happens on stream
# Laughs plus shock reactions ("wtf", "ayo"): both mean something just happened worth clipping.
LAUGH = re.compile(r"\b(lul|lulw|kekw|kek|omegalul|icant|lmao+|lmfao|lol+|haha+|xd+|pepelaugh|kekl|omegakek|"
                   r"bruh+|wtf|ayo+|no shot)\b|😂|🤣|💀|😭", re.I)


def _gql(body, timeout=15):
    for attempt in range(3):
        try:
            r = requests.post(GQL, json=body, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            log.info("twitch gql %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.info("twitch gql error: %s", e)
        time.sleep(1 + attempt * 2)
    return None


def video_id(vod):
    """Numeric Twitch video id for a VOD dict, or None if it isn't a Twitch VOD."""
    for s in (vod.get("url") or "", vod.get("id") or ""):
        m = re.search(r"(?:videos/|twitch:v?|^v)(\d{6,})", s)
        if m:
            return m.group(1)
    return None


def video_info(vid):
    data = _gql({"query": 'query { video(id: "%s") { id lengthSeconds createdAt owner { login } } }' % vid})
    v = ((data or {}).get("data") or {}).get("video")
    return v or None


def viewer_clips(login, vid, period="LAST_MONTH", pages=5):
    """Clips viewers made from this VOD: [{offset, duration, views, title}] sorted by views."""
    out, after = [], None
    for _ in range(pages):
        q = ('query { user(login: "%s") { clips(first: 100%s, criteria: {period: %s, sort: VIEWS_DESC}) '
             '{ pageInfo { hasNextPage } edges { cursor node { title viewCount durationSeconds videoOffsetSeconds '
             'video { id } } } } } }') % (login, f', after: "{after}"' if after else "", period)
        data = _gql({"query": q})
        clips = ((((data or {}).get("data") or {}).get("user") or {}).get("clips")) or {}
        edges = clips.get("edges") or []
        for e in edges:
            n = e.get("node") or {}
            if str((n.get("video") or {}).get("id")) == str(vid) and n.get("videoOffsetSeconds") is not None:
                out.append({"offset": float(n["videoOffsetSeconds"]), "duration": float(n.get("durationSeconds") or 30),
                            "views": int(n.get("viewCount") or 0), "title": (n.get("title") or "").strip()})
        if not edges or not (clips.get("pageInfo") or {}).get("hasNextPage"):
            break
        after = edges[-1].get("cursor")
        if not after:
            break
    return sorted(out, key=lambda c: -c["views"])


def _chat_part(vid, start, stop, max_pages=400):
    """Messages with offset in [start, stop): [(second, text)]."""
    msgs, cursor = [], None
    for _ in range(max_pages):
        var = {"videoID": vid, "cursor": cursor} if cursor else {"videoID": vid, "contentOffsetSeconds": int(start)}
        data = _gql({"operationName": "VideoCommentsByOffsetOrCursor", "variables": var,
                     "extensions": {"persistedQuery": {"version": 1, "sha256Hash": CHAT_HASH}}})
        if isinstance(data, list):
            data = data[0] if data else None
        com = ((((data or {}).get("data") or {}).get("video") or {}).get("comments")) or {}
        edges = com.get("edges") or []
        if not edges:
            break
        last = start
        for e in edges:
            n = e.get("node") or {}
            t = n.get("contentOffsetSeconds")
            if t is None:
                continue
            last = t
            if start <= t < stop:
                text = "".join(f.get("text") or "" for f in ((n.get("message") or {}).get("fragments") or []))
                msgs.append((int(t), text.strip()))
        if last >= stop or not (com.get("pageInfo") or {}).get("hasNextPage"):
            break
        cursor = edges[-1].get("cursor")
        if not cursor:
            break
    return msgs


def chat_replay(vid, length, part=1800, workers=8):
    """All chat messages of a VOD, fetched in parallel slices. [(second, text)]"""
    slices = [(s, min(length, s + part)) for s in range(0, int(length), part)]
    with ThreadPoolExecutor(workers) as ex:
        parts = list(ex.map(lambda sl: _chat_part(vid, *sl), slices))
    return sorted(m for p in parts for m in p)


def laugh_curve(msgs, length):
    """Per-second laugh count and message count, shifted back by the chat delay."""
    laughs, total = np.zeros(int(length) + 1), np.zeros(int(length) + 1)
    for t, text in msgs:
        i = max(0, min(int(length), t - CHAT_DELAY))
        total[i] += 1
        if LAUGH.search(text):
            laughs[i] += 1
    return laughs, total


def chat_peaks(laughs, total, n, min_gap=60):
    """Seconds where chat laughs (or explodes) far above its normal rate. [(second, strength)]"""
    if total.sum() < 50:
        return []
    k = 10
    lw = np.convolve(laughs, np.ones(k), mode="same")
    tw = np.convolve(total, np.ones(k), mode="same")
    base_l = max(1.0, float(np.median(lw[tw > 0])) if (tw > 0).any() else 1.0)
    base_t = max(1.0, float(np.median(tw[tw > 0])) if (tw > 0).any() else 1.0)
    strength = lw / base_l + 0.5 * (tw / base_t)
    picks = []
    for t in np.argsort(strength)[::-1]:
        if strength[t] < 4:
            break
        if all(abs(t - p) >= min_gap for p, _ in picks):
            picks.append((int(t), float(strength[t])))
        if len(picks) >= n:
            break
    return picks


def chat_sample(msgs, s, e, k=10):
    """A few real chat lines from a window (skipping bare emotes), so the judge sees how chat reacted."""
    lines, seen = [], set()
    for t, text in msgs:
        if s + CHAT_DELAY <= t < e + CHAT_DELAY and len(text) > 3 and text.lower() not in seen:
            seen.add(text.lower())
            lines.append(text[:80])
    if len(lines) > k:
        step = len(lines) / k
        lines = [lines[int(i * step)] for i in range(k)]
    return lines


def signals(vod, length):
    """Everything Twitch can tell us about a VOD. Missing pieces come back empty."""
    out = {"clips": [], "msgs": [], "laughs": None, "total": None}
    vid = video_id(vod)
    if not vid:
        return out
    t0 = time.time()
    info = video_info(vid)
    login = (info or {}).get("owner", {}).get("login") or vod.get("streamer", {}).get("channel")
    if login:
        try:
            out["clips"] = viewer_clips(login, vid)
        except Exception as e:
            log.warning("viewer clips failed: %s", e)
    try:
        out["msgs"] = chat_replay(vid, length)
        if out["msgs"]:
            out["laughs"], out["total"] = laugh_curve(out["msgs"], length)
    except Exception as e:
        log.warning("chat replay failed: %s", e)
    print(f"::notice::twitch signals: {len(out['clips'])} viewer clips, {len(out['msgs'])} chat messages "
          f"({time.time() - t0:.0f}s)", flush=True)
    return out
