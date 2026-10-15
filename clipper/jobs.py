"""The four scheduled jobs: process, post, analytics, learn."""
import hashlib
import json
import os
import shutil
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import fetch, highlights, render, services
from .common import (log, load_config, load_state, save_state, now_utc, now_local, days_since_start,
                     WORK, PLAYBOOK_FILE, INSIGHTS_FILE, load_json)

FORCE = os.environ.get("FORCE") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TEST_SOURCE = os.environ.get("TEST_SOURCE", "").strip()   # a VOD URL or twitch:<channel>; test run, nothing posted
CLIP_MAX_AGE_DAYS = 6


def _clip_id(vod_id, start):
    return hashlib.sha1(f"{vod_id}:{start}".encode()).hexdigest()[:10]


def _queued(state):
    return [c for c in state["clips"].values() if c["status"] in ("queued", "approved")]


# ------------------------------------------------------------------ process
def process():
    cfg, state = load_config(), load_state()
    if TEST_SOURCE:
        return _test_run(cfg, state)
    if days_since_start(cfg) < -1 and not FORCE:
        log.info("before launch window; nothing to process (set FORCE=1 to test)")
        return
    cap = 3 * max(r["per_day"] for r in cfg["posting"]["ramp"]) * 2
    if len(_queued(state)) >= cap and not FORCE:
        log.info("queue already has %d clips; skipping", len(_queued(state)))
        return
    for vod in fetch.list_new_vods(cfg, state):
        try:
            _process_vod(cfg, state, vod)
        except Exception as e:  # one bad VOD shouldn't stop the rest
            log.exception("failed on %s: %s", vod["id"], e)
        state["processed_vods"].append(vod["id"])
        state["processed_vods"] = state["processed_vods"][-500:]
        save_state(state)


def _process_vod(cfg, state, vod, client=None):
    d = WORK / _clip_id(vod["id"], 0)
    d.mkdir(parents=True, exist_ok=True)
    try:
        wav = fetch.download_audio(vod, d)
        picks, words = highlights.analyze(cfg, vod, wav, client)
        log.info("%s: %d picks", vod["id"], len(picks))
        for p in picks[: cfg["clip"]["per_vod"]]:
            make_clip(cfg, state, vod, p, words, d)
    finally:
        if DRY_RUN:  # keep the finished clips so a test run can hand them over
            out = WORK.parent / "test-output"
            out.mkdir(exist_ok=True)
            for f in d.glob("*.mp4"):
                if not f.name.endswith("_src.mp4"):
                    shutil.copy(f, out / f.name)
        shutil.rmtree(d, ignore_errors=True)


def _test_run(cfg, state):
    """Process one source end to end without uploading, opening issues or posting."""
    import json as _json
    if TEST_SOURCE.startswith("twitch:"):
        ch = TEST_SOURCE.split(":", 1)[1]
        cfg = dict(cfg, streamers=[{"name": ch, "platform": "twitch", "channel": ch}], manual_urls=[])
        cfg["fetch"] = dict(cfg["fetch"], vods_per_streamer=1, max_vod_age_days=30)
        vods = fetch.list_new_vods(cfg, {"processed_vods": []})[:1]
    else:
        vods = [{"id": "test:" + TEST_SOURCE, "url": TEST_SOURCE, "title": "",
                 "streamer": {"name": "test", "platform": "manual"}}]
    if not vods:
        raise RuntimeError(f"No VOD found for {TEST_SOURCE}")
    cfg["clip"] = dict(cfg["clip"], per_vod=2)
    scratch = {"processed_vods": [], "clips": {}, "posts": [], "pruned": []}
    import clipper.common as common
    common.save_state = lambda s: None  # never touch the real state in a test
    globals()["save_state"] = lambda s: None
    _process_vod(cfg, scratch, vods[0])
    summary = [{k: c.get(k) for k in ("score", "hook", "hook_options", "title", "caption", "duration", "why", "status")}
               for c in scratch["clips"].values()]
    (WORK.parent / "test-output").mkdir(exist_ok=True)
    (WORK.parent / "test-output" / "summary.json").write_text(_json.dumps(
        {"source": vods[0]["url"], "title": vods[0].get("title"), "clips": summary}, indent=2))
    log.info("test run done: %s", _json.dumps(summary, indent=2))


