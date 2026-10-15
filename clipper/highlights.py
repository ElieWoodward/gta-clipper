"""Find the best moments: loudness spikes narrow it down, Claude judges the transcript and writes the hook."""
import json
import re
import subprocess

import numpy as np

from .common import log, run, PLAYBOOK_FILE
from .transcribe import transcribe, text_between

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

Below are candidate windows. Each has an id, absolute start/end seconds, a loudness spike score, and the transcript.

Pick up to {max_picks} clips. Rules:
- Each clip must be between {min_s} and {max_s} seconds long, inside its window. Start a beat before the setup so the payoff lands; end right after the reaction.
- A clip has to work for someone who has never seen this streamer: a clear setup, an escalation, a payoff (chaos, a fail, a huge win, a funny line, a reaction).
- Skip: dead air, menus, reading chat, sponsor reads, gambling, anything sexual, slurs or hateful content, real personal info (addresses, phone numbers), anything involving minors, or content that would get the account flagged.
- Score each 1-10 for how likely it is to hold attention past 60 seconds. Be strict: a 7 is genuinely good.

For each pick, also write:
- hook: the one line of text that sits on screen for the WHOLE clip. Its only job is to make someone keep watching to the end.
  It sums up what the clip is about in one line, the gist people get hooked by. It is never a play-by-play of what happens.
  Write it like a real person captioning a clip they found, not like an AI or an ad. Rules:
  - Open a loop the clip closes: point at the payoff without giving it away ("wait for the cop at the end", "he had no idea who was in the back seat", "this is why you don't trust the getaway driver").
  - Specific to THIS moment (a person, object, or twist in it), never generic.
  - 4 to 9 words. Casual, lowercase in spirit, contractions fine. No emojis, no hashtags, no exclamation marks.
  - Banned: "insane", "epic", "crazy", "you won't believe", "wait for it" on its own, "POV", "when", "this is", "moment", "absolutely", "legendary", "chaos", and anything that sounds like a headline or a trailer.
  - Wrap the ONE word that carries the tension in *stars* (e.g. "he forgot the *cop* was still in the car"). It gets highlighted.
- commentary: 3 to 6 short context lines (max 12 words each) with an offset in seconds from clip start. These add real context a new viewer needs: who is doing what, what went wrong, why it's funny. Write like a sharp human editor, not a hype bot. No emojis.
- title: YouTube Shorts title, max 70 chars.
- caption: TikTok caption, 1-2 sentences, conversational.
- tags: 1-3 category words from: chaos, fail, win, funny_line, reaction, police, heist, driving, glitch, rp, other.

{playbook}

Return ONLY JSON: {{"picks": [{{"id": int, "start": float, "end": float, "score": int, "hook": str, "title": str, "caption": str, "tags": [str], "commentary": [{{"at": float, "text": str}}], "why": str}}]}}

CANDIDATES:
{candidates}
"""


def judge(cfg, vod, windows, client=None):
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
                                 max_picks=c["per_vod"], min_s=c["min_seconds"], max_s=c["max_seconds"],
                                 playbook=playbook, candidates=cand_txt)
    msg = client.messages.create(model=cfg["models"]["judge"], max_tokens=4000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return validate_picks(parse_json(text), windows, c)


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
    return sorted(good, key=lambda p: -p["score"])


def analyze(cfg, vod, wav, client=None):
    """Full pass for one VOD: loudness -> candidate windows -> transcripts -> Claude picks."""
    db = loudness_curve(wav)
    peaks = find_candidates(db, cfg["clip"]["candidates"])
    log.info("%s: %d loudness candidates", vod["id"], len(peaks))
    windows, words_all = [], []
    for i, t in enumerate(peaks):
        s, e = max(0, t - WINDOW_BEFORE), min(len(db), t + WINDOW_AFTER)
        words = transcribe(wav, cfg["models"]["whisper"], start=s, end=e)
        words_all.extend(words)
        windows.append({"id": i, "start": s, "end": e, "spike": float(db[t] - np.median(db)),
                        "text": text_between(words, s, e)})
    if not windows:
        return [], []
    return judge(cfg, vod, windows, client), words_all
