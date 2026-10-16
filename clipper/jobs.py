"""The four scheduled jobs: process, post, analytics, learn."""
import hashlib
import json
import os
import shutil
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import clipqueue, fetch, highlights, render, services, vision
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
def _posting_ready(cfg=None):
    """False (with a note on the run page) until posting is set up, so scheduled runs wait instead of failing."""
    from . import mail
    cfg = cfg or load_config()
    if DRY_RUN:
        return True
    if clipqueue.email_mode(cfg):
        if mail.configured():
            return True
        print("::notice::Email isn't set up yet: add the GMAIL_ADDRESS, GMAIL_APP_PASSWORD and NOTIFY_EMAIL secrets. "
              "Nothing was done this run.", flush=True)
        return False
    if os.environ.get("BLOTATO_API_KEY"):
        return True
    print("::notice::Posting isn't set up yet: add the BLOTATO_API_KEY secret. Nothing was done this run.", flush=True)
    return False


def claude_allowed(cfg, state, why=""):
    """Room in the queue and in today's Claude budget? Prints why not."""
    if TEST_SOURCE:
        return True
    if clipqueue.email_mode(cfg):
        free = clipqueue.room(cfg, state)
        if free <= 0:
            print(f"::notice::queue is full ({clipqueue.settings(cfg)['max_size']} clips waiting); not looking for "
                  f"new clips{why}", flush=True)
            return False
    left = highlights.budget_left(cfg, state)
    if left <= 0:
        print(f"::notice::today's Claude budget is used up; no Claude calls until tomorrow (UTC){why}", flush=True)
        return False
    return True


def process():
    cfg, state = load_config(), load_state()
    if TEST_SOURCE:
        return _test_run(cfg, state)
    if not _posting_ready(cfg):
        return
    if days_since_start(cfg) < -1 and not FORCE:
        log.info("before launch window; nothing to process (set FORCE=1 to test)")
        return
    if clipqueue.email_mode(cfg):
        clipqueue.expire(cfg, state)
        if not claude_allowed(cfg, state):
            return
    else:
        cap = 3 * max(r["per_day"] for r in cfg["posting"]["ramp"]) * 2
        if len(_queued(state)) >= cap and not FORCE:
            log.info("queue already has %d clips; skipping", len(_queued(state)))
            return
    vods = fetch.list_new_vods(cfg, state)
    if vods:
        from . import preflight
        preflight.run_all(cfg, vods[0], posting=not DRY_RUN)   # stop before downloading if anything is broken
    for vod in vods:
        if clipqueue.email_mode(cfg) and not claude_allowed(cfg, state, "; leaving the rest of the streams for later"):
            break
        try:
            _process_vod(cfg, state, vod)
        except fetch.StillLive:
            log.info("%s is still live; will pick it up after the stream ends", vod["id"])
            continue
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
        limit = None
        if clipqueue.email_mode(cfg) and not TEST_SOURCE:
            limit = max(1, clipqueue.room(cfg, state))   # only as many as the queue has room for
        picks, words = highlights.analyze(cfg, vod, wav, client, max_picks=limit)
        log.info("%s: %d picks", vod["id"], len(picks))
        bar = cfg["clip"]["min_score"]
        for p in picks:
            if limit is not None and clipqueue.room(cfg, state) <= 0:
                break
            # Below the quality bar never gets rendered or posted (test runs keep them so you can compare).
            if cfg["review"]["mode"] == "auto" and int(p["score"]) < bar and not TEST_SOURCE:
                continue
            if overlaps_existing(state, vod["id"], p["start"], p["end"]):
                log.info("skipping %s-%s: already clipped live", p["start"], p["end"])
                continue
            make_clip(cfg, state, vod, p, words, d)
    finally:
        if DRY_RUN:  # keep the finished clips so a test run can hand them over
            export_test_clips(d)
        shutil.rmtree(d, ignore_errors=True)


def export_test_clips(d):
    out = WORK.parent / "test-output"
    (out / "previews").mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.mp4"):
        if not f.name.endswith("_src.mp4"):
            shutil.copy(f, out / f.name)
            try:
                contact_sheet(f, out / "previews" / f"{f.stem}.jpg")
            except Exception as e:
                log.warning("preview for %s failed: %s", f.name, e)


