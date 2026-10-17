"""Find the best moments. Viewer-made Twitch clips, chat laugh spikes and loudness narrow a stream down to
candidate windows; Claude judges their transcripts, cuts the clip and writes the hook."""
import json
import re
import subprocess

import numpy as np

from .common import log, run, PLAYBOOK_FILE
from . import twitch
from .transcribe import transcribe, text_between, text_with_marks

WINDOW_BEFORE = 50   # seconds of context before a flagged second, sent for judging
WINDOW_AFTER = 30


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


HOOK_GUIDE = """HOW THE HOOK WORKS (the line of text in a white box at the top, on screen for the whole clip)
It's the reason a stranger stops scrolling. On viral GTA RP clips (100K-2M+ views) the hook is a PREMISE told the way
you'd tell a friend about it: slangy, present tense, and it makes you need to see what happens. It is never the
streamer's opinion, never a description of something boring on screen, and never gives away the punchline.

Formats that work (vary them):
  "When you ..."            WHEN YOU STAND UP TO YO GIRL IN GTARP 😭😭😭💀
  "POV: ..."                POV: he lets us get jumped so you flirt at the hospital 😭💀
  "Bro ..." / "He ..."      BRO GOT SENSITIVE AFTER CATCHING A BODY 😭 / He was reaching 💀
  "X tries to Y"            ICE tries to arrest gang members in GTA RP
  "Who let/made X ..."      Who made Speed a cop 😭💀
  "Chat ..."                Chat controls James in GTA 5 RP
  first person as streamer  I disarmed a cop in front of his whole crew 😭
  promise                   The longer you watch, the funnier it gets 🤣
Rules:
- 3 to 12 words. Casual slang is good ("bro", "nah", "lowkey", "yo", "chat"). ALL CAPS or normal case, both work.
- End with 1-3 emojis from 😭 💀 🤣 😂 💔 🙏 when it's funny (😭 and 💀 are the most used). No emojis on a serious/tense clip.
- True to what happens. Don't promise something the clip doesn't deliver.
- No in-game character names a stranger wouldn't know ("Breckers", "Mickey"): say "bro", "he", "my boss", "the cops".
  A famous streamer's real name is fine.
- Banned: stating what the streamer thinks or feels about something, narrating a boring action ("he grabs the horse"),
  spoilers of the ending, hashtags, "insane", "epic", "you won't believe", "wait for it", headline or trailer language.
The account owner's taste:
  Liked:    "he told chat to spam W way too early" (promises a fail without saying how)
  Liked:    "he still doesn't know what he did 💀" (confusion makes you watch to find out)
  Rejected: "he said spam W if he gets the horse" (just describes the setup)
  Rejected: "he didn't expect the town to just clear out" (narrates and gives the ending away)
  Rejected: any hook that just repeats the streamer's own opinion (boring, nobody cares)"""

