"""GTA 6 launch day.

- A week before launch, one email walks Elie through setting up automatic posting (Blotato). Once the BLOTATO_API_KEY
  secret is added, the next posting check confirms the TikTok and YouTube connections by email (or says what's
  missing), so he never has to ask whether it worked.
- From launch day (local date) the bot switches on its own, with no config edit:
    * posts per day are tripled when automatic posting is ready (doubled while posting is still by email),
    * the queue holds proportionally more clips and posting times spread across the whole day,
    * the live watcher also watches the biggest English GTA 6 streams, not only the saved streamers,
    * with automatic posting ready, each slot's clip is posted through Blotato instead of emailed. If a Blotato
      post fails, that clip is emailed instead, so a slot is never lost.
  An email says when launch mode is on.
- If the date moves, reply `launch YYYY-MM-DD` to any bot email (or change `launch.date` in config.yaml).
"""
import os
from datetime import date, datetime, timedelta

from .common import log, now_utc

DEFAULTS = {"enabled": True, "date": "2026-11-19", "remind_days_before": 7, "remind_hour": 9,
            "multiplier": 3, "email_multiplier": 2, "auto_post": True, "watch_top": 4,
            "games": ["Grand Theft Auto VI", "Grand Theft Auto 6"]}


def settings(cfg):
    return dict(DEFAULTS, **(cfg.get("launch") or {}))


def _local_now(cfg):
    from zoneinfo import ZoneInfo
    return now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC")))


def launch_date(cfg, state=None):
    """The launch date: a date Elie replied with wins over config.yaml."""
    d = ((state or {}).get("launch") or {}).get("date") or settings(cfg)["date"]
    return date.fromisoformat(str(d))


def days_left(cfg, state=None):
    return (launch_date(cfg, state) - _local_now(cfg).date()).days


def launched(cfg, state=None):
    s = settings(cfg)
    return bool(s["enabled"]) and days_left(cfg, state) <= 0


def blotato_key():
    return bool(os.environ.get("BLOTATO_API_KEY"))


def auto_post(cfg, state=None):
    """Post through Blotato at each slot: launched, allowed in config, the key is set and its accounts checked out."""
    ok = ((state or {}).get("launch") or {}).get("blotato_ok")
    return launched(cfg, state) and bool(settings(cfg)["auto_post"]) and blotato_key() and bool(ok)