def contact_sheet(video, dest, n=5):
    """One image with n frames across the clip, so a test run can be checked without downloading the video."""
    from .common import run
    dur = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video]).strip())
    frames = []
    for i in range(n):
        t = dur * (0.03 + 0.94 * i / (n - 1))
        fp = dest.with_name(f"{dest.stem}_{i}.png")
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-vf", "scale=300:-2", fp])
        frames.append(fp)
    args = []
    for fp in frames:
        args += ["-i", fp]
    run(["ffmpeg", "-y", "-loglevel", "error", *args, "-filter_complex", f"hstack={n}", "-q:v", "3", dest])
    for fp in frames:
        fp.unlink()
    return dest


def _test_vods(cfg):
    """The VODs a test run would use (newest first), and the config to use for them."""
    if TEST_SOURCE.startswith("twitch:"):
        ch = TEST_SOURCE.split(":", 1)[1]
        cfg = dict(cfg, streamers=[{"name": ch, "platform": "twitch", "channel": ch}], manual_urls=[])
        cfg["fetch"] = dict(cfg["fetch"], vods_per_streamer=10, max_vod_age_days=30)
        vods = fetch.list_new_vods(cfg, {"processed_vods": []})[:3]  # GTA-titled streams first
        if not vods:
            raise RuntimeError(f"No GTA streams from {ch} in the last 10 broadcasts")
        if vods:
            print(f"::notice::test source: {vods[0]['title']} | game: {vods[0].get('game') or 'unknown'}", flush=True)
    else:
        vods = [{"id": "test:" + TEST_SOURCE, "url": TEST_SOURCE, "title": "",
                 "streamer": {"name": "test", "platform": "manual"}}]
    if not vods:
        raise RuntimeError(f"No VOD found for {TEST_SOURCE}")
    return cfg, vods


def preflight_only():
    """Just the start-of-run check, against the test source's VOD (about a minute, no download)."""
    from . import preflight
    cfg = load_config()
    vod = None
    if TEST_SOURCE:
        cfg, vods = _test_vods(cfg)
        vod = vods[0]
    if clipqueue.email_mode(cfg):
        _check_accounts(cfg)
    preflight.run_all(cfg, vod)


def _check_accounts(cfg):
    """Report (never fail) whether your TikTok and YouTube pages can be read from here."""
    acct = cfg.get("accounts") or {}
    if acct.get("tiktok"):
        try:
            ups = clipqueue.tiktok_uploads(acct["tiktok"])
            print(f"::notice::TikTok page readable: {len(ups)} recent upload(s)", flush=True)
        except Exception as e:
            print(f"::warning::TikTok page not readable from GitHub ({str(e)[-160:]}); "
                  "posted clips will be detected from your email replies instead", flush=True)
    if acct.get("youtube"):
        try:
            cid = clipqueue.youtube_channel_id(acct["youtube"])
            ups = clipqueue.youtube_uploads(cid)
            print(f"::notice::YouTube channel readable ({cid}): {len(ups)} recent upload(s)", flush=True)
        except Exception as e:
            print(f"::warning::YouTube channel not readable ({str(e)[-160:]})", flush=True)


