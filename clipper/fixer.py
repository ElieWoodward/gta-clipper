"""One-tap fix for a campaign draft the reviewer sent back ("needs changes").

1. The Mac's check sees the note and texts it with a "✂️ Fix it" button (mac.compare).
2. Elie taps it: the Mac picks the draft the note belongs to and starts this job (python -m clipper revise).
3. One small Claude call reads the note and the clip and says what to change: hook, title, caption, and how much
   to trim from the start or end. If the note asks for something the bot can't do (different gameplay, music, a
   new edit), it says so instead.
4. The clip is made again from the stream with those changes and sent to Telegram for a look, with
   "📤 Send revision" and "✋ I'll do it". Send revision uploads it on Content Rewards (mac.process_fixes).

Cost: one call of about 1,500 tokens in and 300 out (about a cent), plus the usual frame check of the new render.
"""
import json
import os
import shutil

from .common import DATA, WORK, load_config, load_state, log, now_utc, save_state

JOB = DATA / "fix_job.json"
MAX_SHIFT = 20.0       # seconds a fix may move the start or end
MIN_LEN = 8.0          # never cut a clip shorter than this

PROMPT = """A brand reviewer sent back a short vertical clip (TikTok/YouTube Shorts) from a GTA RP Twitch stream.
Decide how to fix it so it passes review.

Reviewer's note: {note}

The clip now:
- Hook (big text box at the top): {hook}
- YouTube title: {title}
- TikTok caption: {caption}
- Length: {length:.0f}s (cut from the stream at {start:.0f}s to {end:.0f}s)
- What is said in it: {said}
- Campaign rules: {rules}

You can change: the hook, the title, the caption, and trim or extend the start and the end by up to {shift:.0f}
seconds each (start_shift/end_shift in seconds: positive start_shift cuts the start later, negative end_shift ends it
earlier). Keep it at least {minlen:.0f}s. Leave a field empty to keep it.
If the note asks for anything else (different footage, music, a whole new edit, removing the webcam), set can_fix
to false and say in one plain sentence what Elie has to do.
Hooks: under 9 words, plain words, no hashtags, say what happens without spoiling the punchline."""

SCHEMA = {"type": "object", "required": ["can_fix", "why"], "properties": {
    "can_fix": {"type": "boolean"}, "why": {"type": "string"}, "hook": {"type": "string"},
    "title": {"type": "string"}, "caption": {"type": "string"},
    "start_shift": {"type": "number"}, "end_shift": {"type": "number"}}}


def decide(cfg, clip, note, rules="", client=None):
    from . import highlights
    import anthropic
    client = client or anthropic.Anthropic()
    prompt = PROMPT.format(note=note, hook=clip.get("hook", ""), title=clip.get("title", ""),
                           caption=clip.get("caption", ""), length=float(clip.get("duration") or 0),
                           start=float(clip.get("start") or 0), end=float(clip.get("end") or 0),
                           said=(clip.get("said") or "")[:900], rules=rules or "none given",
                           shift=MAX_SHIFT, minlen=MIN_LEN)
    model = (cfg.get("models") or {}).get("editor") or cfg["models"]["judge"]
    return highlights.ask_tool(client, model, prompt, SCHEMA, max_tokens=800)


def apply(clip, ch):
    """The pick for make_clip: the old cut and words with the changes applied (trims clamped)."""
    def clamp(x):
        try:
            return max(-MAX_SHIFT, min(MAX_SHIFT, float(x or 0)))
        except (TypeError, ValueError):
            return 0.0
    start = float(clip["start"]) + clamp(ch.get("start_shift"))
    end = float(clip["end"]) + clamp(ch.get("end_shift"))
    if end - start < MIN_LEN:
        start, end = float(clip["start"]), float(clip["end"])
    keep = ("score", "hook", "title", "caption", "tags", "why", "hook_options", "hook_score", "context")
    p = {k: clip.get(k) for k in keep}
    p.update(start=max(0.0, start), end=end, punch_text="", punch_at=0)
    for k in ("hook", "title", "caption"):
        if (ch.get(k) or "").strip():
            p[k] = ch[k].replace("*", "").strip()
    return p


