"""Speech-to-text with word timestamps via Groq's Whisper API (OpenAI-compatible)."""
import os
import time

import requests

from .common import log, run, require_env

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
CHUNK = 600  # seconds per request, keeps each upload well under the size limit


def _duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def transcribe(wav, model, start=0.0, end=None):
    """Return list of words [{w, s, e}] with absolute timestamps."""
    key = require_env("GROQ_API_KEY")
    end = end or _duration(wav)
    words = []
    t = start
    while t < end:
        seg_len = min(CHUNK, end - t)
        chunk = wav.parent / f"chunk_{int(t)}.mp3"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", t, "-t", seg_len, "-i", wav, "-b:a", "48k", chunk])
        for attempt in range(4):
            with open(chunk, "rb") as fh:
                r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                                  files={"file": (chunk.name, fh, "audio/mpeg")},
                                  data={"model": model, "response_format": "verbose_json",
                                        "timestamp_granularities[]": "word", "language": "en"},
                                  timeout=300)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 20 * (attempt + 1))
                log.warning("Groq rate limit, waiting %.0fs", wait)
                time.sleep(min(wait, 120))
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Groq kept rate-limiting; try again later or lower clip.transcribe_minutes")
        for w in r.json().get("words", []):
            words.append({"w": w["word"].strip(), "s": round(w["start"] + t, 2), "e": round(w["end"] + t, 2)})
        os.remove(chunk)
        t += seg_len
    log.info("transcribed %d words", len(words))
    return words


def text_between(words, s, e):
    return " ".join(w["w"] for w in words if s <= w["s"] < e)


def text_with_marks(words, s, e, every=5):
    """Transcript with an absolute-seconds marker every few seconds, so cut points can be exact."""
    out, next_mark = [], s
    for w in words:
        if not (s <= w["s"] < e):
            continue
        if w["s"] >= next_mark:
            out.append(f"[{int(w['s'])}s]")
            next_mark = w["s"] + every
        out.append(w["w"])
    return " ".join(out)
