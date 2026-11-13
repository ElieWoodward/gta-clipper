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


def streamer_off(cfg, name):
    """True when config has this streamer but isn't clipping them (enabled false, avoided, paused campaign)."""
    for s in cfg.get("streamers") or []:
        if (s.get("name") or "").lower() == (name or "").lower():
            return s.get("enabled", True) is False
    return False


def is_campaign(streamer):
    """A campaign that pays now: one in config.yaml, or one Elie joined. Campaigns he's only been told about are
    clipped (so clips are ready if he joins) but get the normal settings."""
    st = streamer or {}
    return bool(st.get("campaign")) and st.get("campaign_joined", True) is not False


def for_stream(cfg, streamer):
    """Campaign streams pay per view, so they get their own settings (clip.campaign: a lower score bar and more
    clips per stream). A streamer's own min_score wins over both."""
    if not is_campaign(streamer):
        return cfg
    clip = dict(cfg["clip"])
    clip.update({k: v for k, v in (cfg["clip"].get("campaign") or {}).items()})
    if streamer.get("min_score") is not None:
        clip["min_score"] = streamer["min_score"]
    return dict(cfg, clip=clip)


def campaign_allowed(cfg, state):
    """Campaign clips earn money, so a full queue doesn't stop them; only the Claude budget does."""
    return bool(TEST_SOURCE) or highlights.budget_left(cfg, state) > 0


def open_room(cfg, state):
    """Queue room left after the clips that streams already handed to half-price judging will probably add."""
    from . import batch
    expected = sum(min(int(r.get("max_picks") or 2), 2) for r in batch.pending())
    return clipqueue.room(cfg, state) - expected


def claude_allowed(cfg, state, why="", quiet=False):
    """Room in the queue and in today's Claude budget? Prints why not (unless quiet)."""
    if TEST_SOURCE:
        return True
    if clipqueue.email_mode(cfg):
        free = open_room(cfg, state)
        if free <= 0:
            if not quiet:
                print(f"::notice::queue is full ({clipqueue.settings(cfg)['max_size']} clips waiting); not looking for "
                      f"new clips{why}", flush=True)
            return False
    left = highlights.budget_left(cfg, state)
    if left <= 0:
        if not quiet:
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
        finish_batches(cfg, state)   # clips from streams judged at half price on an earlier run
        save_state(state)
        if not DRY_RUN:
            try:   # the posting check too, so a fresh clip goes out now and a bumped posting run costs nothing
                email_post(cfg)
            except Exception as e:
                log.warning("posting check inside process failed: %s", e)
            state = load_state()
        if not claude_allowed(cfg, state, quiet=True):
            paid = [x for x in cfg.get("streamers", []) if is_campaign(x) and x.get("enabled", True)]
            if not paid or not campaign_allowed(cfg, state):
                claude_allowed(cfg, state)   # prints why
                return
            # the queue is full, but campaign streams pay per view: keep looking at those only
            cfg = dict(cfg, streamers=paid, manual_urls=[])
    else:
        cap = 3 * max(r["per_day"] for r in cfg["posting"]["ramp"]) * 2
        if len(_queued(state)) >= cap and not FORCE:
            log.info("queue already has %d clips; skipping", len(_queued(state)))
            return
    vods = fetch.list_new_vods(cfg, state)
    live_now = []
    if vods:
        _preflight_if_due(cfg, state, vods[0])   # stop before downloading if anything is broken
    vods = sorted(vods, key=lambda v: not is_campaign(v["streamer"]))   # paid streams first
    for vod in vods:
        if is_campaign(vod["streamer"]):
            if not campaign_allowed(cfg, state):
                print("::notice::today's Claude budget is used up; campaign streams wait for tomorrow", flush=True)
                break
        elif clipqueue.email_mode(cfg) and not claude_allowed(cfg, state, "; leaving the rest of the streams for later",
                                                               quiet=bool(state.get("_told_full"))):
            state["_told_full"] = True   # say it once per run
            continue
        try:
            _process_vod(for_stream(cfg, vod["streamer"]), state, vod)
        except fetch.StillLive:
            log.info("%s is still live; will pick it up after the stream ends", vod["id"])
            live_now.append(vod["streamer"]["name"])
            continue
        except Exception as e:  # one bad VOD shouldn't stop the rest
            log.exception("failed on %s: %s", vod["id"], e)
            tries = state.setdefault("vod_failures", {})
            tries[vod["id"]] = tries.get(vod["id"], 0) + 1
            print(f"::warning::{vod['streamer']['name']}'s stream {vod['id']} failed ({type(e).__name__}: "
                  f"{str(e)[:160]}); {'giving up on it' if tries[vod['id']] >= 2 else 'will retry next run'}", flush=True)
            if tries[vod["id"]] < 2:
                save_state(state)
                continue
        state["processed_vods"].append(vod["id"])
        state["processed_vods"] = state["processed_vods"][-500:]
        save_state(state)
    state.pop("_told_full", None)
    # one line on the run page: what the stream check saw (why there may be no new clips)
    print(f"::notice::stream check: {len(vods)} new GTA stream(s) found"
          + (f"; still live, waiting for the end: {', '.join(live_now)}" if live_now else "")
          + (f"; listing: {fetch.LISTING_NOTES[-1]}" if getattr(fetch, "LISTING_NOTES", None) else ""), flush=True)