def apply(cfg, state=None):
    """Turn a config into its launch-mode version in place (called by load_config). Returns cfg."""
    import copy
    s = settings(cfg)
    base = cfg.setdefault("_launch_base", {k: copy.deepcopy(cfg.get(k) or {}) for k in ("posting", "queue", "live")})
    for k, v in base.items():   # start from the untouched settings, so applying twice never doubles twice
        cfg[k] = copy.deepcopy(v)
    cfg["_launch"] = {"on": launched(cfg, state), "auto": False, "multiplier": 1}
    if not cfg["_launch"]["on"]:
        return cfg
    auto = auto_post(cfg, state)
    mult = int(s["multiplier"] if auto else s["email_multiplier"])
    cfg["_launch"].update(auto=auto, multiplier=mult, watch_top=int(s["watch_top"]), games=list(s["games"]))
    p = cfg.setdefault("posting", {})
    p["ramp"] = [dict(r, per_day=int(r["per_day"]) * mult) for r in p.get("ramp") or [{"from_day": 0, "per_day": 1}]]
    q = cfg.setdefault("queue", {})
    q["max_size"] = int(q.get("max_size", 5)) * mult
    most = max(r["per_day"] for r in p["ramp"])
    tt = dict(p.get("time_test") or {})
    tt["candidates"] = [f"{h:02d}:00" for h in range(7, 24)]   # every hour from 7am to 11pm
    tt["min_gap_hours"] = max(1, min(int(tt.get("min_gap_hours", 3)), 16 // max(1, most)))
    p["time_test"] = tt
    live = cfg.setdefault("live", {})
    live["extra_posts_per_day"] = int(live.get("extra_posts_per_day", 1)) * mult
    return cfg


# ------------------------------------------------------------------ emails
REMINDER_SUBJECT = "Clip bot: GTA 6 is {days} days out, set up automatic posting"


def reminder_text(cfg, state):
    d = launch_date(cfg, state)
    s = settings(cfg)
    return "\n".join([
        f"GTA 6 comes out {d:%A, %B %-d}. On that day the bot switches to launch mode on its own:",
        f"- {s['multiplier']}x the posts per day once automatic posting is set up "
        f"({s['email_multiplier']}x if posting is still by email)",
        "- it also watches the biggest English GTA 6 streams live, not only your saved streamers",
        "- with automatic posting, each clip goes straight to TikTok and YouTube at its time, no email needed",
        "",
        "Set up automatic posting now (about 10 minutes):",
        "1. Go to blotato.com and sign up (the $29/month plan).",
        "2. In Blotato, open Accounts (left menu) > Connect account > TikTok, and log in to @wantedlevelclips8.",
        "3. Connect account again > YouTube, and pick the Wanted Level channel.",
        "4. In Blotato, open Settings > API, click Generate API key, and copy it.",
        "5. On GitHub, open the gta-clipper repo > Settings > Secrets and variables > Actions.",
        "6. Click New repository secret. Name: BLOTATO_API_KEY. Secret: paste the key. Click Add secret.",
        "",
        "That's it. Within about 15 minutes you'll get an email saying whether both accounts are connected. "
        "Until launch day nothing changes: you keep getting the posting emails like now.",
        "",
        "If the release date moves, reply to this email with the new date, like: launch 2026-12-03",
    ])


def _send(state, key, subject, text):
    from . import mail
    if not mail.configured():
        return False
    try:
        mail.send(subject, text)
    except Exception as e:
        log.warning("launch email failed (%s); will try again next run", e)
        return False
    state.setdefault("launch", {})[key] = now_utc().isoformat()
    print(f"::notice::emailed: {subject}", flush=True)
    return True


def blotato_check():
    """(ok, message) about the Blotato connections. Never raises."""
    from . import services
    try:
        accts = services.blotato_accounts()
    except Exception as e:
        return False, f"Blotato didn't accept the API key ({str(e)[:150]}). Copy it again and update the secret."
    have = {a.get("platform") for a in accts or []}
    missing = [p for p in ("tiktok", "youtube") if p not in have]
    if missing:
        return False, ("Blotato is connected, but these accounts aren't linked yet: " + ", ".join(missing)
                       + ". In Blotato, open Accounts > Connect account and add them.")
    return True, "Blotato is connected to TikTok and YouTube."


def daily(cfg, state, dry_run=False):
    """Run from every posting check: the week-before reminder, the Blotato confirmation, the launch-mode email."""
    s = settings(cfg)
    if not s["enabled"] or dry_run:
        return
    rec = state.setdefault("launch", {})
    left = days_left(cfg, state)
    local = _local_now(cfg)
    if 0 < left <= s["remind_days_before"] and local.hour >= s["remind_hour"] and not rec.get("reminded"):
        _send(state, "reminded", REMINDER_SUBJECT.format(days=left), reminder_text(cfg, state))
    # Once the Blotato key shows up, confirm (or say what's missing) at most once a day until it works.
    if blotato_key() and not rec.get("blotato_ok"):
        last = rec.get("blotato_checked")
        if not last or now_utc() - datetime.fromisoformat(last) > timedelta(hours=20):
            ok, msg = blotato_check()
            rec["blotato_checked"] = now_utc().isoformat()
            when = (f"on launch day ({launch_date(cfg, state):%B %-d})" if left > 0 else "now")
            if ok:
                rec["blotato_ok"] = now_utc().isoformat()
                _send(state, "blotato_ok_mail", "Clip bot: automatic posting is ready",
                      f"{msg}\n\nThe bot starts posting for you {when}. Until then the posting emails continue.")
            else:
                _send(state, "blotato_bad_mail", "Clip bot: automatic posting isn't connected yet",
                      f"{msg}\n\nThe bot checks again tomorrow. Posting emails continue in the meantime.")
    if cfg.get("_launch", {}).get("on") and not rec.get("started"):
        L = cfg["_launch"]
        per = max(r["per_day"] for r in cfg["posting"]["ramp"])
        how = ("posted for you automatically through Blotato" if L["auto"] else
               "still emailed to you (automatic posting isn't set up, so it's doubled instead of tripled)")
        _send(state, "started", "Clip bot: GTA 6 launch mode is on",
              f"GTA 6 is out. The bot now aims for {per} posts a day per platform ({L['multiplier']}x), "
              f"{how}. It's also watching the {L['watch_top']} biggest English GTA 6 streams live.\n\n"
              "Reply pause to any bot email to stop posting, resume to start again.")


# ------------------------------------------------------------------ live: the biggest GTA 6 streams
def find_categories(query="grand theft auto"):
    """Twitch category names that look like GTA 6 (in case it isn't named exactly as expected)."""
    import re
    from . import twitch
    q = 'query { searchCategories(query: "%s", first: 25) { edges { node { name } } } }' % query
    edges = ((((twitch._gql({"query": q}) or {}).get("data") or {}).get("searchCategories") or {}).get("edges")) or []
    names = [((e.get("node") or {}).get("name") or "") for e in edges]
    return [nm for nm in names if re.search(r"\b(vi|6)\b", nm, re.I) and re.search(r"grand theft|gta", nm, re.I)]


def top_streams(cfg, n=None, exclude=()):
    """Logins of the biggest English GTA 6 streams with a recording VOD (so they can be clipped live)."""
    from . import twitch
    L = cfg.get("_launch") or {}
    n = n or L.get("watch_top") or 0
    if not n:
        return []
    out = []
    games = list(L.get("games") or DEFAULTS["games"])
    try:
        games += [g for g in find_categories() if g not in games]
    except Exception as e:
        log.info("category search failed: %s", e)
    for game in games:
        q = ('query { game(name: "%s") { streams(first: 30) { edges { node { viewersCount broadcaster { login '
             'broadcastSettings { language } videos(first: 1, type: ARCHIVE, sort: TIME) { edges { node { status } } } '
             '} } } } } }' % game)
        try:
            data = twitch._gql({"query": q}) or {}
        except Exception as e:
            log.info("GTA 6 stream list failed for %s: %s", game, e)
            continue
        edges = ((((data.get("data") or {}).get("game") or {}).get("streams") or {}).get("edges")) or []
        for e in edges:
            b = (e.get("node") or {}).get("broadcaster") or {}
            lang = ((b.get("broadcastSettings") or {}).get("language") or "EN").upper()
            vids = (b.get("videos") or {}).get("edges") or []
            login = (b.get("login") or "").lower()
            if (lang == "EN" and login and login not in exclude and login not in out
                    and vids and (vids[0].get("node") or {}).get("status") == "RECORDING"):
                out.append(login)
            if len(out) >= n:
                return out
        if out:
            break
    return out