JUDGE_PROMPT = """You pick moments from a livestream to post as short vertical clips on TikTok and YouTube Shorts.
Streamer: {streamer}. Stream title: {title}. Game: GTA (GTA 6 when it is out).

Below are candidate windows. Each has an id, absolute start/end seconds, why it was flagged (viewers clipped it, chat
burst out laughing, or it got loud), real chat messages from that moment when available, and the transcript with [Ns]
markers giving the absolute second each stretch of speech starts. Use the markers to set exact cut points.
"Viewers clipped this" and "chat laughed" are strong evidence that something good happened there; loudness alone is weak.

WHAT'S ACTUALLY FUNNY (what makes people laugh, rewatch and send it to a friend):
- someone losing their composure: the streamer breaking character laughing, screaming, or going dead silent
- a quick comeback, roast or deadpan one-liner, especially one that shuts someone down
- a plan, flex or threat that fails instantly or backfires on the person doing it
- a misunderstanding that keeps escalating, or someone committing way too hard to a bit
- a character doing something absurd with a straight face; other players' stunned reactions
- chaos that spirals: one small mistake turning into a disaster
NOT funny on its own: people just talking, inside jokes that need the backstory, menus, driving around, a mildly
amusing comment with no reaction. If chat didn't react and nobody on stream laughed, it probably isn't funny.

WHAT TO PICK: streamer reactions, fails, RP drama and storylines, chaotic action, funny lines. Pick up to {max_picks}
clips. That is a ceiling, not a target: only include moments that are genuinely funny or gripping to someone who has
never seen this streamer. Returning 2, or even 0, is better than padding with weak clips.

LENGTH: {min_s}-{max_s} seconds, inside its window. Find the shortest cut that still makes sense to a stranger: start
1-3 seconds before the action kicks off (only as much setup as needed to get it), end right after the reaction or
punchline lands. Most great clips are 15-35 seconds. Cut warm-up, menus, and the chatter after the moment is over.
End within about 2 seconds of the biggest laugh or reaction; a clip that keeps going after the payoff loses people.
People should watch to the very end and feel the length was right.{length_note}

SKIP: dead air, menus, reading chat, sponsor reads, gambling, anything sexual, slurs or hateful content, real personal
info, anything involving minors, or content that would get the account flagged.

SCORE each 1-10: how likely is a stranger to watch to the end and laugh or react? Be strict: 7 is genuinely good,
9-10 is something people would send to a friend.

{hook_guide}

For each pick write:
- hook_options: 5 different hooks using different formats from the list above.
- hook: the strongest of the 5.
- punch_text: OPTIONAL. If the clip has one quotable payoff line (something the streamer or another player actually
  says at the climax, or a 1-3 word reaction), give it in 1-6 words, max 30 characters, as said. It appears big at the
  bottom from punch_at until the end. Empty string if nothing is quotable. No emojis.
- punch_at: seconds from clip start when that line is said (0 if punch_text is empty).
- title: YouTube Shorts title, max 70 chars, same voice as the hook.
- caption: TikTok description: ONE casual reaction line like a viewer would comment, with 1-2 emojis,
  e.g. "bro started apologizing in front of the homies 😭💔". No hashtags (added automatically).
- tags: 1-3 from: reaction, fail, rp_drama, chaos, funny_line, police, heist, driving, glitch, win, other.
- why: one sentence on what happens and why it works.

{playbook}

Submit with the tool as JSON: {{"picks": [{{"id": int, "start": float, "end": float, "score": int, "hook_options": [str], "hook": str, "punch_text": str, "punch_at": float, "title": str, "caption": str, "tags": [str], "why": str}}]}}

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


def judge_prompt(cfg, vod, windows, max_picks=None):
    playbook = PLAYBOOK_FILE.read_text() if PLAYBOOK_FILE.exists() else ""
    if playbook:
        playbook = "What has worked and failed on this account so far (follow it):\n" + playbook
    cand_txt = "\n\n".join(
        f"[id {w['id']}] {w['start']:.0f}s-{w['end']:.0f}s\nFlagged: {'; '.join(w.get('signals') or ['loud moment'])}\n"
        + (f"Chat at that moment: {' | '.join(w['chat'])}\n" if w.get("chat") else "")
        + f"Transcript: {w['text'] or '(no speech)'}"
        for w in windows)
    c = cfg["clip"]
    from .common import INSIGHTS_FILE, load_json
    ins = load_json(INSIGHTS_FILE, {})
    length_note = ""
    if ins.get("target_seconds"):
        lo, hi = ins["target_seconds"]
        length_note = (f"\nOn this account, clips of {lo}-{hi} seconds have been doing best so far: aim for that "
                       f"unless the moment truly needs more or less.")
    return JUDGE_PROMPT.format(length_note=length_note, streamer=vod["streamer"]["name"], title=vod.get("title", ""),
                               max_picks=max_picks or c.get("min_per_vod", 3), min_s=c["min_seconds"], max_s=c["max_seconds"],
                               hook_guide=HOOK_GUIDE, playbook=playbook, candidates=cand_txt)


JUDGE_MAX_TOKENS = 8000


def judge_params(cfg, vod, windows, max_picks=None):
    """The judge request, ready to send now or to put in a batch."""
    return tool_params(cfg["models"]["judge"], judge_prompt(cfg, vod, windows, max_picks), list_schema("picks"),
                       JUDGE_MAX_TOKENS)


def judge_picks(cfg, data, windows, max_picks=None):
    return validate_picks(data, windows, cfg["clip"])[: max_picks or len(windows)]


def judge(cfg, vod, windows, client=None, max_picks=None):
    import anthropic
    client = client or anthropic.Anthropic()
    data = ask_json(client, cfg["models"]["judge"], judge_prompt(cfg, vod, windows, max_picks), "picks",
                    max_tokens=JUDGE_MAX_TOKENS)
    return judge_picks(cfg, data, windows, max_picks)


def list_schema(key):
    return {"type": "object", "required": [key], "properties": {key: {"type": "array", "items": {"type": "object"}}}}


def ask_json(client, model, prompt, key, max_tokens=4000):
    """Ask Claude for {key: [objects]}. A forced tool call guarantees well-formed JSON (hooks are full of quotes
    and emojis that break hand-written JSON); falls back to parsing text if the reply has no tool call."""
    return ask_tool(client, model, prompt, list_schema(key), max_tokens)


def tool_params(model, prompt, schema, max_tokens=4000):
    """The request for a tool-call answer (also what goes into a batch)."""
    tool = {"name": "submit", "description": "Submit the answer. Always answer by calling this tool.", "input_schema": schema}
    ask = "\n\nAnswer by calling the submit tool."
    if isinstance(prompt, list):   # images + text
        content = prompt[:-1] + [dict(prompt[-1], text=prompt[-1]["text"] + ask)]
    else:
        content = prompt + ask
    return {"model": model, "max_tokens": max_tokens, "tools": [tool], "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": content}]}


def _get(x, k):
    return x.get(k) if isinstance(x, dict) else getattr(x, k, None)


def read_tool_reply(msg):
    """The JSON object from a tool-call reply (or from JSON text). Raises ValueError if neither is there."""
    blocks = _get(msg, "content") or []
    for b in blocks:
        inp = _get(b, "input")
        if _get(b, "type") == "tool_use" and isinstance(inp, dict) and inp:
            return inp
    return parse_json("".join(_get(b, "text") or "" for b in blocks if _get(b, "type") == "text"))


def ask_tool(client, model, prompt, schema, max_tokens=4000, attempts=3):
    """Get a JSON object from Claude through a tool call (valid JSON even when hooks are full of quotes and emojis).
    Falls back to parsing a JSON reply, and asks again if neither works."""
    params = tool_params(model, prompt, schema, max_tokens)
    last = None
    for _ in range(attempts):
        msg = client.messages.create(**params)
        record_usage(msg)
        try:
            return read_tool_reply(msg)
        except ValueError as e:   # JSONDecodeError is a ValueError
            last = e
            log.warning("unreadable reply from Claude (%s); asking again", e)
    raise RuntimeError(f"Claude never returned readable JSON: {last}")


PRICE_PER_MTOK = {"input": 2.0, "output": 10.0}   # Claude Sonnet 5.5 list price (USD); config limits can override


class SpendCapReached(RuntimeError):
    """Today's Claude budget is used up; skip Claude work until tomorrow."""