def _test_run(cfg, state):
    """Process one source end to end without uploading, opening issues or posting."""
    import json as _json
    from . import preflight
    cfg, vods = _test_vods(cfg)
    preflight.run_all(cfg, vods[0])   # about a minute: stop now if anything is broken, before the long download
    cfg["clip"] = dict(cfg["clip"], max_per_vod=min(6, cfg["clip"].get("max_per_vod", 16)))
    scratch = {"processed_vods": [], "clips": {}, "posts": [], "pruned": []}
    import clipper.common as common
    common.save_state = lambda s: None  # never touch the real state in a test
    globals()["save_state"] = lambda s: None
    for vod in vods:  # the newest may still be live; fall back to the latest finished stream
        try:
            _process_vod(cfg, scratch, vod)
            vods = [vod]
            break
        except fetch.StillLive:
            log.info("%s still live, trying the previous stream", vod["id"])
    else:
        raise RuntimeError("All recent streams are still live; try again later")
    summary = [{k: c.get(k) for k in ("score", "hook", "hook_score", "hook_options", "punch_text", "punch_at", "title", "caption", "duration", "why",
                         "status", "facecam", "layout", "framing", "said", "file", "start")}
               for c in scratch["clips"].values()]
    (WORK.parent / "test-output").mkdir(exist_ok=True)
    (WORK.parent / "test-output" / "summary.json").write_text(_json.dumps(
        {"source": vods[0]["url"], "title": vods[0].get("title"), "clips": summary}, indent=2))
    log.info("test run done: %s", _json.dumps(summary, indent=2))
    for c in summary:  # visible on the run page, so hooks can be reviewed without downloading anything
        print(f"::notice::[{c['status']} {c['score']}/10, {c['duration']}s, {c['layout']}] {c['hook']}"
              + (f"  | punchline: {c['punch_text']}" if c.get("punch_text") else "") + f"  | {c['why']}", flush=True)


def make_clip(cfg, state, vod, p, words, d, src=None, src_offset=None):
    cid = _clip_id(vod["id"], p["start"])
    if cid in state["clips"]:
        return state["clips"][cid]
    if src is None:
        dl_start = max(0, int(p["start"]) - 2)
        src = fetch.download_section(vod, dl_start, p["end"] + 2, d / f"{cid}_src.mp4")
        src_offset = p["start"] - dl_start
    out = d / f"{cid}.mp4"
    words = caption_words(cfg, src, src_offset, p, d / f"{cid}_cap.wav", fallback=words)
    # Look at the frames first: find the webcam and where the action is, so the vertical crop keeps what matters.
    p = dict(p, **place_punchline(p, words))   # only show a punchline that's really said, exactly when it's said
    look = vision.analyze_frames(cfg, src, src_offset, p["end"] - p["start"], d)
    facecam = vod["streamer"].get("facecam") or look["facecam"]
    layout = cfg["clip"].get("layout", "auto")
    if layout == "auto":
        layout = look["layout"]
    render.render(src, src_offset, p, words, out, facecam=facecam, focus_x=look["focus_x"],
                  commentary=cfg["clip"].get("commentary", False), layout=layout)
    if facecam:  # remember which streamers show their face; their clips get priority
        state.setdefault("streamers", {}).setdefault(vod["streamer"]["name"], {})["facecam"] = True
    asset_id = None
    if DRY_RUN:
        media_url = None
    elif clipqueue.email_mode(cfg):
        asset_id, media_url = services.release_upload(out, f"{cid}.mp4")
    else:
        media_url = services.blotato_upload(out)
    clip = {
        "id": cid, "vod": vod["id"], "vod_url": vod["url"], "streamer": vod["streamer"]["name"],
        "start": p["start"], "end": p["end"], "duration": round(p["end"] - p["start"], 1),
        "score": int(p["score"]), "hook": p["hook"], "title": p["title"], "caption": p["caption"],
        "tags": p.get("tags", []), "why": p.get("why", ""), "hook_options": p.get("hook_options", []), "hook_score": p.get("hook_score"),
        "punch_text": p.get("punch_text", ""), "punch_at": p.get("punch_at", 0), "media_url": media_url,
        "facecam": bool(facecam), "layout": "cam+" + layout if facecam else layout, "framing": look["why"],
        "said": " ".join(w["w"] for w in words if p["start"] <= w["s"] < p["end"])[:600], "file": out.name,
        "created": now_utc().isoformat(), "posts": {}, "issue": None, "local_file": str(out), "asset_id": asset_id,
    }
    auto = cfg["review"]["mode"] == "auto"
    clip["status"] = ("approved" if clip["score"] >= cfg["clip"]["min_score"] else "rejected") if auto else "queued"
    if not DRY_RUN:
        verdict = {"approved": "Will post automatically.", "rejected": "Below the score bar, won't post.",
                   "queued": "Add the `approve` label to post it, or close this issue to skip it."}[clip["status"]]
        body = (f"**Score {clip['score']}/10** · {clip['streamer']} · {clip['duration']}s\n\n"
                f"Video: {media_url}\n\n**Hook:** {clip['hook']}\n\n"
                + (f"**Punchline:** {clip['punch_text']} (at {clip['punch_at']}s)\n\n" if clip.get("punch_text") else "")
                + "**Other hooks it considered:**\n"
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


def _norm(w):
    return "".join(ch for ch in w.lower() if ch.isalnum())


def place_punchline(p, words):
    """Find the punchline in what's actually said in the clip. Returns punch_text/punch_at; drops it if not found."""
    target = [_norm(w) for w in (p.get("punch_text") or "").split() if _norm(w)]
    said = [w for w in words if p["start"] <= w["s"] < p["end"] and _norm(w["w"])]
    if not target or not said:
        return {"punch_text": "", "punch_at": 0}
    best, best_i = 0.0, None
    for i in range(len(said)):
        window = [_norm(w["w"]) for w in said[i:i + len(target)]]
        hit = sum(1 for a, b in zip(target, window) if a == b) / len(target)
        if hit > best:
            best, best_i = hit, i
    if best_i is None or best < 0.6:
        log.info("punchline %r isn't in the clip's audio; dropping it", p.get("punch_text"))
        return {"punch_text": "", "punch_at": 0}
    return {"punch_text": p["punch_text"], "punch_at": round(said[best_i]["s"] - p["start"], 2)}


def caption_words(cfg, src, src_offset, p, wav, fallback):
    """Transcribe exactly the audio that ends up in the clip, so captions match what is said on screen."""
    from .common import run
    try:
        dur = p["end"] - p["start"]
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{src_offset:.2f}", "-i", src, "-t", f"{dur:.2f}",
             "-vn", "-ac", "1", "-ar", "16000", wav])
        model = cfg["models"].get("captions", cfg["models"]["whisper"])
        words = highlights.transcribe(wav, model, start=0.0, end=dur, lenient=True)
        return [dict(w, s=w["s"] + p["start"], e=w["e"] + p["start"]) for w in words]
    except Exception as e:
        log.warning("caption transcription failed (%s); using scan transcript", e)
        return fallback


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
    """Viral-style description: one casual reaction line, a credit, then 4-6 hashtags incl. the streamer's."""
    tags = list(cfg["posting"]["hashtags"])
    if clip["streamer"] not in ("manual", "test"):
        tags.append("#" + "".join(ch for ch in clip["streamer"].lower() if ch.isalnum()))
    tags = " ".join(dict.fromkeys(tags))
    credit = f"Streamer: {clip['streamer']}" if clip["streamer"] not in ("manual", "test") else ""
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
    cfg = load_config()
    if not _posting_ready(cfg) and TEST_SOURCE != "email":
        return
    if clipqueue.email_mode(cfg) or TEST_SOURCE == "email":
        return email_post(cfg)
    state = load_state()
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
        # Best score first, facecam clips win ties (a visible reaction holds viewers), then the newest.
        clip = sorted(ready, key=lambda c: (c["score"] + (0.5 if c.get("facecam") else 0), c["created"]),
                      reverse=True)[0]
        post_clip(cfg, state, clip, platform)