def make_clip(cfg, state, vod, p, words, d, src=None, src_offset=None):
    cid = _clip_id(vod["id"], p["start"])
    if cid in state["clips"]:
        return state["clips"][cid]
    if src is None:
        dl_start = max(0, int(p["start"]) - 2)
        src = fetch.download_section(vod, dl_start, p["end"] + 2, d / f"{cid}_src.mp4")
        src_offset = p["start"] - dl_start
    out = d / f"{cid}.mp4"
    render.render(src, src_offset, p, words, out, facecam=vod["streamer"].get("facecam"),
                  commentary=cfg["clip"].get("commentary", False))
    media_url = None if DRY_RUN else services.blotato_upload(out)
    clip = {
        "id": cid, "vod": vod["id"], "vod_url": vod["url"], "streamer": vod["streamer"]["name"],
        "start": p["start"], "end": p["end"], "duration": round(p["end"] - p["start"], 1),
        "score": int(p["score"]), "hook": p["hook"], "title": p["title"], "caption": p["caption"],
        "tags": p.get("tags", []), "why": p.get("why", ""), "hook_options": p.get("hook_options", []), "media_url": media_url,
        "created": now_utc().isoformat(), "posts": {}, "issue": None, "local_file": str(out),
    }
    auto = cfg["review"]["mode"] == "auto"
    clip["status"] = ("approved" if clip["score"] >= cfg["clip"]["min_score"] else "rejected") if auto else "queued"
    if not DRY_RUN:
        verdict = {"approved": "Will post automatically.", "rejected": "Below the score bar, won't post.",
                   "queued": "Add the `approve` label to post it, or close this issue to skip it."}[clip["status"]]
        body = (f"**Score {clip['score']}/10** · {clip['streamer']} · {clip['duration']}s\n\n"
                f"Video: {media_url}\n\n**Hook:** {clip['hook']}\n\n**Other hooks it considered:**\n"
                + "".join(f"- {h}\n" for h in clip.get("hook_options", []) if h != clip["hook"])
                + f"\n**Title:** {clip['title']}\n\n"
                f"**Caption:** {clip['caption']}\n\n**Why:** {clip['why']}\n\n"
                f"Source: {vod['url']} at {int(p['start'])}s\n\n{verdict}")
        clip["issue"] = services.gh_issue(f"[clip {clip['score']}] {clip['hook']}", body, ["clip", clip["status"]])
        if clip["status"] == "rejected":
            services.gh_update(clip["issue"], state="closed")
    state["clips"][cid] = clip
    save_state(state)
    return clip


# ------------------------------------------------------------------ post
def _quota(cfg):
    day = days_since_start(cfg)
    per = 0
    for r in cfg["posting"]["ramp"]:
        if day >= r["from_day"]:
            per = r["per_day"]
    return per


def _slots(cfg, n):
    slots = list(cfg["posting"]["slots"])
    ins = load_json(INSIGHTS_FILE, {})
    if cfg["posting"].get("adaptive_slots") and ins.get("best_slots"):
        slots = ins["best_slots"]
    return sorted(slots[:n])


def _caption(cfg, clip, platform):
    tags = " ".join(cfg["posting"]["hashtags"])
    credit = f"Streamer: {clip['streamer']}" if clip["streamer"] != "manual" else ""
    if platform == "tiktok":
        return f"{clip['caption']}\n\n{credit}\n{tags}".strip()
    return f"{clip['caption']}\n\n{credit}\n{tags} #shorts".strip()


def _refresh_manual_approvals(state):
    for c in state["clips"].values():
        if c["status"] == "queued" and c.get("issue"):
            iss = services.gh_get(c["issue"])
            labels = {l["name"] for l in iss.get("labels", [])}
            if "approve" in labels:
                c["status"] = "approved"
            elif iss.get("state") == "closed":
                c["status"] = "rejected"


