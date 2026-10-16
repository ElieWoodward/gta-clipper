"""Posting-time experiment: which time of day gets the most views fastest.

Each day's posting times are picked from a list of candidate times. Times that haven't been tried enough go first;
once every time has a few results, most slots use the best times and one slot keeps testing. A post's result is its
TikTok views a few hours after it went up (measured from the view history the hourly check records), so a clip that
was posted at 18:05 counts for the 18:00 candidate no matter when its email went out.
"""
import statistics
from datetime import datetime, timedelta

from .common import now_utc

DEFAULTS = {"enabled": True, "candidates": ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"],
            "measure_hours": 3, "min_samples": 2, "min_gap_hours": 3, "platform": "tiktok"}


def settings(cfg):
    return dict(DEFAULTS, **((cfg.get("posting") or {}).get("time_test") or {}))


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def views_at(post, hours):
    """Views `hours` after posting, interpolated from the recorded history. None if it's too early to tell."""
    if not post.get("posted_at") or not post.get("history"):
        return None
    t0 = datetime.fromisoformat(post["posted_at"])
    target = t0 + timedelta(hours=hours)
    pts = [(datetime.fromisoformat(t), v) for t, v in post["history"]]
    pts = [(t0, 0)] + sorted(pts)
    if pts[-1][0] < target:
        return None   # not old enough yet
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        if ta <= target <= tb:
            span = (tb - ta).total_seconds()
            return va if span <= 0 else va + (vb - va) * (target - ta).total_seconds() / span
    return pts[-1][1]


def nearest(cands, hhmm):
    m = _minutes(hhmm)
    return min(cands, key=lambda c: min(abs(_minutes(c) - m), 1440 - abs(_minutes(c) - m)))


def results(cfg, state, tz):
    """{candidate time: [views at measure_hours, ...]} from posts so far."""
    from zoneinfo import ZoneInfo
    s = settings(cfg)
    out = {c: [] for c in s["candidates"]}
    for c in state["clips"].values():
        p = (c.get("posts") or {}).get(s["platform"])
        if not p or not p.get("posted_at"):
            continue
        v = views_at(p, s["measure_hours"])
        if v is None:
            continue
        local = datetime.fromisoformat(p["posted_at"]).astimezone(ZoneInfo(tz)).strftime("%H:%M")
        out[nearest(s["candidates"], local)].append(v)
    return out


def _spaced(chosen, cand, gap):
    return all(min(abs(_minutes(cand) - _minutes(x)), 1440 - abs(_minutes(cand) - _minutes(x))) >= gap * 60
               for x in chosen)


def plan(cfg, state, day, n, tz):
    """The posting times for a local date (decided once, then kept so every run that day agrees)."""
    s = settings(cfg)
    book = state.setdefault("slot_plan", {})
    if day in book and len(book[day]) >= n:
        return sorted(book[day][:n])
    if n <= 0:
        return []
    res = results(cfg, state, tz)
    cands = s["candidates"]
    # untested times first, rotated by date so the test covers the whole day
    offset = datetime.fromisoformat(day).toordinal() % len(cands)
    untested = sorted([c for c in cands if len(res[c]) < s["min_samples"]],
                      key=lambda c: (len(res[c]), (cands.index(c) - offset) % len(cands)))
    ranked = sorted([c for c in cands if c not in untested], key=lambda c: -statistics.median(res[c]))
    chosen = list(book.get(day, []))
    if untested:
        order = untested + ranked
    else:
        # every time has results: use the best ones, but keep one slot testing the least-tried other time
        others = sorted(ranked[1:], key=lambda c: len(res[c]))
        order = (ranked[: n - 1] + others[:1] + ranked) if n >= 2 else ranked
    for c in order:
        if len(chosen) >= n:
            break
        if c not in chosen and _spaced(chosen, c, s["min_gap_hours"]):
            chosen.append(c)
    for c in cands:   # fall back if spacing made it impossible
        if len(chosen) >= n:
            break
        if c not in chosen:
            chosen.append(c)
    book[day] = sorted(chosen)
    for old in sorted(book)[:-14]:
        book.pop(old)
    return sorted(chosen)


def report(cfg, state, tz):
    """Lines for the weekly email: how each posting time is doing."""
    s = settings(cfg)
    res = results(cfg, state, tz)
    lines = []
    for c in sorted(res, key=lambda c: -(statistics.median(res[c]) if res[c] else -1)):
        v = res[c]
        if v:
            lines.append(f"- {c}: {statistics.median(v):,.0f} views after {s['measure_hours']}h "
                         f"(median of {len(v)} post{'s' if len(v) != 1 else ''})")
        else:
            lines.append(f"- {c}: not tested yet")
    return lines


def today_local(cfg):
    from zoneinfo import ZoneInfo
    return now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC"))).date().isoformat()
