"""Telegram relay for the bot that runs on GitHub (used until the clipping itself moves to the Mac).

Every few minutes the Mac reads the live bot's state from GitHub's main branch (read-only; it never writes there)
and sends Elie on Telegram:
- each clip the bot sends him to post: the video plus the TikTok caption, YouTube title and description
- the live TikTok/YouTube link of every posted campaign clip, ready to paste into Content Rewards
- new paid campaigns it found, with the join link
- anything that breaks: a job failing twice in a row, the GitHub runs stopping, Gmail refusing the bot's login
No Claude calls. The pure part (what to send) is relay(); fetching and sending live in run().
"""
import json
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

from . import tg
from .common import ROOT, log, now_utc

ACTIONS_URL = "https://github.com/ElieWoodward/gta-clipper/actions"
FIXES = {
    "anthropic": "The Claude API key or its credit. 1. Go to console.anthropic.com > Settings > Billing and check there's "
                 "credit. 2. If the key was deleted, make a new one under API Keys and paste it into GitHub: repo "
                 "Settings > Secrets and variables > Actions > ANTHROPIC_API_KEY > Update.",
    "credit": "The Claude account is probably out of credit: console.anthropic.com > Settings > Billing > add credit.",
    "groq": "The Groq key (used for transcripts). 1. console.groq.com > API Keys > create a key. 2. GitHub repo Settings > "
            "Secrets and variables > Actions > GROQ_API_KEY > Update.",
    "gmail": "Gmail refused the bot's login. 1. Go to myaccount.google.com (signed in as the channel Gmail) > Security > "
             "App passwords and make a new one. 2. GitHub repo Settings > Secrets and variables > Actions > "
             "GMAIL_APP_PASSWORD > Update. 3. The bot tries again within 3 hours on its own.",
    "twitch": "Twitch didn't answer. Usually it fixes itself within an hour; nothing to do unless this repeats tomorrow.",
}


# ------------------------------------------------------------------ reading the live bot (git, read-only)
def _git(*args, timeout=90):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)


def read_main():
    """(config, state, last_commit_time) from GitHub's main branch. Raises if GitHub can't be reached."""
    f = _git("fetch", "-q", "origin", "main")
    if f.returncode != 0:
        raise RuntimeError(f"couldn't reach GitHub: {f.stderr.strip()[:200]}")

    def show(path, default=""):
        r = _git("show", f"origin/main:{path}")
        return r.stdout if r.returncode == 0 else default
    cfg = yaml.safe_load(show("config.yaml")) or {}
    state = json.loads(show("data/state.json", "{}") or "{}")
    live = json.loads(show("data/live.json", "{}") or "{}")
    state.setdefault("clips", {})
    for cid, clip in (live.get("clips") or {}).items():   # clips the live watcher made and emailed
        state["clips"].setdefault(cid, clip)
    state["health_live"] = live.get("health") or {}
    backoff = json.loads(show("data/mail_backoff.json", "{}") or "{}")
    last = _git("log", "-1", "--format=%cI", "origin/main").stdout.strip()
    return cfg, state, backoff, last


# ------------------------------------------------------------------ deciding what to send (pure, tested offline)
def _campaign_of(cfg, state, clip):
    """(name, submit_url) if this clip's streamer is in a paid campaign Elie is in, else None."""
    for s in cfg.get("streamers") or []:
        if s.get("campaign") and (s.get("name") or "").lower() == (clip.get("streamer") or "").lower():
            return s["campaign"], "https://contentrewards.com/c/campaigns"
    for r in (state.get("campaigns") or {}).values():
        if r.get("status") == "joined" and (r.get("twitch_login") or "").lower() == (clip.get("streamer") or "").lower():
            return r["name"], r.get("join_url") or r.get("url")
    return None


COPY_FIELDS = ("TikTok caption", "YouTube title", "YouTube description")   # each sent as its own message


def split_blocks(blocks):
    """Package blocks -> (info text, [(label, text) to paste]). Each pasteable field becomes its own Telegram message
    with nothing else in it, because Telegram on a phone only copies whole messages."""
    info, fields = [], []
    for label, text in blocks:
        if label == "Still in the queue":
            continue
        if label in COPY_FIELDS:
            fields.append((label, text.strip()))
            continue
        text = text.replace('3. Reply "posted" here once it\'s up (or "skip" if they turn it down).',
                            "3. Post it. The bot notices on its own once it's up.")
        info += [f"▸ {label.upper()}", text, ""]
    return "\n".join(info).strip(), fields


def _post_parts(cfg, state, clip):
    from . import clipqueue
    queue_now = [c for c in state["clips"].values() if c.get("status") in ("queued", "approved")]
    try:
        pkg = clipqueue.package(cfg, clip, queue_now, state)
    except Exception as e:   # the live bot's config changed shape: still send the essentials
        log.info("couldn't build the full package (%s); sending the basics", e)
        pkg = {"subject": f"Post now: {clip.get('hook')}", "blocks": [
            ("TikTok caption", clip.get("sent_caption") or clip.get("caption") or ""),
            ("YouTube title", clip.get("sent_title") or clip.get("title") or "")]}
    info, fields = split_blocks(pkg["blocks"])
    order = ", then ".join(label for label, _ in fields)
    info = (f"📲 {pkg['subject']}\n\n{info}\n\nNext messages, one per field so you can copy each whole: {order}.\n"
            "It notices on its own when it shows up on your TikTok/YouTube.").replace("\n\n\n", "\n\n")
    return info, fields