def record_usage(msg, discount=1.0):
    from .common import add_spend
    u = _get(msg, "usage")
    if u is None:
        return
    inp = (_get(u, "input_tokens") or 0) + (_get(u, "cache_creation_input_tokens") or 0)
    out = _get(u, "output_tokens") or 0
    if not isinstance(inp, (int, float)) or not isinstance(out, (int, float)):
        return
    add_spend(discount * (inp / 1e6 * PRICE_PER_MTOK["input"] + out / 1e6 * PRICE_PER_MTOK["output"]))


def budget_left(cfg, state):
    """USD of Claude budget left today (inf if no cap is set)."""
    from .common import spend_today
    lim = (cfg.get("limits") or {})
    prices = lim.get("price_per_mtok")
    if prices:
        PRICE_PER_MTOK.update(prices)
    cap = lim.get("claude_usd_per_day")
    return float("inf") if cap is None else cap - spend_today(state)


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
            s = e - c["max_seconds"]   # the payoff is at the end: trim setup, never the ending
        if e - s < c["min_seconds"]:
            log.info("dropping pick %s: window too short", p.get("id"))
            continue
        # The punchline's timestamp may have been given in absolute seconds; make it clip-relative and in range.
        s0, pa = float(p["start"]), float(p.get("punch_at") or 0)
        if pa > e - s0 + 1 and s0 <= pa <= e + 5:
            pa -= s0           # was absolute stream seconds
        pa += s0 - s           # rebase if the start moved above
        p["punch_text"] = (p.get("punch_text") or "").strip()
        p["punch_at"] = round(min(max(0.0, pa), max(0.0, e - s - 1.5)), 2)
        p["start"], p["end"] = round(s, 2), round(e, 2)
        p["hook"] = (p.get("hook") or "").replace("*", "").strip()
        p["commentary"] = [x for x in p.get("commentary", []) if 0 <= float(x["at"]) < e - s]
        good.append(p)
    # Best first, and drop any clip that mostly overlaps a better one.
    kept = []
    for p in sorted(good, key=lambda p: -p["score"]):
        if all(min(p["end"], k["end"]) - max(p["start"], k["start"]) < 0.4 * (p["end"] - p["start"]) for k in kept):
            kept.append(p)
    return kept