def _preflight_if_due(cfg, state, vod, hours=12):
    """The full service check costs a little Claude each time, so scheduled runs do it twice a day at most."""
    from . import preflight
    last = (state.get("preflight") or {}).get("ok")
    if last and now_utc() - datetime.fromisoformat(last) < timedelta(hours=hours):
        return
    preflight.VERBOSE["on"] = False
    preflight.run_all(cfg, vod, posting=not DRY_RUN)
    state.setdefault("preflight", {})["ok"] = now_utc().isoformat()
    save_state(state)


def _process_vod(cfg, state, vod, client=None):
    d = WORK / _clip_id(vod["id"], 0)
    d.mkdir(parents=True, exist_ok=True)
    try:
        wav = fetch.download_audio(vod, d)
        limit = None
        if clipqueue.email_mode(cfg) and not TEST_SOURCE and not is_campaign(vod["streamer"]):
            limit = max(1, open_room(cfg, state))   # only as many as the queue has room for
        from . import batch
        if batch.enabled(cfg) and not TEST_SOURCE and not DRY_RUN:
            windows, words, max_picks = highlights.prepare(cfg, vod, wav, limit)
            if windows:
                import anthropic
                batch.submit(client or anthropic.Anthropic(), vod, windows, words, max_picks,
                             highlights.judge_params(cfg, vod, windows, max_picks))
            return
        picks, words = highlights.analyze(cfg, vod, wav, client, max_picks=limit, keep_all=bool(TEST_SOURCE))
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


def finish_batches(cfg, state, client=None):
    """Turn finished half-price judging batches into clips (or judge normally if a batch is stuck)."""
    from . import batch
    recs = batch.pending()
    if not recs:
        return
    import anthropic
    client = client or anthropic.Anthropic()
    base = cfg
    for rec in recs:
        vod = rec["vod"]
        cfg = for_stream(base, vod.get("streamer"))
        try:
            status, data = batch.result(client, cfg, rec)
        except Exception as e:
            log.warning("couldn't check batch %s: %s", rec["batch_id"], e)
            continue
        if status == "waiting":
            log.info("%s: batch still running", vod["id"])
            continue
        if highlights.budget_left(cfg, state) <= 0:
            print("::notice::today's Claude budget is used up; finishing judged streams tomorrow", flush=True)
            return
        windows, words = rec["windows"], batch.words_of(rec)
        try:
            if status == "fallback":
                print(f"::notice::{vod['streamer']['name']}: {data}; judging the normal way", flush=True)
                picks = highlights.judge(cfg, vod, windows, client, max_picks=rec["max_picks"])
            else:
                picks = highlights.judge_picks(cfg, data, windows, rec["max_picks"])
            picks = highlights.keepers(cfg, picks)
            picks = highlights.edit_hooks(cfg, picks, windows, client) if picks else []
            _make_from_picks(cfg, state, vod, picks, words)
        except Exception as e:
            log.exception("finishing %s failed: %s", vod["id"], e)
        batch.drop(rec)
        save_state(state)


def _make_from_picks(cfg, state, vod, picks, words):
    d = WORK / _clip_id(vod["id"], 1)
    d.mkdir(parents=True, exist_ok=True)
    try:
        bar = cfg["clip"]["min_score"]
        for p in picks:
            if clipqueue.email_mode(cfg) and clipqueue.room(cfg, state) <= 0 and not is_campaign(vod.get("streamer")):
                break
            if cfg["review"]["mode"] == "auto" and int(p["score"]) < bar:
                continue
            if overlaps_existing(state, vod["id"], p["start"], p["end"]):
                continue
            make_clip(cfg, state, vod, p, words, d)
        made = sum(1 for c in state["clips"].values() if c.get("vod") == vod["id"])
        print(f"::notice::{vod['streamer']['name']}: {len(picks)} picks judged, {made} clip(s) in the queue from this "
              "stream", flush=True)
    finally:
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
            top = ups[0] if ups else {}
            print(f"::notice::TikTok page readable: {len(ups)} recent upload(s); latest has {top.get('views')} views, "
                  f"{top.get('likes')} likes", flush=True)
        except Exception as e:
            print(f"::warning::TikTok page not readable from GitHub ({str(e)[-160:]}); "
                  "posted clips will be detected from your email replies instead", flush=True)
    if acct.get("youtube"):
        try:
            cid = clipqueue.youtube_channel_id(acct["youtube"])
            ups = clipqueue.youtube_uploads(cid, acct["youtube"])
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


def clip_speed(cfg):
    return float((cfg.get("clip") or {}).get("speed", 1.0) or 1.0)


def ensure_speed(cfg, state, clip, src, d):
    """Clips made before clip.speed was set (or at another speed) are sped up before they go out, and the stored
    copy behind the download link is replaced too. Returns the file to send."""
    want = clip_speed(cfg)
    have = float(clip.get("speed") or 1.0)
    if abs(want - have) < 0.01:
        return src
    out = Path(d) / f"{clip['id']}_x{want:g}.mp4"
    render.speed_up(src, out, want / have, ffmpeg=clipqueue._ffmpeg())
    clip["duration"] = round(float(clip.get("duration") or 0) * have / want, 1)
    clip["speed"] = want
    if not DRY_RUN and clip.get("asset_id"):
        try:
            old = clip["asset_id"]
            clip["asset_id"], clip["media_url"] = services.release_upload(out, f"{clip['id']}_x{want:g}.mp4")
            services.release_delete(old)
        except Exception as e:
            log.warning("couldn't replace the stored copy of %s: %s", clip["id"], e)
    clip["local_file"] = None
    save_state(state)
    return out


