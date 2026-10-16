"""Find the best moments: loudness spikes narrow it down, Claude judges the transcript and writes the hook."""
import json
import re
import subprocess

import numpy as np

from .common import log, run, PLAYBOOK_FILE
from .transcribe import transcribe, text_between, text_with_marks

WINDOW_BEFORE = 75   # seconds of context before a spike sent for judging
WINDOW_AFTER = 45


def loudness_curve(wav):
    """RMS loudness per second (dB)."""
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                       capture_output=True, check=True)
    a = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
    n = len(a) // 8000
    a = a[: n * 8000].reshape(n, 8000)
    rms = np.sqrt((a ** 2).mean(axis=1)) + 1e-6
    return 20 * np.log10(rms)


def find_candidates(db, n, min_gap=90):
    """Seconds where loudness jumps well above the local baseline (yelling, explosions, laughter)."""
    if len(db) < 30:
        return []
    k = 120
    pad = np.pad(db, (k, k), mode="edge")
    baseline = np.array([np.median(pad[i:i + 2 * k]) for i in range(len(db))])
    # 5-second smoothed excess over baseline
    excess = np.convolve(db - baseline, np.ones(5) / 5, mode="same")
    order = np.argsort(excess)[::-1]
    picks = []
    for t in order:
        if excess[t] < 3:
            break
        if all(abs(t - p) >= min_gap for p in picks):
            picks.append(int(t))
        if len(picks) >= n:
            break
    return sorted(picks)


JUDGE_PROMPT = """You pick moments from a livestream to post as vertical clips on TikTok and YouTube Shorts.
Streamer: {streamer}. Stream title: {title}. Game: GTA (GTA 6 when it is out).

Below are candidate windows. Each has an id, absolute start/end seconds, a loudness spike score, and the transcript with [Ns] markers giving the absolute second each stretch of speech starts. Use the markers to set exact start and end times.

Pick up to {max_picks} clips. That is a ceiling, not a target: only include moments that are genuinely good. Returning 2, or even 0, is better than padding with weak clips. Rules:
- Each clip must be between {min_s} and {max_s} seconds long, inside its window. Start a beat before the setup so the payoff lands; end right after the reaction.
- A clip has to work for someone who has never seen this streamer: a clear setup, an escalation, a payoff (chaos, a fail, a huge win, a funny line, a reaction).
- Skip: dead air, menus, reading chat, sponsor reads, gambling, anything sexual, slurs or hateful content, real personal info (addresses, phone numbers), anything involving minors, or content that would get the account flagged.
- Score each 1-10 for how likely it is to hold attention past 60 seconds. Be strict: a 7 is genuinely good.

For each pick, also write:
- hook_options: 5 different hooks for the line of text that sits on screen for the WHOLE clip. Its only job is to make a stranger scrolling past stop and watch to the end.
  A good hook gives a reason to CARE, never a description of what's on screen. "he grabs the horse" fails: nobody cares yet. Ask: what's at stake, what goes wrong, who's about to lose it, what's the twist?
  Use a different angle for each of the 5:
    1. stakes: what he's about to lose or win ("one wrong turn and the whole heist is gone")
    2. contradiction or irony ("he planned this for 3 hours and it lasted 9 seconds")
    3. reaction: the emotion it causes, without spoiling why ("I've never heard chat go this quiet")
    4. curiosity gap with a promise about the ending ("the last 10 seconds ruined his whole night")
    5. relatable or a challenge to the viewer ("every GTA player has done this exactly once")
  Rules for every hook:
  - 4 to 10 words, sounds like a real person, casual, contractions fine. No emojis, hashtags or exclamation marks.
  - Must be true to what actually happens in the clip. Never promise something that isn't there.
  - Never just narrate the action on screen. Never generic enough to fit any clip.
  - Banned: "insane", "epic", "crazy", "you won't believe", "wait for it" on its own, "POV", "this is", "moment", "absolutely", "legendary", "chaos", headline or trailer language.
  - Wrap the ONE word that carries the tension in *stars*; it gets highlighted.
- hook: copy the single strongest of your 5 options, judged by: would a stranger who has never heard of this streamer stop scrolling for it?
  The account owner's taste, from hooks he approved: irony and confused reactions win.
    Approved: "he told chat to spam W way too *early*" (irony: promises a fail without saying how)
    Approved: "he still doesn't know what he *did*" (reaction: confusion makes you watch to find out)
    Rejected: "he said spam W if he gets the *horse*" (just describes the setup, no reason to care)
    Rejected: "he didn't expect the *town* to just clear out" (narrates the event and gives away the ending)
- commentary: 3 to 6 short context lines (max 12 words each) with an offset in seconds from clip start. These add real context a new viewer needs: who is doing what, what went wrong, why it's funny. Write like a sharp human editor, not a hype bot. No emojis.
- title: YouTube Shorts title, max 70 chars.
- caption: TikTok caption, 1-2 sentences, conversational.
- tags: 1-3 category words from: chaos, fail, win, funny_line, reaction, police, heist, driving, glitch, rp, other.

{playbook}

Return ONLY JSON: {{"picks": [{{"id": int, "start": float, "end": float, "score": int, "hook_options": [str], "hook": str, "title": str, "caption": str, "tags": [str], "commentary": [{{"at": float, "text": str}}], "why": str}}]}}

CANDIDATES:
{candidates}
"""