EDITOR_PROMPT = """You are a ruthless short-form video editor for a GTA RP clip account. A stranger is scrolling TikTok.
For each clip below you get what happens in it and candidate hooks.

{hook_guide}

Score every candidate 1-10 on one question: would a stranger who has never heard of this streamer stop and watch to
the end? Be harsh. Anything that states an opinion, narrates the screen, gives away the ending, or sounds like an ad,
a headline or an AI scores 4 or lower.

If the best candidate scores under 8, write a better one yourself following the guide above.

Submit with the tool as JSON: {{"clips": [{{"i": int, "best": str, "score": int}}]}} where best is the final hook to use.

CLIPS:
{clips}
"""

def edit_hooks(cfg, picks, windows, client=None):
    """Second pass: a strict editor scores every hook option and rewrites weak ones."""
    if not picks:
        return picks
    import anthropic
    client = client or anthropic.Anthropic()
    by_id = {w["id"]: w for w in windows}
    blocks = []
    for i, p in enumerate(picks):
        opts = list(dict.fromkeys((p.get("hook_options") or []) + [p.get("hook", "")]))
        said = by_id.get(p.get("id"), {}).get("text", "")[:1200]
        blocks.append(f"[{i}] What happens: {p.get('why', '')}\nWhat they say: {said}\nCandidates:\n"
                      + "\n".join(f"- {o}" for o in opts if o))
    try:
        out = ask_json(client, cfg["models"]["judge"],
                       EDITOR_PROMPT.format(hook_guide=HOOK_GUIDE, clips="\n\n".join(blocks)), "clips", max_tokens=3000)
        for c in out.get("clips", []):
            i = int(c["i"])
            if 0 <= i < len(picks) and c.get("best"):
                if c["best"] != picks[i]["hook"]:
                    picks[i].setdefault("hook_options", []).append(picks[i]["hook"])
                picks[i]["hook"] = c["best"].replace("*", "").strip()
                picks[i]["hook_score"] = int(c.get("score", 0))
    except Exception as e:
        log.warning("hook editor pass failed (%s); keeping original hooks", e)
    return picks