def post_clip(cfg, state, clip, platform):
    """Publish one clip to one platform now and record it. Returns the Blotato status, or None on error."""
    now = now_local(cfg)
    try:
        acct = services.blotato_account_id(platform)
        sub = services.blotato_post(platform, acct, clip["media_url"], _caption(cfg, clip, platform),
                                    title=clip["title"], ai_label=cfg["posting"].get("label_ai_generated"))
        status, info = services.blotato_wait(sub)
    except Exception as e:
        log.exception("post to %s failed: %s", platform, e)
        return None
    clip["posts"][platform] = {"submission": sub, "status": status, "url": info if status == "published" else None,
                               "error": info if status == "failed" else None, "date": now.date().isoformat(),
                               "posted_at": now_utc().isoformat(), "hour": now.strftime("%H:00"),
                               "views": None, "history": []}
    clip["status"] = "posted"
    if clip.get("issue"):
        services.gh_comment(clip["issue"], f"Posted to {platform}: {status} {info or ''}")
    log.info("posted %s to %s: %s", clip["id"], platform, status)
    print(f"::notice::posted to {platform}: {status} {info or ''} | {clip['hook']}", flush=True)
    save_state(state)
    return status


def posted_today(cfg, state, platform):
    today = now_local(cfg).date().isoformat()
    return sum(1 for c in state["clips"].values() if c["posts"].get(platform, {}).get("date") == today)