def relay(st, cfg, state, backoff, last_commit, now=None):
    """What to send this round: [("post", clip, caption_line, info, fields) | ("text", text) |
    ("problem", key, what, fix)].
    Updates st["relay"] so nothing is sent twice."""
    now = now or now_utc()
    r = st.setdefault("relay", {})
    if not r.get("since"):   # first run: start from now, so old emails aren't re-sent
        r["since"] = (now - timedelta(minutes=30)).isoformat()
        r["campaigns"] = [k for k, v in (state.get("campaigns") or {}).items() if v.get("status") == "notified"]
        r["links"] = [f"{c['id']}:{p}" for c in state["clips"].values()
                      for p, post in (c.get("posts") or {}).items() if post.get("url")]
    sent, links, camps = r.setdefault("sent", []), r.setdefault("links", []), r.setdefault("campaigns", [])
    out = []
    since = r["since"]
    for c in sorted(state["clips"].values(), key=lambda c: c.get("emailed_at") or ""):
        if c.get("emailed_at") and c["emailed_at"] >= since and c["id"] not in sent:
            sent.append(c["id"])
            info, fields = _post_parts(cfg, state, c)
            out.append(("post", c, f"📲 Post now: {c.get('hook', '')} ({c.get('score', '?')}/10)", info, fields))
    for c in state["clips"].values():
        camp = _campaign_of(cfg, state, c)
        for platform, post in (c.get("posts") or {}).items():
            key = f"{c['id']}:{platform}"
            if not post.get("url") or key in links:
                continue
            links.append(key)
            if camp:
                out.append(("text", f"💰 Your {platform} post of \"{c.get('hook')}\" is live. Submit it on Content "
                                    f"Rewards ({camp[0]}) to get paid:\n\n{post['url']}\n\nWhere: {camp[1]} > the "
                                    "approved draft > submit link. Do it today; views only count once it's approved."))
    for k, v in (state.get("campaigns") or {}).items():
        if v.get("status") == "notified" and k not in camps:
            camps.append(k)
            rate = f"${v['rate']:.2f} per 1K views" if v.get("rate") else "rate on the page"
            out.append(("text", f"🆕 Paid campaign that fits the channel: {v.get('name')}\nPays {rate}. "
                                f"Why it fits: {v.get('why')}\n\nJoin here: {v.get('join_url') or v.get('url')}\n\n"
                                "Once you're accepted, reply \"join\" to the bot's email about it so its clips go "
                                "first. (Join buttons here come with the switch to the Mac.)"))
    for book in ("health", "health_live"):
        for job, h in (state.get(book) or {}).items():
            if (h.get("fails") or 0) >= 2:
                err = h.get("last_error") or "unknown error"
                fix = next((v for k, v in FIXES.items() if k in err.lower()),
                           f"Nothing to do yet if it clears on its own. If you get this again tomorrow, send this "
                           f"message to Claude. Runs: {ACTIONS_URL}")
                out.append(("problem", f"fail:{job}:{h.get('last_fail', '')[:10]}",
                            f"The {job} job failed {h['fails']} times in a row: {err[:300]}", fix))
    if last_commit:
        age = now - datetime.fromisoformat(last_commit)
        if age > timedelta(hours=3):
            out.append(("problem", "stalled", f"The bot on GitHub hasn't saved anything for {age.total_seconds() // 3600:.0f} "
                        "hours, so its runs have probably stopped.",
                        f"1. Open {ACTIONS_URL}\n2. Click \"live\" on the left.\n3. Click \"Run workflow\", then the green "
                        "\"Run workflow\" button. It restarts the others by itself."))
    if backoff.get("until") and backoff["until"] > now.isoformat():
        out.append(("problem", "gmail", f"Gmail refused the bot's login ({backoff.get('error', '')[:150]}), so posting "
                    "messages can't go out until it's fixed.", FIXES["gmail"]))
    del sent[:-300], links[:-1000], camps[:-500]
    return out


# ------------------------------------------------------------------ sending
def send(st, items):
    for item in items:
        try:
            if item[0] == "post":
                _, clip, caption, info, fields = item
                _send_video(clip, caption)
                for chunk in _chunks(info):
                    tg.send(chunk)
                send_fields(fields)
            elif item[0] == "text":
                tg.send(item[1])
            elif item[0] == "problem":
                tg.problem(st, item[1], item[2], item[3])
        except Exception as e:
            log.warning("couldn't send a relay message: %s", e)


def send_fields(fields, rows=None):
    """A small label, then the field alone (copy the whole message). rows (buttons) go on the last one."""
    for i, (label, text) in enumerate(fields):
        tg.send(f"⬇️ {label}", silent=True)
        tg.send(text or "(empty)", rows if i == len(fields) - 1 else None, silent=True)


def _send_video(clip, caption):
    url = clip.get("media_url")
    if not url:
        tg.send(caption + "\n(No video file this time; it's attached to the email.)")
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"{clip['id']}.mp4"
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(p, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            tg.send_video(str(p), caption)
        except Exception as e:
            log.info("couldn't send the video (%s); sending the link", e)
            tg.send(f"{caption}\nVideo: {url}")


def _chunks(text, size=3900):
    out, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    return out + ([cur] if cur else [])


def run(st):
    """One relay round (called every few minutes by the Mac loop)."""
    try:
        cfg, state, backoff, last = read_main()
    except Exception as e:
        r = st.setdefault("relay", {})
        r["fail"] = r.get("fail", 0) + 1
        if r["fail"] >= 10:   # ~30 minutes of not reaching GitHub
            tg.problem(st, "relay_github", f"The Mac can't read the bot's state from GitHub ({str(e)[:200]}).",
                       "Check the Mac is online (Wi-Fi icon in the menu bar). If it is, it usually clears on its own.")
        return
    st.setdefault("relay", {}).update(fail=0, ok=now_utc().isoformat())
    send(st, relay(st, cfg, state, backoff, last))