def clip_budget(cfg, seconds):
    """How many candidates to judge and clips to keep, scaled by stream length."""
    c, hours = cfg["clip"], seconds / 3600
    picks = int(min(c.get("max_per_vod", 16), max(c.get("min_per_vod", 3), round(c.get("clips_per_hour", 2) * hours))))
    cands = int(min(c.get("max_candidates", 70), max(picks * 3, round(c.get("candidates_per_hour", 6) * hours))))
    window = WINDOW_BEFORE + WINDOW_AFTER
    cands = min(cands, max(6, int(c.get("transcribe_minutes", 50) * 60 // window)))
    return picks, cands


def judge(cfg, vod, windows, client=None, max_picks=None):
    import anthropic
    client = client or anthropic.Anthropic()
    playbook = PLAYBOOK_FILE.read_text() if PLAYBOOK_FILE.exists() else ""
    if playbook:
        playbook = "What has worked and failed on this account so far (follow it):\n" + playbook
    cand_txt = "\n\n".join(
        f"[id {w['id']}] {w['start']:.0f}s-{w['end']:.0f}s spike={w['spike']:.1f}\n{w['text'] or '(no speech)'}"
        for w in windows)
    c = cfg["clip"]
    prompt = JUDGE_PROMPT.format(streamer=vod["streamer"]["name"], title=vod.get("title", ""),
                                 max_picks=max_picks or c.get("min_per_vod", 3), min_s=c["min_seconds"], max_s=c["max_seconds"],
                                 playbook=playbook, candidates=cand_txt)
    msg = client.messages.create(model=cfg["models"]["judge"], max_tokens=4000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return validate_picks(parse_json(text), windows, c)[: max_picks or len(windows)]


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("judge returned no JSON")
    return json.loads(m.group(0))


def validate_picks(data, windows, c):
    by_id = {w["id"]: w for w in windows}
    good = []
    for p in data.get("picks", []):
        w = by_id.get(p.get("id"))
        if not w:
            continue
        s = max(float(p["start"]), w["start"])
        e = min(float(p["end"]), w["end"])
        # Force length into range. The payoff is usually at the end, so add setup time before it first.
        if e - s < c["min_seconds"]:
            s = max(w["start"], e - c["min_seconds"] - 1)
            e = min(w["end"], s + c["min_seconds"] + 1)
        if e - s > c["max_seconds"]:
            e = s + c["max_seconds"]
        if e - s < c["min_seconds"]:
            log.info("dropping pick %s: window too short", p.get("id"))
            continue
        p["start"], p["end"] = round(s, 2), round(e, 2)
        p["commentary"] = [x for x in p.get("commentary", []) if 0 <= float(x["at"]) < e - s]
        good.append(p)
    # Best first, and drop any clip that mostly overlaps a better one.
    kept = []
    for p in sorted(good, key=lambda p: -p["score"]):
        if all(min(p["end"], k["end"]) - max(p["start"], k["start"]) < 0.4 * (p["end"] - p["start"]) for k in kept):
            kept.append(p)
    return kept


def analyze(cfg, vod, wav, client=None):
    """Full pass for one VOD: loudness -> candidate windows -> transcripts -> Claude picks."""
    db = loudness_curve(wav)
    max_picks, n_cands = clip_budget(cfg, len(db))
    peaks = find_candidates(db, n_cands)
    log.info("%s: %.1f h stream, %d loudness candidates, up to %d clips", vod["id"], len(db) / 3600, len(peaks), max_picks)
    print(f"::notice::{len(db) / 3600:.1f} h stream, judging {len(peaks)} moments, up to {max_picks} clips", flush=True)
    windows, words_all = [], []
    for i, t in enumerate(peaks):
        s, e = max(0, t - WINDOW_BEFORE), min(len(db), t + WINDOW_AFTER)
        words = transcribe(wav, cfg["models"]["whisper"], start=s, end=e)
        words_all.extend(words)
        windows.append({"id": i, "start": s, "end": e, "spike": float(db[t] - np.median(db)),
                        "text": text_with_marks(words, s, e)})
    if not windows:
        return [], []
    return judge(cfg, vod, windows, client, max_picks=max_picks), words_all