def post():
    cfg, state = load_config(), load_state()
    if days_since_start(cfg) < 0 and not FORCE:
        log.info("before start_date %s; not posting", cfg["start_date"])
        return
    if cfg["review"]["mode"] == "manual":
        _refresh_manual_approvals(state)
    now = now_local(cfg)
    today = now.date().isoformat()
    slots = _slots(cfg, _quota(cfg))
    due = sum(1 for s in slots if now.strftime("%H:%M") >= s) if not FORCE else len(slots) or 1
    fresh_cut = now_utc() - timedelta(days=CLIP_MAX_AGE_DAYS)
    for platform in cfg["posting"]["platforms"]:
        done_today = sum(1 for c in state["clips"].values()
                         if c["posts"].get(platform, {}).get("date") == today)
        if done_today >= due:
            continue
        ready = [c for c in state["clips"].values()
                 if c["status"] in ("approved", "posted") and platform not in c["posts"]
                 and datetime.fromisoformat(c["created"]) > fresh_cut and c.get("media_url")]
        if not ready:
            log.info("%s: nothing ready to post", platform)
            continue
        # Best score first; ties go to the newest.
        clip = sorted(ready, key=lambda c: (c["score"], c["created"]), reverse=True)[0]
        try:
            acct = services.blotato_account_id(platform)
            sub = services.blotato_post(platform, acct, clip["media_url"], _caption(cfg, clip, platform),
                                        title=clip["title"], ai_label=cfg["posting"].get("label_ai_generated"))
            status, info = services.blotato_wait(sub)
        except Exception as e:
            log.exception("post to %s failed: %s", platform, e)
            continue
        clip["posts"][platform] = {"submission": sub, "status": status, "url": info if status == "published" else None,
                                   "error": info if status == "failed" else None, "date": today,
                                   "posted_at": now_utc().isoformat(), "hour": now.strftime("%H:00"),
                                   "views": None, "history": []}
        clip["status"] = "posted"
        if clip.get("issue"):
            services.gh_comment(clip["issue"], f"Posted to {platform}: {status} {info or ''}")
        log.info("posted %s to %s: %s", clip["id"], platform, status)
        save_state(state)


# ------------------------------------------------------------------ analytics + prune
def analytics():
    cfg, state = load_config(), load_state()
    if days_since_start(cfg) < 0 and not FORCE:
        log.info("before start_date %s; nothing posted yet", cfg["start_date"])
        return
    since = (now_utc() - timedelta(days=60)).isoformat()
    published = services.blotato_published(since)
    by_media = {}
    for it in published:
        for m in it.get("mediaUrls") or []:
            by_media[(it.get("platform"), m)] = it
    for c in state["clips"].values():
        for platform, p in c["posts"].items():
            it = by_media.get((platform, c.get("media_url")))
            if not it:
                continue
            st = it.get("state") or {}
            p["url"] = st.get("postUrl") or p.get("url")
            met = ((st.get("analytics") or {}).get("latest") or {}).get("metrics") or {}
            if met.get("viewsCount") is not None:
                v = int(met["viewsCount"])
                p["views"] = v
                p["likes"] = int(met.get("likesCount") or 0)
                p["comments"] = int(met.get("commentsCount") or 0)
                p["shares"] = int(met.get("sharesCount") or 0)
                p["history"] = (p.get("history") or [])[-30:] + [[now_utc().isoformat(), v]]
    save_state(state)
    _prune(cfg, state)
    _status_issue(cfg, state)


def _age_hours(p):
    return (now_utc() - datetime.fromisoformat(p["posted_at"])).total_seconds() / 3600