def overlaps_existing(state, vod_id, start, end, min_share=0.5):
    """True if a clip already made from this stream covers most of [start, end] (live and VOD runs overlap)."""
    for c in state["clips"].values():
        if c.get("vod") != vod_id:
            continue
        inter = min(end, c["end"]) - max(start, c["start"])
        if inter > min_share * min(end - start, c["end"] - c["start"]):
            return True
    return False


# ------------------------------------------------------------------ email mode: one email per posting slot
def cleanup_assets(state):
    """Delete the stored video of clips that left the queue."""
    for c in state["clips"].values():
        if c.get("asset_id") and not c.get("asset_deleted") and c.get("status") != "approved":
            try:
                services.release_delete(c["asset_id"])
                c["asset_deleted"] = True
            except Exception as e:
                log.info("couldn't delete stored video for %s: %s", c["id"], e)


def emails_due(cfg, state, now):
    """How many posting emails should have gone out by now today, and how many did."""
    sent = clipqueue.emailed_on(state, now.date().isoformat(), cfg.get("timezone", "UTC"))
    slots = _slots(cfg, _quota(cfg))
    due = sum(1 for sl in slots if now.strftime("%H:%M") >= sl)
    return due, sent


def send_package(cfg, state, clip, test=False):
    """Email one clip with everything needed to post it. Returns the Message-ID."""
    from . import mail
    pkg = clipqueue.package(cfg, clip, clipqueue.queue(state))
    d = WORK / f"email_{clip['id']}"
    d.mkdir(parents=True, exist_ok=True)
    attach = None
    try:
        src = clip.get("local_file")
        if not (src and os.path.exists(src)):
            src = services.download(clip["media_url"], d / "clip.mp4") if clip.get("media_url") else None
        if src:
            attach = clipqueue.fit_for_email(src, d / f"{clip['id']}.mp4", mail.ATTACH_LIMIT)
    except Exception as e:
        log.warning("couldn't prepare the attachment (%s); sending the link only", e)
        attach = None
    text, html = clipqueue.render_email(pkg, download_url=clip.get("media_url"), attached=bool(attach))
    subject = ("[TEST] " if test else "") + pkg["subject"]
    mid = mail.send(subject, text, html, attachment=attach, attachment_name=f"wantedlevel_{clip['id']}.mp4")
    shutil.rmtree(d, ignore_errors=True)
    now = now_utc()
    if not test:
        clip.update(emailed_at=now.isoformat(), email_message_id=mid, sent_caption=pkg["tiktok_caption"],
                    sent_title=pkg["yt_title"])
    em = state.setdefault("email", {})
    em.setdefault("log", []).append({"date": now_local(cfg).date().isoformat(), "at": now.isoformat(),
                                     "clip": clip["id"], "subject": subject, "test": test})
    em["log"] = em["log"][-200:]
    print(f"::notice::emailed {'test ' if test else ''}package: {pkg['subject']} "
          f"({'attached' if attach else 'link only'})", flush=True)
    return mid


def _email_test(cfg, state):
    """test_source=email: send one real package for the best queued clip, or a freshly rendered sample."""
    clip = clipqueue.pick_for_email(state) or (clipqueue.queue(state) or [None])[0]
    if clip is None:
        from . import preflight
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "sample.mp4"
        src = preflight._sample_video(tmp, seconds=12)
        words = [{"w": w, "s": 0.5 + i * 0.4, "e": 0.8 + i * 0.4} for i, w in enumerate("no way he actually did it".split())]
        render.render(src, 0.0, {"start": 0.0, "end": 12.0, "hook": "TEST: this is what a posting email looks like 📬",
                                 "punch_text": "", "punch_at": 0}, words, out, layout="wide")
        clip = {"id": "test", "hook": "TEST: this is what a posting email looks like 📬", "score": 9,
                "streamer": cfg["streamers"][0]["name"] if cfg.get("streamers") else "test", "title": "Test clip title",
                "caption": "test caption, nothing to post 😭", "tags": ["test"], "why": "A sample clip to check the email.",
                "duration": 12.0, "start": 0, "vod_url": "", "created": now_utc().isoformat(), "local_file": str(out),
                "facecam": False}
    send_package(cfg, state, clip, test=True)


