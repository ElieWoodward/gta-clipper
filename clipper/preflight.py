"""Start-of-run check: one tiny real call to every service and a 3-second test render.

Runs before the long audio download, so a broken key, an API change, a missing font or a bad prompt format fails
the run in about a minute instead of after 10+ minutes of work. Each check prints a line on the run page.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import highlights, render, twitch, vision
from .common import log, run


class PreflightFailed(RuntimeError):
    pass


def _check(name, fn, results):
    t0 = time.time()
    try:
        detail = fn() or ""
        results.append((name, True, f"{detail} ({time.time() - t0:.0f}s)"))
        print(f"::notice::preflight ok: {name}: {detail}", flush=True)
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {str(e)[:300]}"))
        print(f"::error::preflight FAILED: {name}: {type(e).__name__}: {str(e)[:400]}", flush=True)


def _claude_picks(cfg, client):
    # Same call shape as the real judge, with the characters that broke parsing before: quotes and emojis.
    prompt = ('Test of the answer format. Submit picks with exactly one pick: id 1, start 10, end 30, score 8, '
              'hook_options ["he said \\"spam W\\" way too early 😭", "POV: chat controls him 💀"], '
              'hook "POV: he said \\"spam W\\" way too early 😭💀", punch_text "NAH", punch_at 12, title "t", '
              'caption "bro 😭", tags ["fail"], why "test".')
    out = highlights.ask_json(client, cfg["models"]["judge"], prompt, "picks", max_tokens=600)
    picks = out.get("picks") or []
    if not picks or not isinstance(picks[0].get("hook"), str) or not picks[0]["hook"].strip():
        raise ValueError(f"unexpected answer: {json.dumps(out)[:200]}")
    return f'judge format ok, hook came back as {picks[0]["hook"]}'


def _claude_learn(cfg, client):
    from .jobs import LEARN_SCHEMA
    out = highlights.ask_tool(client, cfg["models"]["learn"],
                              'Test of the answer format. Submit playbook "- test \\"rule\\"", summary "ok", '
                              'best_slots ["12:00"], target_seconds [15, 30].', LEARN_SCHEMA, max_tokens=400)
    if not isinstance(out.get("playbook"), str):
        raise ValueError(f"unexpected answer: {json.dumps(out)[:200]}")
    return "weekly-learn format ok"


def _sample_video(d, seconds=3):
    """16:9 test video with a 'webcam' box in the top-right corner and a tone for audio."""
    src = d / "pre_src.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         "-vf", "drawbox=x=1500:y=20:w=400:h=300:color=black@1:t=fill,drawbox=x=1620:y=80:w=160:h=180:color=0xE0B090@1:t=fill",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", src])
    return src


def _vision(cfg, client, d):
    src = _sample_video(d)
    look = vision.analyze_frames(cfg, src, 0, 3, d, client=client, strict=True)
    return f"frame check answered: layout {look['layout']}, webcam {'found' if look['facecam'] else 'none'}"


def _groq(cfg, d):
    wav = d / "pre.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-ar", "16000", wav])
    for m in {cfg["models"]["whisper"], cfg["models"].get("captions", cfg["models"]["whisper"])}:
        highlights.transcribe(wav, m, start=0.0, end=3.0)
    return "transcription models answered"


def _render(d):
    if render._emoji_font() is None:
        raise RuntimeError("color emoji font missing (fonts-noto-color-emoji not installed)")
    src = _sample_video(d)
    pick = {"start": 100.0, "end": 103.0, "hook": 'POV: he said "spam W" too early 😭💀', "punch_text": "NAH HE DIDNT",
            "punch_at": 1.0}
    words = [{"w": w, "s": 100.2 + i * 0.4, "e": 100.5 + i * 0.4} for i, w in enumerate("no way he actually did it".split())]
    made = []
    for name, kw in [("fill", {"layout": "fill", "focus_x": 0.3}), ("wide", {"layout": "wide"}),
                     ("cam", {"layout": "wide", "facecam": {"x": 0.78, "y": 0.02, "w": 0.21, "h": 0.28}})]:
        out = d / f"pre_{name}.mp4"
        render.render(src, 0.0, pick, words, out, **kw)
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_type:format=duration",
                                          "-of", "json", str(out)], capture_output=True, text=True).stdout)
        v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
        dur = float(info["format"]["duration"])
        if (v["width"], v["height"]) != (1080, 1920) or not 2.5 <= dur <= 3.6:
            raise RuntimeError(f"{name} render came out {v['width']}x{v['height']}, {dur:.1f}s")
        made.append(name)
    return "rendered " + ", ".join(made) + " at 1080x1920"


def _twitch(vod):
    vid = twitch.video_id(vod)
    if not vid:
        return "not a Twitch VOD, skipped"
    info = twitch.video_info(vid)
    if not info:
        raise RuntimeError("Twitch API returned nothing for the video")
    msgs = twitch._chat_part(vid, 600, 660, max_pages=1)
    return f"video {vid} ({(info.get('game') or {}).get('name')}), chat replay {'ok' if msgs else 'empty'}"


def _blotato(cfg):
    from . import services
    names = {}
    for platform in cfg["posting"]["platforms"]:
        acct = services.blotato_account_id(platform)   # raises with a clear message if not connected
        names[platform] = acct
    return "connected: " + ", ".join(f"{p} ({a})" for p, a in names.items())


def _release():
    from . import services
    rel = services.queue_release()
    return f"release ready ({len(rel.get('assets') or [])} stored clips)"


def run_all(cfg, vod=None, client=None, posting=False):
    import anthropic
    client = client or anthropic.Anthropic()
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _check("Claude (clip picks)", lambda: _claude_picks(cfg, client), results)
        _check("Claude (weekly learn)", lambda: _claude_learn(cfg, client), results)
        _check("Claude (frame check)", lambda: _vision(cfg, client, d), results)
        _check("Groq", lambda: _groq(cfg, d), results)
        _check("render", lambda: _render(d), results)
        if vod:
            _check("Twitch", lambda: _twitch(vod), results)
        from . import clipqueue, mail
        if clipqueue.email_mode(cfg):
            if posting or mail.configured():
                _check("Gmail (sending + reading replies)", lambda: (mail.check_login(), "logged in")[1], results)
            if posting or os.environ.get("GITHUB_TOKEN"):
                _check("clip storage (GitHub release)", _release, results)
        elif posting or os.environ.get("BLOTATO_API_KEY"):
            _check("Blotato (posting)", lambda: _blotato(cfg), results)
    bad = [r for r in results if not r[1]]
    if bad:
        raise PreflightFailed("; ".join(f"{n}: {m}" for n, _, m in bad))
    log.info("preflight passed: %s", results)
    return results