def _prune(cfg, state):
    pc = cfg["prune"]
    if not pc["enabled"] or days_since_start(cfg) < pc["start_after_days"]:
        return
    hidden = 0
    for platform in ("youtube", "tiktok"):
        views = [p["views"] for c in state["clips"].values() for pl, p in c["posts"].items()
                 if pl == platform and p.get("views") is not None]
        if len(views) < 8:
            continue
        median = statistics.median(views)
        bar = max(pc["min_views"], median * pc["relative_to_median"])
        for c in state["clips"].values():
            p = c["posts"].get(platform)
            if not p or p.get("hidden") or p.get("views") is None or _age_hours(p) < pc["judge_after_hours"]:
                continue
            if p["views"] >= bar or hidden >= pc["max_per_day"]:
                continue
            ok = False
            if platform == "youtube" and p.get("url") and "shorts/" in p["url"]:
                vid = p["url"].rstrip("/").split("/")[-1].split("?")[0]
                ok = services.youtube_set_private(vid)
            p["hidden"] = ok
            p["prune_flagged"] = True
            hidden += ok
            state["pruned"].append({"clip": c["id"], "platform": platform, "views": p["views"], "auto": ok,
                                    "at": now_utc().isoformat()})
    save_state(state)


def _status_issue(cfg, state):
    rows = []
    posts = [(c, pl, p) for c in state["clips"].values() for pl, p in c["posts"].items()]
    posts.sort(key=lambda x: x[2]["posted_at"], reverse=True)
    for c, pl, p in posts[:40]:
        rows.append(f"| {p['date']} | {pl} | {c['streamer']} | {c['hook'][:40]} | {p.get('views') if p.get('views') is not None else '…'} "
                    f"| {'hidden' if p.get('hidden') else ('low' if p.get('prune_flagged') else '')} | {p.get('url') or p.get('error') or ''} |")
    total = defaultdict(int)
    for c, pl, p in posts:
        total[pl] += p.get("views") or 0
    body = (f"Updated {now_local(cfg):%b %d %I:%M %p}. Day {days_since_start(cfg)} since launch, "
            f"{_quota(cfg)} posts/day per platform.\n\n"
            + " · ".join(f"**{pl}**: {v:,} total views" for pl, v in total.items())
            + f"\n\nQueue: {len(_queued(state))} clips ready.\n\n"
            "| date | platform | streamer | hook | views | flag | link |\n|---|---|---|---|---|---|---|\n"
            + "\n".join(rows))
    services.gh_upsert_pinned("status", "Clip dashboard", body)


# ------------------------------------------------------------------ learn
LEARN_PROMPT = """You manage a short-form clip account (GTA streamer clips on TikTok and YouTube Shorts).
Here is performance data for every posted clip (views measured at the latest check, age in hours).
Find what separates winners from losers: streamer, category tags, hook wording, clip length, posting hour, platform.

Write a playbook of at most 12 concrete rules the clip picker should follow next week (what to pick more of,
what to stop picking, hook styles that worked with real examples). Then a 3-sentence plain-English summary
for the account owner.

Also return the 4 best posting times (HH:MM, local) if the data supports it, else an empty list.

Return ONLY JSON: {{"playbook": "markdown string", "summary": "string", "best_slots": ["HH:MM"]}}

DATA:
{data}
"""


def learn(client=None):
    import anthropic
    cfg, state = load_config(), load_state()
    rows = []
    for c in state["clips"].values():
        for pl, p in c["posts"].items():
            if p.get("views") is None:
                continue
            rows.append({"platform": pl, "streamer": c["streamer"], "tags": c["tags"], "hook": c["hook"],
                         "seconds": c["duration"], "score": c["score"], "hour": p.get("hour"),
                         "views": p["views"], "likes": p.get("likes"), "shares": p.get("shares"),
                         "age_h": round(_age_hours(p))})
    if len(rows) < 6:
        log.info("only %d posts with data; skipping learn", len(rows))
        return
    client = client or anthropic.Anthropic()
    msg = client.messages.create(model=cfg["models"]["learn"], max_tokens=3000,
                                 messages=[{"role": "user", "content": LEARN_PROMPT.format(data=json.dumps(rows))}])
    out = highlights.parse_json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
    PLAYBOOK_FILE.write_text(out["playbook"])
    INSIGHTS_FILE.write_text(json.dumps({"best_slots": out.get("best_slots") or [], "updated": now_utc().isoformat()}))
    if not DRY_RUN:
        services.gh_upsert_pinned("weekly-report", f"Weekly report {now_local(cfg):%b %d}",
                                  f"{out['summary']}\n\n### Playbook the picker now follows\n\n{out['playbook']}")