def email_post(cfg):
    from . import health
    state = load_state()
    if TEST_SOURCE == "email":
        _email_test(cfg, state)
        save_state(state)
        return
    if days_since_start(cfg) < 0 and not FORCE:
        log.info("before start_date %s; not emailing", cfg["start_date"])
        return
    for note in clipqueue.refresh_posted(cfg, state):
        print(f"::notice::{note}", flush=True)
    for c in clipqueue.expire(cfg, state):
        print(f"::notice::expired (too old to post): {c['hook']}", flush=True)
    cleanup_assets(state)
    save_state(state)
    now = now_local(cfg)
    due, sent = emails_due(cfg, state, now)
    q = clipqueue.queue(state)
    log.info("queue %d, emails due today %d, sent %d", len(q), due, sent)
    if FORCE or sent < due:
        clip = clipqueue.pick_for_email(state)
        if clip:
            send_package(cfg, state, clip)
        else:
            log.info("a posting slot is due but nothing new is waiting")
    health.check_quiet(cfg, state, len(clipqueue.queue(state)))
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
Find what separates winners from losers: streamer, category tags, hook wording, clip length, whether the
streamer's webcam was on screen, posting hour, platform. Likes and shares per view show what people found funny;
views relative to the account's median show what held attention.

Write a playbook of at most 12 concrete rules the clip picker should follow next week (what to pick more of,
what to stop picking, what kind of humor worked, hook styles that worked with real examples). Then a 3-sentence
plain-English summary for the account owner.

Also return target_seconds: the [shortest, longest] clip length range that performed best, if the data supports it
(at least a few clips on each side), else null.

Also return the 4 best posting times (HH:MM, local) if the data supports it, else an empty list.

Submit with the tool: {{"playbook": "markdown string", "summary": "string", "best_slots": ["HH:MM"], "target_seconds": [int, int] or null}}

DATA:
{data}
"""


LEARN_SCHEMA = {"type": "object", "required": ["playbook", "summary"],
                "properties": {"playbook": {"type": "string"}, "summary": {"type": "string"},
                               "best_slots": {"type": "array", "items": {"type": "string"}},
                               "target_seconds": {"type": ["array", "null"], "items": {"type": "number"}}}}


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
                         "webcam": c.get("facecam", False),
                         "views": p["views"], "likes": p.get("likes"), "shares": p.get("shares"),
                         "age_h": round(_age_hours(p))})
    if len(rows) < 6:
        log.info("only %d posts with data; skipping learn", len(rows))
        return
    client = client or anthropic.Anthropic()
    out = highlights.ask_tool(client, cfg["models"]["learn"], LEARN_PROMPT.format(data=json.dumps(rows)), LEARN_SCHEMA,
                              max_tokens=3000)
    PLAYBOOK_FILE.write_text(out["playbook"])
    ts = out.get("target_seconds")
    lo_hi = cfg["clip"]["min_seconds"], cfg["clip"]["max_seconds"]
    if not (isinstance(ts, list) and len(ts) == 2 and all(isinstance(x, (int, float)) for x in ts) and ts[0] < ts[1]):
        ts = None
    else:  # keep it inside the configured limits
        ts = [int(max(lo_hi[0], ts[0])), int(min(lo_hi[1], ts[1]))]
        ts = ts if ts[0] < ts[1] else None
    INSIGHTS_FILE.write_text(json.dumps({"best_slots": out.get("best_slots") or [], "target_seconds": ts,
                                         "updated": now_utc().isoformat()}))
    if not DRY_RUN:
        services.gh_upsert_pinned("weekly-report", f"Weekly report {now_local(cfg):%b %d}",
                                  f"{out['summary']}\n\n### Playbook the picker now follows\n\n{out['playbook']}")