def make_clip(cfg, state, vod, p, words, d, src=None, src_offset=None):
    cid = _clip_id(vod["id"], p["start"])
    if cid in state["clips"]:
        return state["clips"][cid]
    from . import clean
    if clean.enabled(cfg):   # no strong swearing in on-screen text or post text (YouTube age-restricts for it)
        p = dict(p, **{k: clean.mask(p[k]) for k in ("hook", "title", "caption", "punch_text") if p.get(k)})
    if src is None:
        dl_start = max(0, int(p["start"]) - 2)
        src = fetch.download_section(vod, dl_start, p["end"] + 2, d / f"{cid}_src.mp4")
        src_offset = p["start"] - dl_start
    out = d / f"{cid}.mp4"
    words = caption_words(cfg, src, src_offset, p, d / f"{cid}_cap.wav", fallback=words)
    # Look at the frames first: find the webcam and where the action is, so the vertical crop keeps what matters.
    p = dict(p, **place_punchline(p, words))   # only show a punchline that's really said, exactly when it's said
    if clean.enabled(cfg) and p.get("punch_text"):
        p["punch_text"] = clean.mask(p["punch_text"])
    p["mask_profanity"] = clean.enabled(cfg)
    look = vision.analyze_frames(cfg, src, src_offset, p["end"] - p["start"], d)
    facecam = vod["streamer"].get("facecam") or look["facecam"]
    layout = cfg["clip"].get("layout", "auto")
    if layout == "auto":
        layout = look["layout"]
    speed = clip_speed(cfg)
    st = vod["streamer"]
    commentary = cfg["clip"].get("commentary", False)
    if st.get("context") and (p.get("context") or "").strip():   # campaign rule: give the clip context
        p = dict(p, commentary=[{"at": 0.0, "text": p["context"].strip()[:70]}])
        commentary = True
    render.render(src, src_offset, p, words, out, facecam=facecam, focus_x=look["focus_x"],
                  commentary=commentary, layout=layout, speed=speed, handle=st.get("handle"))
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
        "start": p["start"], "end": p["end"], "duration": round((p["end"] - p["start"]) / speed, 1), "speed": speed,
        "score": int(p["score"]), "hook": p["hook"], "title": p["title"], "caption": p["caption"],
        "tags": p.get("tags", []), "why": p.get("why", ""), "hook_options": p.get("hook_options", []), "hook_score": p.get("hook_score"),
        "punch_text": p.get("punch_text", ""), "punch_at": p.get("punch_at", 0), "media_url": media_url,
        "facecam": bool(facecam), "layout": "cam+" + layout if facecam else layout, "framing": look["why"],
        "said": " ".join(w["w"] for w in words if p["start"] <= w["s"] < p["end"])[:600], "file": out.name,
        "context": p.get("context", ""), "handle": st.get("handle"), "preapproval": bool(st.get("preapproval")),
        "campaign": st.get("campaign") if is_campaign(st) else None,
        "created": now_utc().isoformat(), "posts": {}, "issue": None, "local_file": str(out), "asset_id": asset_id,
    }
    auto = cfg["review"]["mode"] == "auto"
    clip["status"] = ("approved" if clip["score"] >= cfg["clip"]["min_score"] else "rejected") if auto else "queued"
    if not DRY_RUN:
        verdict = {"approved": ("Waiting in the queue; it'll be emailed to you at a posting time."
                                if clipqueue.email_mode(cfg) else "Will post automatically."),
                   "rejected": "Below the score bar, won't post.",
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
    from . import crflow
    total = crflow.daily_total(cfg)
    if total and os.environ.get("MAC_RUNS_CLIPPER") == "1":
        # one daily total for everything: campaign clips (paid) claim first, regular posts get the rest
        per = max(0, total - min(total, crflow.claimed_today(cfg)))
    return per


def _slots(cfg, n, state=None):
    from . import timing
    if state is not None and clipqueue.email_mode(cfg) and timing.settings(cfg)["enabled"]:
        return timing.plan(cfg, state, now_local(cfg).date().isoformat(), n, cfg.get("timezone", "UTC"))
    slots = list(cfg["posting"]["slots"])
    ins = load_json(INSIGHTS_FILE, {})
    if cfg["posting"].get("adaptive_slots") and ins.get("best_slots"):
        slots = ins["best_slots"]
    return sorted(slots[:n])


def _caption(cfg, clip, platform):
    """Viral-style description: one casual reaction line, a credit, then 4-6 hashtags incl. the streamer's."""
    from . import clean
    if clean.enabled(cfg):
        clip = dict(clip, caption=clean.mask(clip.get("caption") or ""))
    tags = list(cfg["posting"]["hashtags"])
    if clip["streamer"] not in ("manual", "test"):
        tags.append("#" + "".join(ch for ch in clip["streamer"].lower() if ch.isalnum()))
    tags = " ".join(dict.fromkeys(tags))
    credit = f"Streamer: {clip['streamer']}" if clip["streamer"] not in ("manual", "test") else ""
    if clip.get("handle"):
        credit = f"Streamer: {clip['handle']}"
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
def _has_video(c):
    """The clip's video is still on disk (or a non-local download address is known)."""
    if c.get("local_file") and os.path.exists(c["local_file"]):
        return True
    url = str(c.get("media_url") or "")
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return os.path.exists(unquote(urlparse(url).path))
    return bool(url)


def _keep_for_tiktok(c):
    """Posted on YouTube in the last 3 days and not on TikTok yet: a candidate for the daily TikTok, keep its video."""
    try:
        if not clipqueue.tiktok_daily(load_config()):
            return False
    except Exception:
        return False
    posts = c.get("posts") or {}
    since = (now_utc() - timedelta(days=3)).isoformat()
    return "tiktok" not in posts and ((posts.get("youtube") or {}).get("posted_at") or "") >= since


def cleanup_assets(state):
    """Delete the stored video of clips that left the queue, and close their GitHub issues."""
    for c in state["clips"].values():
        if c.get("status") == "approved" or _keep_for_tiktok(c):
            continue
        if c.get("asset_id") and not c.get("asset_deleted"):
            try:
                services.release_delete(c["asset_id"])
                c["asset_deleted"] = True
            except Exception as e:
                log.info("couldn't delete stored video for %s: %s", c["id"], e)
        if c.get("issue") and not c.get("issue_closed") and not DRY_RUN:
            try:
                services.gh_comment(c["issue"], f"Left the queue: {c['status']}.")
                services.gh_update(c["issue"], state="closed")
                c["issue_closed"] = True
            except Exception as e:
                log.info("couldn't close issue for %s: %s", c["id"], e)


def emails_due(cfg, state, now):
    """How many posting emails should have gone out by now today, and how many did."""
    sent = clipqueue.emailed_on(state, now.date().isoformat(), cfg.get("timezone", "UTC"))
    slots = _slots(cfg, _quota(cfg), state)
    due = sum(1 for sl in slots if now.strftime("%H:%M") >= sl)
    return due, sent


def send_package(cfg, state, clip, test=False):
    """Email one clip with everything needed to post it. Returns the Message-ID."""
    from . import mail
    d = WORK / f"email_{clip['id']}"
    d.mkdir(parents=True, exist_ok=True)
    attach = None
    src = clip.get("local_file")
    if not (src and os.path.exists(src)):
        src = services.download(clip["media_url"], d / "clip.mp4") if clip.get("media_url") else None
    if src:
        src = ensure_speed(cfg, state, clip, src, d)   # raises rather than send a clip at the old speed
    pkg = clipqueue.package(cfg, clip, clipqueue.queue(state), state)
    from . import crflow
    if src and not test and crflow.applies(cfg, clip):   # Mac: campaign clips go to Content Rewards as a draft first
        mid = crflow.enqueue(cfg, clip, src, pkg)
        shutil.rmtree(d, ignore_errors=True)
        now = now_utc()
        clip.update(emailed_at=now.isoformat(), email_message_id=mid, sent_caption=pkg["tiktok_caption"],
                    sent_title=pkg["yt_title"])
        em = state.setdefault("email", {})
        em.setdefault("log", []).append({"date": now_local(cfg).date().isoformat(), "at": now.isoformat(),
                                         "clip": clip["id"], "subject": "[draft] " + pkg["subject"], "test": False})
        em["log"] = em["log"][-200:]
        print(f"::notice::campaign clip queued for a Content Rewards draft: {pkg['subject']}", flush=True)
        return mid
    try:
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


def auto_platforms(cfg):
    """Platforms the bot posts itself through Blotato (posting.auto_post), once BLOTATO_API_KEY is set."""
    if not os.environ.get("BLOTATO_API_KEY") or DRY_RUN or os.environ.get("MAC_RUNS_CLIPPER") != "1":
        return []   # the Mac only; on GitHub, Blotato stays the launch-mode path
    want = (cfg.get("posting") or {}).get("auto_post") or []
    return [p for p in cfg["posting"]["platforms"] if p in want]


def deliver(cfg, state, clip):
    """At a posting slot: post through Blotato in launch mode with automatic posting, otherwise email it.
    A failed automatic post falls back to the email, so the slot isn't lost."""
    from . import launch
    if launch.auto_post(cfg, state) and not DRY_RUN and not clip.get("preapproval"):
        try:
            return auto_post_clip(cfg, state, clip)
        except Exception as e:
            log.exception("automatic post failed: %s", e)
            print(f"::warning::automatic post failed ({str(e)[:150]}); emailing the clip instead", flush=True)
    auto = auto_platforms(cfg) if not clip.get("preapproval") else []
    test_day = str((cfg.get("posting") or {}).get("hand_youtube_test") or "")
    if "youtube" in auto and test_day == now_local(cfg).date().isoformat() and not os.environ.get("HAND_TEST_OFF") \
            and state.get("hand_youtube_test") != test_day:
        # one clip that day comes to Elie to upload on YouTube by hand, to compare with the Blotato ones
        state["hand_youtube_test"] = test_day
        clip["hand_test"] = True
        auto = [p for p in auto if p != "youtube"]
        from . import tg
        tg.send("✋ Test for the views problem: upload the next clip on YouTube yourself, from the YouTube app, with "
                "the title and description in its message. Today's other clips go through Blotato, so in 2 days we "
                "can see if the hand-uploaded one does better.")
    if auto:   # e.g. YouTube through Blotato; TikTok still comes to Elie
        try:
            auto_post_clip(cfg, state, clip, platforms=auto, finish=False)
        except Exception as e:
            log.exception("automatic post failed: %s", e)
            print(f"::warning::automatic {', '.join(auto)} post failed ({str(e)[:150]}); sending it to you instead",
                  flush=True)
        left = [p for p in cfg["posting"]["platforms"] if p not in clip.get("posts", {})
                and not (p == "tiktok" and (clipqueue.tiktok_paused(cfg) or clipqueue.tiktok_daily(cfg)))]
        if not left:
            clip.update(status="posted", emailed_at=now_utc().isoformat(), auto_posted=True)
            save_state(state)
            return None
    try:
        return send_package(cfg, state, clip)
    except Exception as e:   # Gmail down: keep the clip in the queue and try again on a later run
        from . import mail
        log.warning("couldn't email the clip: %s", e)
        if not isinstance(e, mail.MailDown):
            print(f"::warning::couldn't email the clip ({type(e).__name__}: {str(e)[:150]}); it stays in the queue",
                  flush=True)
        clip.pop("extra", None)
        return None


def auto_post_clip(cfg, state, clip, platforms=None, finish=True):
    """Upload one queued clip to Blotato and post it to every platform (or just `platforms`). Raises if none worked.
    finish=False leaves the clip in the queue for the platforms Elie posts himself."""
    d = WORK / f"auto_{clip['id']}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        src = clip.get("local_file")
        if not (src and os.path.exists(src)):
            src = services.download(clip["media_url"], d / "clip.mp4")
        src = ensure_speed(cfg, state, clip, src, d)
        url = services.blotato_upload(src)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    now = now_local(cfg)
    ok = []
    for platform in platforms or cfg["posting"]["platforms"]:
        if platform in clip.get("posts", {}):
            continue
        try:
            acct = services.blotato_account_id(platform)
            sub = services.blotato_post(platform, acct, url, _caption(cfg, clip, platform), title=clip["title"],
                                        ai_label=cfg["posting"].get("label_ai_generated"))
            status, info = services.blotato_wait(sub)
        except Exception as e:
            log.warning("%s post failed: %s", platform, e)
            continue
        if status == "failed":
            log.warning("%s post failed: %s", platform, info)
            continue
        clip.setdefault("posts", {})[platform] = {
            "via": "blotato", "submission": sub, "status": status, "url": info if status == "published" else None,
            "date": now.date().isoformat(), "posted_at": now_utc().isoformat(), "hour": now.strftime("%H:00"),
            "views": None, "history": []}
        ok.append(platform)
    shutil.rmtree(d, ignore_errors=True)
    if not ok:
        raise RuntimeError("no platform accepted the post")
    if not finish:
        clip["blotato_url"] = url
        print(f"::notice::posted automatically to {', '.join(ok)}: {clip['hook']}", flush=True)
        save_state(state)
        return None
    clip.update(status="posted", emailed_at=now_utc().isoformat(), auto_posted=True, blotato_url=url)
    print(f"::notice::posted automatically to {', '.join(ok)}: {clip['hook']}", flush=True)
    save_state(state)
    return None


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


def tiktok_daily_post(cfg, state, now=None):
    """posting.tiktok_daily: one TikTok a day, at the same time. Picks the recent clip that did best on YouTube
    (it already proved itself), posts it through Blotato, and tells Elie. Campaign clips are left out (their drafts
    don't include TikTok). Returns the clip posted, or None."""
    at = clipqueue.tiktok_daily(cfg)
    if not at:
        return None
    now = now or now_local(cfg)
    hh, mm = (int(x) for x in at.split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    rec = state.setdefault("tiktok_daily", {})
    today = now.date().isoformat()
    if rec.get("tries_day") != today:
        rec.update(tries_day=today, tries=0)
    if now < start or now > start + timedelta(hours=2) or rec.get("posted_day") == today or rec.get("tries", 0) >= 3:
        return None   # once a day, inside 2 hours of its time; 3 tries at most (they're 15 minutes apart)
    rec["tries"] = rec.get("tries", 0) + 1
    since = (now_utc() - timedelta(days=3)).isoformat()

    def yt_views(c):
        yt = (c.get("posts") or {}).get("youtube") or {}
        return yt.get("views") or 0

    pool = [c for c in state["clips"].values()
            if "tiktok" not in (c.get("posts") or {}) and not c.get("preapproval") and not streamer_off(cfg, c.get("streamer"))
            and ((c.get("posts") or {}).get("youtube") or {}).get("posted_at", "") >= since and _has_video(c)]
    pool.sort(key=lambda c: (yt_views(c), c.get("score") or 0), reverse=True)
    # then the best clips waiting in the queue, in case nothing recent still has its video
    pool += sorted((c for c in clipqueue.queue(state) if not c.get("preapproval") and _has_video(c)
                    and c not in pool), key=lambda c: c.get("score") or 0, reverse=True)
    if not pool:
        print("::notice::daily TikTok: no clip to post today", flush=True)
        return None
    err = None
    for clip in pool[:4]:   # a clip whose video went missing is skipped, not the whole day
        try:
            auto_post_clip(cfg, state, clip, platforms=["tiktok"], finish=False)
            err = None
            break
        except Exception as e:
            err = e
            log.warning("daily TikTok: %s failed (%s), trying the next one", clip["id"], e)
    if err is not None:
        e = err
        print(f"::warning::daily TikTok post failed: {str(e)[:200]}", flush=True)
        try:
            from . import tg
            if rec["tries"] >= 3:
                tg.send(f"⚠️ Today's TikTok didn't go up after 3 tries ({str(e)[:150]}). If it mentions Blotato, "
                        "check Accounts there shows TikTok connected. It tries again tomorrow at the same time.")
        except Exception:
            pass
        save_state(state)
        return None
    rec.update(clip=clip["id"], posted_day=today)
    url = ((clip.get("posts") or {}).get("tiktok") or {}).get("url")
    try:
        from . import tg
        tg.info(f"🎵 Today's TikTok is up: \"{clip['hook']}\"" + (f"\n{url}" if url else ""), silent=True)
    except Exception:
        pass
    save_state(state)
    return clip


def _ramp_guard(cfg, state, now):
    """Once a day: stay at a few posts if the last days' Shorts got almost no views (posting.ramp_guard)."""
    from . import crflow, tg
    today = now.date().isoformat()
    if state.get("ramp_checked") == today:
        return
    state["ramp_checked"] = today
    try:
        held, med, n = crflow.ramp_check(cfg, state["clips"])
    except Exception as e:
        log.warning("ramp check: %s", e)
        return
    if held:
        g = cfg["posting"]["ramp_guard"]
        tg.send(f"📉 Your YouTube Shorts from the last 3 days have a median of {med:,.0f} views ({n} posts), so today "
                f"stays at {g.get('hold_at', 3)} posts instead of {crflow._step(cfg, today).get('per_day') or 'more'}. "
                "I check again tomorrow and step up once views come back.\n\nIf you want to post more anyway, tell "
                "Claude to turn the ramp guard off.")


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
    state["tiktok_paused"] = bool(clipqueue.tiktok_paused(cfg))
    for note in clipqueue.refresh_posted(cfg, state):
        print(f"::notice::{note}", flush=True)
    for c in clipqueue.expire(cfg, state):
        print(f"::notice::expired (too old to post): {c['hook']}", flush=True)
    for c in clipqueue.queue(state):   # streamers no longer clipped (avoided, or a paused small campaign)
        if streamer_off(cfg, c.get("streamer")):
            c["status"] = "skipped"
            log.info("dropped %s: %s isn't clipped right now", c["id"], c.get("streamer"))
    cleanup_assets(state)
    save_state(state)
    now = now_local(cfg)
    due, sent = emails_due(cfg, state, now)
    q = clipqueue.queue(state)
    log.info("queue %d, emails due today %d, sent %d", len(q), due, sent)
    em = state.setdefault("email", {})
    _ramp_guard(cfg, state, now)
    try:
        tiktok_daily_post(cfg, state)
    except Exception as e:
        log.exception("daily TikTok: %s", e)
    from . import crflow
    for c in [c for c in clipqueue.queue(state) if not c.get("emailed_at") and crflow.applies(cfg, c)]:
        # a pre-approval campaign clip goes up as a draft as soon as it's made: approval takes hours, and it's
        # posted when approved, so it doesn't use (or wait for) a posting slot
        c["extra"] = True
        deliver(cfg, state, c)
    if em.pop("send_next", False):   # you replied "next": one more clip now, on top of the day's count
        clip = clipqueue.pick_for_email(state)
        if clip:
            clip["extra"] = True
            deliver(cfg, state, clip)
    elif em.get("paused") and not FORCE:
        log.info("posting emails are paused (reply resume to turn them back on)")
    elif FORCE or sent < due:
        clip = clipqueue.pick_for_email(state)
        spaced = True
        if clip and "youtube" in auto_platforms(cfg):   # YouTube posts spread out, paid campaign clips first
            entries = crflow.load()
            spaced = (crflow.youtube_ready(cfg, state["clips"], entries)
                      and not (crflow.per_day(cfg) and crflow.campaign_waiting(entries, cfg=cfg)))
        if clip and spaced:
            deliver(cfg, state, clip)
        elif clip:
            log.info("a posting slot is due; waiting so it doesn't land right after the last YouTube post")
        else:
            log.info("a posting slot is due but nothing new is waiting")
    from . import launch
    launch.daily(cfg, state, dry_run=DRY_RUN)
    health.check_quiet(cfg, state, len(clipqueue.queue(state)))
    last = state.get("status_updated")
    if not last or now_utc() - datetime.fromisoformat(last) > timedelta(minutes=55):
        try:
            _status_issue(cfg, state)
        except Exception as e:
            log.info("dashboard update failed: %s", e)
    save_state(state)


# ------------------------------------------------------------------ analytics + prune
def _ran(state, job):
    state.setdefault("last_run", {})[job] = now_utc().isoformat()


def analytics():
    cfg, state = load_config(), load_state()
    _ran(state, "analytics")
    if clipqueue.email_mode(cfg):   # posting is by hand: no Blotato stats; look for paid campaigns instead
        from . import campaigns
        from . import launch
        try:
            campaigns.scan(cfg, state)
            launch.refresh_streamers(cfg, state, dry_run=DRY_RUN)   # GTA 6 launch: paid campaigns, then the biggest
        finally:
            save_state(state)
        _status_issue(cfg, state)
        return
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


def _status_email_mode(cfg, state):
    """Dashboard body for email mode: queue, today's posting times, streamers, campaigns, spend, recent posts."""
    from . import batch, campaigns
    from .common import spend_today
    now = now_local(cfg)
    q = clipqueue.queue(state)
    out = [f"Updated {now:%a %b %d %I:%M %p}. Day {days_since_start(cfg)}: {_quota(cfg)} posting email(s) a day."]
    slots = _slots(cfg, _quota(cfg), state)
    due, sent = emails_due(cfg, state, now)
    nxt = next((sl for sl in slots if now.strftime("%H:%M") < sl), None)
    out.append(f"Today's posting times: {', '.join(slots) or 'none'} ({sent} sent so far"
               + (f", next at {nxt})" if nxt else ")"))
    if (state.get("email") or {}).get("paused"):
        out.append("**Emails are paused.** Reply `resume` to any bot email to turn them back on.")
    from . import launch, mail
    if mail.down_until():
        out.append(f"**Gmail refused the bot's login.** It tries again after {mail.down_until()[:16]} UTC; clips keep "
                   "collecting. If this stays, check the channel Gmail and its app password.")
    L, lrec = cfg.get("_launch") or {}, state.get("launch") or {}
    if L.get("on"):
        out.append(f"**GTA 6 launch mode is on:** {L['multiplier']}x posts, "
                   + ("posting automatically through Blotato." if L.get("auto") else "still by email."))
    elif launch.settings(cfg)["enabled"]:
        out.append(f"GTA 6 launch mode starts in {launch.days_left(cfg, state)} day(s) "
                   f"({launch.launch_date(cfg, state):%b %d}). Automatic posting: "
                   + ("ready." if lrec.get("blotato_ok") else "not set up yet (reminder email a week before)."))
    if clipqueue.tiktok_paused(cfg):
        out.append(f"**TikTok is paused until {clipqueue.tiktok_paused(cfg)}.** Emails are YouTube only until then.")
    stalled = clipqueue.dead_on_tiktok(state)
    if stalled:
        out.append(f"TikTok stopped showing recent clips of {', '.join(sorted(stalled))} (last 3 under 10 views), "
                   "so their clips are ranked lower until one does well again.")
    out.append(f"\n### Queue ({len(q)}/{clipqueue.settings(cfg)['max_size']})")
    out += [f"- {c['score']}/10 · {c['streamer']} · {int((now_utc() - datetime.fromisoformat(c['created'])).total_seconds() // 3600)}h old"
            f"{' · emailed' if c.get('emailed_at') else ''} · {c['hook']}" for c in q] or ["- empty: it's looking for clips"]
    pend = batch.pending()
    if pend:
        out.append(f"\n{len(pend)} stream(s) being judged at half price: " + ", ".join(r['vod']['streamer']['name'] for r in pend))
    names = [f"{x['name']}{' (paid campaign)' if x.get('campaign') else ''}" for x in cfg.get("streamers", [])
             if x.get("enabled", True)]
    out.append("\n### Streamers being clipped\n" + ", ".join(names))
    camps = [r for r in (state.get("campaigns") or {}).values() if r.get("status") in ("notified", "joined", "auto")]
    if camps:
        out.append("\n### Paid campaigns\n" + "\n".join(
            f"- {r['name']} ({r.get('rate') or '?'} per 1K) · {r['status']} · {r.get('join_url') or r['url']}" for r in camps))
    week = sum(v for d, v in (state.get("spend") or {}).items()
               if d >= (now_utc() - timedelta(days=7)).date().isoformat())
    cap = (cfg.get("limits") or {}).get("claude_usd_per_day")
    out.append(f"\nClaude spend: ${spend_today(state):.2f} today" + (f" (cap ${float(cap):.2f})" if cap else "")
               + f", ${week:.2f} in the last 7 days.")
    return "\n".join(out)


def _status_issue(cfg, state):
    if not os.environ.get("GITHUB_TOKEN"):   # on the Mac the dashboard is the Telegram "status" message
        return
    rows = []
    posts = [(c, pl, p) for c in state["clips"].values() for pl, p in c["posts"].items()]
    posts.sort(key=lambda x: x[2].get("posted_at") or x[2].get("seen_at") or "", reverse=True)
    for c, pl, p in posts[:40]:
        rows.append(f"| {p.get('date', '')} | {pl} | {c['streamer']} | {c['hook'][:40]} | {p.get('views') if p.get('views') is not None else '…'} "
                    f"| {'hidden' if p.get('hidden') else ('low' if p.get('prune_flagged') else '')} | {p.get('url') or p.get('error') or ''} |")
    total = defaultdict(int)
    for c, pl, p in posts:
        total[pl] += p.get("views") or 0
    head = (_status_email_mode(cfg, state) if clipqueue.email_mode(cfg) else
            f"Updated {now_local(cfg):%b %d %I:%M %p}. Day {days_since_start(cfg)} since launch, "
            f"{_quota(cfg)} posts/day per platform.\n\nQueue: {len(_queued(state))} clips ready.")
    body = (head + "\n\n### Posts\n"
            + (" · ".join(f"**{pl}**: {v:,} total views" for pl, v in total.items()) or "nothing posted yet")
            + "\n\n| date | platform | streamer | hook | views | flag | link |\n|---|---|---|---|---|---|---|\n"
            + "\n".join(rows))
    if not DRY_RUN:
        services.gh_upsert_pinned("status", "Clip dashboard", body)
    state["status_updated"] = now_utc().isoformat()
    return body


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


def _weekly_email(cfg, rows, out):
    """Monday email: best and worst clips by views, and what the picker will do differently (email mode only)."""
    from . import mail
    if not clipqueue.email_mode(cfg) or not mail.configured() or DRY_RUN:
        return
    if not rows:
        return   # nothing posted with numbers yet; no point emailing

    def line(r):
        extra = f", {r['likes']} likes" if r.get("likes") is not None else ""
        return f"- {r['views']:,} views{extra} on {r['platform']}: {r['hook']} ({r['seconds']:.0f}s, posted {r.get('hour') or '?'})"
    ranked = sorted(rows, key=lambda r: r["views"], reverse=True)
    total = sum(r["views"] for r in rows)
    text = [f"{len(rows)} posts with numbers, {total:,} views in total.", "", "Best:"]
    text += [line(r) for r in ranked[:3]]
    if len(ranked) > 3:
        text += ["", "Worst:"] + [line(r) for r in ranked[-3:][::-1]]
    if out:
        text += ["", "What's working:", out["summary"], "", "Rules the clip picker follows this week:", out["playbook"]]
        from . import timing
        if timing.settings(cfg)["enabled"]:
            text += ["", "Posting-time test (views a few hours after posting):"] + timing.report(cfg, load_state(),
                                                                                                cfg.get("timezone", "UTC"))
        elif out.get("best_slots"):
            text += ["", f"Posting emails will now come at: {', '.join(out['best_slots'])} (Pacific)."]
        if out.get("target_seconds"):
            lo, hi = out["target_seconds"]
            text += [f"Clip length it will aim for: {lo} to {hi} seconds."]
    else:
        text += ["", "Not enough posts yet to change anything (it needs 6 with view counts). Keep posting."]
    try:
        mail.send(f"Weekly: {total:,} views from {len(rows)} posts", "\n".join(text))
    except Exception as e:
        log.warning("weekly email failed: %s", e)


def learn(client=None):
    import anthropic
    cfg, state = load_config(), load_state()
    _ran(state, "learn")
    save_state(state)
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
        _weekly_email(cfg, rows, None)
        return
    client = client or anthropic.Anthropic()
    out = {}
    for _ in range(2):   # a long playbook can cut the answer short; ask again once if a field is missing
        out = highlights.ask_tool(client, cfg["models"]["learn"], LEARN_PROMPT.format(data=json.dumps(rows)),
                                  LEARN_SCHEMA, max_tokens=6000) or {}
        if out.get("playbook") and out.get("summary"):
            break
    if not out.get("playbook"):   # keep last week's rules rather than wiping them
        out["playbook"] = PLAYBOOK_FILE.read_text() if PLAYBOOK_FILE.exists() else ""
    out.setdefault("summary", "")
    out["summary"] = out["summary"] or "(Claude didn't write a summary this week; the numbers above are complete.)"
    PLAYBOOK_FILE.write_text(out["playbook"])
    ts = out.get("target_seconds")
    lo_hi = cfg["clip"]["min_seconds"], cfg["clip"]["max_seconds"]
    if not (isinstance(ts, list) and len(ts) == 2 and all(isinstance(x, (int, float)) for x in ts) and ts[0] < ts[1]):
        ts = None
    else:  # keep it inside the configured limits
        ts = [int(max(lo_hi[0], ts[0])), int(min(lo_hi[1], ts[1]))]
        ts = ts if ts[0] < ts[1] else None
    prev = load_json(INSIGHTS_FILE, {})
    if ts is None and "target_seconds" not in out:   # answer cut short: keep last week's values
        ts = prev.get("target_seconds")
    INSIGHTS_FILE.write_text(json.dumps({"best_slots": out.get("best_slots") or prev.get("best_slots") or [],
                                         "target_seconds": ts, "updated": now_utc().isoformat()}))
    _weekly_email(cfg, rows, dict(out, target_seconds=ts))
    if not DRY_RUN:
        services.gh_upsert_pinned("weekly-report", f"Weekly report {now_local(cfg):%b %d}",
                                  f"{out['summary']}\n\n### Playbook the picker now follows\n\n{out['playbook']}")


def rerender():
    """CLIP_ID=<id>: make a queued clip again from its stream with the current settings (same cut, hook, title and
    caption), replace its stored video, and email it again. For fixing a clip that went out with an old look."""
    cfg, state = load_config(), load_state()
    cid = os.environ.get("CLIP_ID", "").strip()
    old = state["clips"].get(cid)
    if not old:
        raise RuntimeError(f"no clip {cid!r}")
    streamer = next((s for s in cfg.get("streamers", []) if s["name"] == old["streamer"]),
                    {"name": old["streamer"], "platform": "twitch", "channel": old["streamer"].lower()})
    vod = {"id": old["vod"], "url": old["vod_url"], "title": "", "streamer": streamer}
    keep = ("start", "end", "score", "hook", "title", "caption", "tags", "why", "hook_options", "hook_score",
            "punch_text", "punch_at")
    p = {k: old.get(k) for k in keep}
    del state["clips"][cid]   # same id: make_clip would otherwise return the old one
    d = WORK / f"rerender_{cid}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        new = make_clip(cfg, state, vod, p, [], d)
    except Exception:
        state["clips"][cid] = old
        save_state(state)
        raise
    for k in ("issue_closed",):
        new.pop(k, None)
    if old.get("asset_id") and old.get("asset_id") != new.get("asset_id") and not DRY_RUN:
        try:
            services.release_delete(old["asset_id"])
        except Exception as e:
            log.info("couldn't delete the old video: %s", e)
    new["status"] = "approved"
    save_state(state)
    print(f"::notice::made again: {new['hook']} ({new['duration']}s)", flush=True)
    deliver(cfg, state, new)
    save_state(state)