def _rules(cfg, clip):
    st = next((s for s in cfg.get("streamers") or [] if s.get("name") == clip.get("streamer")), {})
    bits = [" ".join(str(st[k]).split()) for k in ("picker_notes", "rules", "title_rule") if st.get(k)]
    if st.get("handle"):
        bits.append(f"show {st['handle']} on screen")
    return "; ".join(str(b) for b in bits)


def revise():
    """The job: read data/fix_job.json, ask Claude, make the clip again, send it for a look."""
    from . import crflow, highlights, jobs, tg, clipqueue
    job = json.loads(JOB.read_text())
    cfg, state = load_config(), load_state()
    cid = job["clip_id"]
    clip = state["clips"].get(cid)
    if not clip:
        raise RuntimeError(f"no clip {cid!r} in the state")
    if highlights.budget_left(cfg, state) <= 0:
        job["status"] = "failed"
        JOB.write_text(json.dumps(job))
        tg.send("✂️ Today's Claude budget is used up, so I can't work out the fix right now. Tap ✂️ Fix it again "
                "tomorrow, or fix it yourself: Drafts > Needs changes > Upload revision.")
        return
    ch = decide(cfg, clip, job["note"], _rules(cfg, clip))
    save_state(state)   # records the spend
    if not ch.get("can_fix"):
        job.update(status="cant", why=ch.get("why"))
        JOB.write_text(json.dumps(job))
        tg.send(f"✋ I can't fix this one myself: {ch.get('why')}\n\nReviewer said: {job['note']}\n\n"
                "Fix it by hand, then Drafts > Needs changes > Upload revision on Content Rewards.")
        return
    p = apply(clip, ch)
    streamer = next((s for s in cfg.get("streamers", []) if s["name"] == clip["streamer"]),
                    {"name": clip["streamer"], "platform": "twitch", "channel": clip["streamer"].lower()})
    vod = {"id": clip["vod"], "url": clip["vod_url"], "title": "", "streamer": streamer}
    d = WORK / f"revise_{cid}"
    d.mkdir(parents=True, exist_ok=True)
    old = state["clips"].pop(cid)
    try:
        new = jobs.make_clip(cfg, state, vod, p, [], d)
    except Exception:
        state["clips"][cid] = old
        save_state(state)
        raise
    state["clips"].pop(new["id"], None)
    out = WORK / "cr_drafts" / f"{cid}_rev{now_utc():%H%M%S}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(new["local_file"], out)
    for k in ("start", "end", "duration", "hook", "title", "caption", "said", "punch_text", "punch_at", "speed"):
        if k in new:
            old[k] = new[k]
    old.update(local_file=str(out), revised=now_utc().isoformat())
    state["clips"][cid] = old
    save_state(state)
    entries = crflow.load()   # the draft's post package uses the new text once it's approved
    e = crflow.by_token(entries, job["token"])
    if e:
        pkg = clipqueue.package(cfg, old, clipqueue.queue(state), state)
        e.update(hook=old["hook"], title=old["title"], caption=old["caption"], duration=old["duration"],
                 blocks=pkg["blocks"], subject=pkg["subject"], revision_file=str(out))
        crflow.save(entries)
    shutil.rmtree(d, ignore_errors=True)
    job.update(status="ready", file=str(out), why=ch.get("why"))
    JOB.write_text(json.dumps(job))
    changed = [f"Hook: {old['hook']}" if (ch.get("hook") or "").strip() else "",
               f"Title: {old['title']}" if (ch.get("title") or "").strip() else "",
               f"Caption: {old['caption']}" if (ch.get("caption") or "").strip() else "",
               f"Now {old['duration']:.0f}s long" if ch.get("start_shift") or ch.get("end_shift") else ""]
    tg.send_video(str(out), f"✂️ Fixed: {old['hook']}")
    tg.send(f"✂️ Here's the fix for {job.get('campaign')}.\n\nReviewer said: {job['note']}\n"
            f"What I changed: {ch.get('why')}\n" + "\n".join(c for c in changed if c) +
            "\n\nSend it as the revision on Content Rewards?",
            [[("📤 Send revision", f"fxs:{job['key']}"), ("✋ I'll do it", f"fxm:{job['key']}")]])
    log.info("revision ready for %s", cid)
