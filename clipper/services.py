"""Thin clients for Blotato (posting + analytics), GitHub (issues as the dashboard) and YouTube (hiding videos)."""
import os
import time

import requests

from .common import log, require_env

# ---------------- Blotato ----------------
BLOTATO = "https://backend.blotato.com/v2"


def _bh():
    return {"blotato-api-key": require_env("BLOTATO_API_KEY"), "Content-Type": "application/json"}


def blotato_accounts():
    r = requests.get(f"{BLOTATO}/users/me/accounts", headers=_bh(), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("items", data) if isinstance(data, dict) else data


def blotato_account_id(platform):
    env = os.environ.get(f"BLOTATO_{platform.upper()}_ACCOUNT_ID")
    if env:
        return env
    for a in blotato_accounts():
        if a.get("platform") == platform:
            return str(a["id"])
    raise RuntimeError(f"No {platform} account connected in Blotato (Accounts > Connect account).")


def blotato_upload(path):
    """Upload a local file; returns its public URL."""
    r = requests.post(f"{BLOTATO}/media/uploads", headers=_bh(), json={"filename": os.path.basename(path)}, timeout=30)
    r.raise_for_status()
    up = r.json()
    with open(path, "rb") as fh:
        p = requests.put(up["presignedUrl"], data=fh, headers={"Content-Type": "video/mp4"}, timeout=600)
    p.raise_for_status()
    return up["publicUrl"]


def blotato_post(platform, account_id, media_url, text, title=None, ai_label=False):
    if platform == "tiktok":
        target = {"targetType": "tiktok", "privacyLevel": "PUBLIC_TO_EVERYONE", "disabledComments": False,
                  "disabledDuet": False, "disabledStitch": False, "isBrandedContent": False,
                  "isYourBrand": False, "isAiGenerated": bool(ai_label)}
    elif platform == "youtube":
        target = {"targetType": "youtube", "title": (title or text)[:100], "privacyStatus": "public",
                  "shouldNotifySubscribers": True, "isMadeForKids": False,
                  "containsSyntheticMedia": bool(ai_label)}
    else:
        raise ValueError(platform)
    body = {"post": {"accountId": account_id, "content": {"text": text, "mediaUrls": [media_url], "platform": platform},
                     "target": target}}
    r = requests.post(f"{BLOTATO}/posts", headers=_bh(), json=body, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Blotato {platform} post rejected: {r.status_code} {r.text[:500]}")
    return r.json()["postSubmissionId"]


def blotato_wait(submission_id, limit=180):
    """Poll until published/failed. Returns (status, url_or_error)."""
    t0 = time.time()
    while time.time() - t0 < limit:
        r = requests.get(f"{BLOTATO}/posts/{submission_id}", headers=_bh(), timeout=30)
        r.raise_for_status()
        d = r.json()
        st = d.get("status")
        if st == "published":
            return st, d.get("publicUrl")
        if st == "failed":
            return st, d.get("errorMessage")
        time.sleep(10)
    return "in-progress", None


def blotato_published(since_iso):
    """All published posts since a date, with their latest analytics."""
    items, cursor = [], None
    while True:
        params = {"since": since_iso, "limit": 250, "status": "published"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BLOTATO}/posts", headers=_bh(), params=params, timeout=60)
        r.raise_for_status()
        d = r.json()
        items += d.get("items", [])
        cursor = d.get("cursor")
        if not cursor:
            return items


# ---------------- GitHub issues (your dashboard) ----------------
GH = "https://api.github.com"


def _gh():
    return {"Authorization": f"Bearer {require_env('GITHUB_TOKEN')}", "Accept": "application/vnd.github+json"}


def _repo():
    return require_env("GITHUB_REPOSITORY")


def gh_issue(title, body, labels):
    r = requests.post(f"{GH}/repos/{_repo()}/issues", headers=_gh(),
                      json={"title": title[:250], "body": body, "labels": labels}, timeout=30)
    r.raise_for_status()
    return r.json()["number"]


def gh_update(number, **fields):
    r = requests.patch(f"{GH}/repos/{_repo()}/issues/{number}", headers=_gh(), json=fields, timeout=30)
    r.raise_for_status()


def gh_comment(number, body):
    requests.post(f"{GH}/repos/{_repo()}/issues/{number}/comments", headers=_gh(), json={"body": body}, timeout=30)


def gh_get(number):
    r = requests.get(f"{GH}/repos/{_repo()}/issues/{number}", headers=_gh(), timeout=30)
    r.raise_for_status()
    return r.json()


def gh_find_open(label):
    r = requests.get(f"{GH}/repos/{_repo()}/issues", headers=_gh(),
                     params={"labels": label, "state": "open", "per_page": 100}, timeout=30)
    r.raise_for_status()
    return r.json()


def gh_upsert_pinned(label, title, body):
    """Keep one open issue per label (weekly report, prune list) and overwrite its body."""
    found = gh_find_open(label)
    if found:
        gh_update(found[0]["number"], body=body, title=title)
        return found[0]["number"]
    return gh_issue(title, body, [label])


# ---------------- YouTube (optional, for hiding low performers) ----------------
def youtube_set_private(video_id):
    cid, sec, ref = (os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and sec and ref):
        return False
    tok = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": sec, "refresh_token": ref, "grant_type": "refresh_token"}, timeout=30)
    tok.raise_for_status()
    r = requests.put("https://www.googleapis.com/youtube/v3/videos", params={"part": "status"},
                     headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
                     json={"id": video_id, "status": {"privacyStatus": "private"}}, timeout=30)
    if r.status_code >= 400:
        log.warning("youtube hide failed for %s: %s", video_id, r.text[:300])
        return False
    return True


# ---------------- GitHub Release "queue": where queued clip videos live between runs ----------------
QUEUE_TAG = "queue"


def queue_release():
    """The `queue` release (created on first use). Returns its JSON."""
    r = requests.get(f"{GH}/repos/{_repo()}/releases/tags/{QUEUE_TAG}", headers=_gh(), timeout=30)
    if r.status_code == 200:
        return r.json()
    r = requests.post(f"{GH}/repos/{_repo()}/releases", headers=_gh(), timeout=30,
                      json={"tag_name": QUEUE_TAG, "name": "Clip queue",
                            "body": "Videos waiting to be posted. Managed by the bot; assets are removed after posting.",
                            "prerelease": True})
    r.raise_for_status()
    return r.json()


def release_upload(path, name=None):
    """Upload a file to the queue release. Returns (asset_id, public download URL)."""
    rel = queue_release()
    name = name or os.path.basename(str(path))
    for a in rel.get("assets") or []:   # re-running a clip replaces its old upload
        if a["name"] == name:
            release_delete(a["id"])
    with open(path, "rb") as f:
        r = requests.post(f"https://uploads.github.com/repos/{_repo()}/releases/{rel['id']}/assets",
                          params={"name": name}, data=f, timeout=600,
                          headers=dict(_gh(), **{"Content-Type": "video/mp4"}))
    r.raise_for_status()
    a = r.json()
    return a["id"], a["browser_download_url"]


def release_delete(asset_id):
    r = requests.delete(f"{GH}/repos/{_repo()}/releases/assets/{asset_id}", headers=_gh(), timeout=30)
    if r.status_code not in (204, 404):
        r.raise_for_status()


def download(url, dest):
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest
