"""Jellyfin clean-up on the Mac: titles disappear a few days after they've been watched, so the drive never fills.

Once a day (mac.py decides when):
- a movie or episode watched JELLYFIN_DELETE_DAYS (default 3) or more days ago is deleted through Jellyfin, which
  removes the file too. Radarr/Sonarr see it's gone (with "Unmonitor deleted" on, they don't download it again).
- it happens silently. Set JELLYFIN_NOTIFY=1 in .env to get a heads-up the day before (with a Keep button) and a
  note of what was cleared.
- favorites (the heart in Jellyfin) are never deleted, and neither is any episode of a favorited show.
Settings in the Mac's .env: JELLYFIN_API_KEY (Jellyfin > Dashboard > API Keys > +), optional JELLYFIN_URL
(default http://localhost:8096), JELLYFIN_USER (whose "watched" counts; default the first admin), JELLYFIN_DELETE_DAYS.
No Claude calls.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

from .common import log


def configured():
    return bool(os.environ.get("JELLYFIN_API_KEY"))


def _url(path):
    return os.environ.get("JELLYFIN_URL", "http://localhost:8096").rstrip("/") + path


def _hdr():
    key = os.environ["JELLYFIN_API_KEY"]
    return {"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key}


def _req(method, path, **kw):
    r = requests.request(method, _url(path), headers=_hdr(), timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Jellyfin {method} {path} failed ({r.status_code}): {r.text[:200]}")
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else None


def user_id():
    users = _req("GET", "/Users") or []
    want = (os.environ.get("JELLYFIN_USER") or "").lower()
    for u in users:
        if want and u.get("Name", "").lower() == want:
            return u["Id"]
    admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
    if not (admins or users):
        raise RuntimeError("Jellyfin has no users yet")
    return (admins or users)[0]["Id"]


def watched_items(uid):
    q = {"IncludeItemTypes": "Movie,Episode", "Recursive": "true", "Filters": "IsPlayed",
         "Fields": "Path,SeriesId", "EnableUserData": "true", "userId": uid}
    return (_req("GET", f"/Users/{uid}/Items", params=q) or {}).get("Items") or []


def favorite_series(uid):
    q = {"IncludeItemTypes": "Series", "Recursive": "true", "Filters": "IsFavorite", "userId": uid}
    return {i["Id"] for i in (_req("GET", f"/Users/{uid}/Items", params=q) or {}).get("Items") or []}


def _label(item):
    if item.get("Type") == "Episode":
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        code = f" S{s:02d}E{e:02d}" if isinstance(s, int) and isinstance(e, int) else ""
        return f"{item.get('SeriesName') or 'Show'}{code}"
    return f"{item.get('Name')}{(' (' + str(item['ProductionYear']) + ')') if item.get('ProductionYear') else ''}"


def _when(text):
    """Jellyfin dates look like 2026-11-04T10:22:33.1234567Z (7-digit fraction, Z): parse to aware UTC."""
    t = text.strip().replace("Z", "")
    tz = ""
    for sign in ("+", "-"):
        i = t.rfind(sign)
        if i > 18:
            t, tz = t[:i], t[i:]
            break
    if "." in t:
        base, frac = t.split(".", 1)
        t = f"{base}.{frac[:6].ljust(6, '0')}"
    return datetime.fromisoformat(t + (tz or "+00:00"))


def plan(items, fav_series, now=None, days=3):
    """(to_delete, to_warn): watched days+ ago and not kept -> delete; watched days-1 ago -> warn once."""
    now = now or datetime.now(timezone.utc)
    delete, warn = [], []
    for it in items:
        ud = it.get("UserData") or {}
        if not ud.get("Played") or ud.get("IsFavorite") or it.get("SeriesId") in fav_series:
            continue
        last = ud.get("LastPlayedDate")
        if not last:
            continue
        age = now - _when(last)
        if age >= timedelta(days=days):
            delete.append(it)
        elif age >= timedelta(days=days - 1):
            warn.append(it)
    return delete, warn


def keep(item_id):
    """Favorite an item so it's never deleted (newer Jellyfin route first, then the older one)."""
    uid = user_id()
    try:
        _req("POST", f"/UserFavoriteItems/{item_id}", params={"userId": uid})
    except RuntimeError:
        _req("POST", f"/Users/{uid}/FavoriteItems/{item_id}")


def run(st, send, days=None, notify=None):
    """One clean-up pass. send(text, rows) delivers Telegram messages (only with notify / JELLYFIN_NOTIFY=1).
    Returns (deleted labels, warned labels)."""
    days = int(days or os.environ.get("JELLYFIN_DELETE_DAYS") or 3)
    if notify is None:
        notify = os.environ.get("JELLYFIN_NOTIFY") == "1"
    uid = user_id()
    delete, warn = plan(watched_items(uid), favorite_series(uid), days=days)
    warned = st.setdefault("jf_warned", [])
    done, told = [], []
    for it in delete:
        try:
            _req("DELETE", f"/Items/{it['Id']}")
            done.append(_label(it))
        except Exception as e:
            log.warning("couldn't delete %s: %s", _label(it), e)
    for it in (warn if notify else []):
        if it["Id"] in warned:
            continue
        warned.append(it["Id"])
        told.append(_label(it))
        send(f"🗑 {_label(it)} gets deleted tomorrow (watched {days - 1} days ago). Tap Keep to hold on to it.",
             [[("Keep it", f"jf:keep:{it['Id']}")]])
    del warned[:-500]
    if done:
        log.info("cleared %d watched title(s): %s", len(done), ", ".join(done))
    if done and notify:
        send("🧹 Cleared " + str(len(done)) + f" watched title{'s' if len(done) > 1 else ''} to free up space:\n" +
             "\n".join(f"• {d}" for d in done), None)
    return done, told