def merge_candidates(db, sig, n, min_gap=45):
    """Combine the three signals into at most n flagged seconds, best evidence first.
    Returns [{t, signals, strength}] where t is roughly when the moment peaks."""
    length = len(db)
    out = []

    def add(t, label, strength):
        t = int(min(max(0, t), length - 1))
        for c in out:
            if abs(c["t"] - t) < min_gap:
                c["signals"].append(label)
                c["strength"] += strength * 0.5
                return
        if len(out) < n:
            out.append({"t": t, "signals": [label], "strength": strength})

    # 1. Moments viewers clipped themselves: the best evidence there is. A Twitch clip usually ends at the payoff.
    clips = sig.get("clips") or []
    for c in clips[: max(1, n // 2)]:
        title = f' "{c["title"]}"' if c["title"] and c["title"].lower() != "clip" else ""
        add(c["offset"] + c["duration"] * 0.7, f"viewers clipped this{title} ({c['views']:,} views)", 10 + min(10, c["views"] / 50))
    # 2. Chat laughing or exploding.
    if sig.get("laughs") is not None:
        for t, st in twitch.chat_peaks(sig["laughs"], sig["total"], n):
            add(t, f"chat laughed/reacted ({st:.0f}x normal)", 4 + st)
    # 3. Loudness fills whatever is left.
    for t in find_candidates(db, n, min_gap=min_gap):
        add(t, "loud moment (yelling or action)", 1)
    return sorted(out, key=lambda c: c["t"])


def keepers(cfg, picks, keep_all=False):
    """Picks worth polishing: below the score bar they're thrown away, so don't pay to rewrite their hooks."""
    return picks if keep_all else [p for p in picks if int(p.get("score") or 0) >= cfg["clip"]["min_score"]]


def analyze(cfg, vod, wav, client=None, max_picks=None, keep_all=False):
    """Full pass for one VOD: signals -> candidate windows -> transcripts -> Claude picks."""
    windows, words_all, max_picks = prepare(cfg, vod, wav, max_picks)
    if not windows:
        return [], []
    picks = keepers(cfg, judge(cfg, vod, windows, client, max_picks=max_picks), keep_all)
    return (edit_hooks(cfg, picks, windows, client) if picks else []), words_all


def prepare(cfg, vod, wav, max_picks=None):
    """Signals -> candidate windows with transcripts (no Claude). Returns (windows, words, max_picks)."""
    db = loudness_curve(wav)
    budget_picks, n_cands = clip_budget(cfg, len(db))
    if max_picks:   # the queue only has room for a few: judge fewer moments too (fewer tokens)
        n_cands = min(n_cands, max(12, max_picks * 6))
        max_picks = min(max_picks, budget_picks)
    else:
        max_picks = budget_picks
    sig = twitch.signals(vod, len(db)) if cfg.get("signals", {}).get("twitch", True) else {}
    ranges = vod.get("gta_ranges")
    if ranges:   # only the GTA part of the stream (big streamers often open with an hour of Just Chatting)
        def in_gta(t):
            return any(s + 20 <= t <= (e if e is not None else len(db)) - 5 for s, e in ranges)
        cands = [c for c in merge_candidates(db, sig, n_cands * 3) if in_gta(c["t"])]
        cands = sorted(sorted(cands, key=lambda c: -c["strength"])[:n_cands], key=lambda c: c["t"])
    else:
        cands = merge_candidates(db, sig, n_cands)
    kinds = {k: sum(1 for c in cands if c["signals"][0].startswith(k)) for k in ("viewers", "chat", "loud")}
    log.info("%s: %.1f h stream, %d candidates %s, up to %d clips", vod["id"], len(db) / 3600, len(cands), kinds, max_picks)
    print(f"::notice::{len(db) / 3600:.1f} h stream, judging {len(cands)} moments "
          f"({kinds['viewers']} viewer clips, {kinds['chat']} chat spikes, {kinds['loud']} loud), up to {max_picks} clips",
          flush=True)
    windows, words_all = [], []
    for i, c in enumerate(cands):
        s, e = max(0, c["t"] - WINDOW_BEFORE), min(len(db), c["t"] + WINDOW_AFTER)
        words = transcribe(wav, cfg["models"]["whisper"], start=s, end=e)
        words_all.extend(words)
        windows.append({"id": i, "start": s, "end": e, "signals": c["signals"],
                        "chat": twitch.chat_sample(sig.get("msgs") or [], s, e),
                        "spike": float(db[c["t"]] - np.median(db)), "text": text_with_marks(words, s, e)})
    return windows, words_all, max_picks
