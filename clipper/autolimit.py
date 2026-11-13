"""Posts a day, chosen by the channel's own numbers instead of a fixed schedule (posting.auto_limit).

Once a day (first post run after 6 AM) the bot compares the YouTube Shorts posted at the current posts-a-day level
with the ones from the level before, each measured at the same age (views after measure_hours, plus likes per view):

  much better (2x the views, likes holding up)   -> up 2
  better (1.2x)                                  -> up 1
  about the same, for 3+ days                    -> up 1 (try a little more; it comes back down if that hurts)
  worse (under 0.8x the views, or likes per view under 0.75x)  -> down 1
  dead (median under crash_views)                -> straight down to 3 (what got the channel seen again)

It never goes below `min` or above `max`, and it only judges a level once at least 3 of its posts are old enough to
measure, so a change always has its own numbers behind it. Why each change happened goes in the day summary.
"""
import json
import statistics
from datetime import datetime, timedelta

from .common import DATA, log, now_utc

FILE = DATA / "auto_limit.json"
DEFAULTS = {"enabled": False, "min": 2, "max": 8, "measure_hours": 24, "up": 1.2, "big_up": 2.0, "down": 0.8,
            "like_drop": 0.75, "crash_views": 50, "steady_days": 3}


def settings(cfg):
    return dict(DEFAULTS, **((cfg.get("posting") or {}).get("auto_limit") or {}))


def on(cfg):
    import os
    return bool(settings(cfg)["enabled"]) and not os.environ.get("AUTO_LIMIT_OFF")


def load():
    try:
        return json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(d):
    FILE.write_text(json.dumps(d, indent=1))


def level(cfg):
    """Today's posts a day, or None when auto_limit is off or hasn't decided anything yet."""
    if not on(cfg):
        return None
    d = load()
    if not d.get("level"):
        return None
    s = settings(cfg)
    return max(int(s["min"]), min(int(s["max"]), int(d["level"])))


def measured(cfg, clips, now):
    """[(posted_at, views at measure_hours, likes per view or None)] for YouTube posts old enough to measure."""
    from .timing import views_at
    h = int(settings(cfg)["measure_hours"])
    out = []
    for c in clips.values():
        p = (c.get("posts") or {}).get("youtube") or {}
        if not p.get("posted_at"):
            continue
        try:
            t = datetime.fromisoformat(p["posted_at"])
        except ValueError:
            continue
        if now - t < timedelta(hours=h):
            continue
        v = views_at(p, h)
        if v is None:   # no view history yet: the current count is the best there is once it's old enough
            v = p.get("views")
        if v is None:
            continue
        lr = (p.get("likes") / max(1, p["views"])) if p.get("likes") is not None and p.get("views") else None
        out.append((t, float(v), lr))
    return out


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def decide(cfg, clips, start_level, now=None):
    """The day's decision. Returns {"level", "old", "change", "why"} and saves it."""
    s = settings(cfg)
    now = now or now_utc()
    d = load()
    if not d.get("level"):
        d = {"level": int(start_level), "since": now.isoformat(), "prev_median": None, "prev_likes": None, "log": []}
    lo, hi = int(s["min"]), int(s["max"])
    old = max(lo, min(hi, int(d["level"])))
    since = datetime.fromisoformat(d["since"])
    posts = measured(cfg, clips, now)
    cur = [p for p in posts if p[0] >= since]
    if d.get("prev_median") is None:   # first run: the few days before this level are what it's compared with
        before = [p for p in posts if since - timedelta(days=3) <= p[0] < since]
        if before:
            d["prev_median"], d["prev_likes"] = _median([p[1] for p in before]), _median([p[2] for p in before])
    new, why = old, None
    if len(cur) < 3:
        why = f"holding at {old}: waiting for {3 - len(cur)} more post(s) at this level to be {s['measure_hours']}h old"
    else:
        med, likes = _median([p[1] for p in cur]), _median([p[2] for p in cur])
        prev, prev_l = d.get("prev_median"), d.get("prev_likes")
        r = (med / prev) if prev else None
        lr = (likes / prev_l) if (likes is not None and prev_l) else None
        days = (now - since).total_seconds() / 86400
        if med < float(s["crash_views"]):
            new, why = min(old, max(lo, 3)), f"Shorts are getting almost nothing ({med:,.0f} median views)"
            if old <= 3:
                new = max(lo, old - 1)
        elif r is None:
            why = f"holding at {old}: nothing earlier to compare with yet"
        elif lr is not None and lr < float(s["like_drop"]):
            new, why = old - 1, f"likes per view fell to {lr:.0%} of before"
        elif r < float(s["down"]):
            new, why = old - 1, f"views per Short fell to {r:.0%} of before ({med:,.0f} vs {prev:,.0f})"
        elif r >= float(s["big_up"]):
            new, why = old + 2, f"views per Short are {r:.1f}x before ({med:,.0f} vs {prev:,.0f})"
        elif r >= float(s["up"]):
            new, why = old + 1, f"views per Short are {r:.1f}x before ({med:,.0f} vs {prev:,.0f})"
        elif days >= float(s["steady_days"]):
            new, why = old + 1, (f"views held steady at {old} a day for {days:.0f} days ({med:,.0f} per Short), "
                                 "so trying one more")
        else:
            why = f"holding at {old}: views about the same as before ({med:,.0f} vs {prev:,.0f})"
        new = max(lo, min(hi, new))
        if new != old:
            d.update(prev_median=med, prev_likes=likes, since=now.isoformat())
        elif new == hi and why and not why.startswith("holding"):
            why = f"holding at the top ({hi}): " + why
    d["level"] = new
    entry = {"at": now.isoformat(), "old": old, "level": new, "why": why}
    d["log"] = (d.get("log") or [])[-60:] + [entry]
    save(d)
    log.info("posts a day: %s -> %s (%s)", old, new, why)
    return dict(entry, change=new - old)


def today_note(now=None):
    """One line for the day summary: the latest decision, if it was made in the last day."""
    now = now or now_utc()
    for e in reversed(load().get("log") or []):
        if now - datetime.fromisoformat(e["at"]) > timedelta(hours=24):
            return None
        if e["level"] > e["old"]:
            return f"Posts a day: {e['level']} (up from {e['old']}) because {e['why']}"
        if e["level"] < e["old"]:
            return f"Posts a day: {e['level']} (down from {e['old']}) because {e['why']}"
        return f"Posts a day: {e['level']}, {e['why']}" if not e["why"].startswith("holding") else \
            f"Posts a day: {e['why']}"
    return None
