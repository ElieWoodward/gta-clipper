"""Edit pass on a finished clip: the "properly edited" look campaign reviewers ask for ("add some cuts, SFX, b rolls,
overlays, better bg music"). Runs after render.render on the 1080x1920 file, so burned-in captions move with every
cut. No Claude calls.

- dead air: silent stretches over edit.trim_silence_s are cut out (kept tight, never the punchline)
- punch-in zooms on the loudest moments (edit.zooms of them, spaced out), each with a whoosh
- a boom hit on the punchline (pick["punch_at"])
- a quiet music bed from edit.music_dir that dips under the streamer's voice (skipped if the folder is empty)

Sound effects are made here with ffmpeg's own generators, so they carry no copyright. Music is whatever Elie puts
in the folder (YouTube Audio Library or other royalty-free tracks)."""
import json
import random
import re
import shutil
import subprocess
from pathlib import Path

from .common import log, ROOT

W, H = 1080, 1920
DEFAULTS = {"enabled": True, "trim_silence_s": 0.7, "silence_db": -32, "pad_s": 0.12, "zooms": 3, "zoom": 1.08,
            "zoom_s": 0.9, "min_gap_s": 3.0, "sfx": True, "sfx_db": -10, "music_dir": "music", "music_db": -24}
SFX_DIR = ROOT / "data" / "sfx"


def settings(cfg):
    return dict(DEFAULTS, **((cfg or {}).get("edit") or {}))


def _run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def duration(path):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]).stdout
    return float(json.loads(out)["format"]["duration"])


# ------------------------------------------------------------------ sound effects (generated, no copyright)
def sfx(name):
    """whoosh: a swept noise rush; boom: a deep sine hit with a fast decay. Made once, kept in data/sfx."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    out = SFX_DIR / f"{name}.wav"
    if out.exists():
        return out
    if name == "whoosh":
        src = ("anoisesrc=d=0.45:c=pink:a=0.9,highpass=f=400,lowpass=f=5000,"
               "afade=t=in:d=0.25:curve=exp,afade=t=out:st=0.25:d=0.2,volume=1.6")
    else:   # boom
        src = ("aevalsrc='0.95*sin(2*PI*(55+70*exp(-t*18))*t)*exp(-t*4.5)':d=1.1:s=44100,"
               "lowpass=f=900,volume=1.4")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-ac", "2", "-ar", "44100", str(out)])
    return out


# ------------------------------------------------------------------ analysis
def loudness(path, step=0.1):
    """[(time, rms_db)] every `step` seconds."""
    n = int(8000 * step)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-af",
                        f"aresample=8000,asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
                        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"],
                       capture_output=True, text=True)
    out, t = [], None
    for ln in r.stdout.splitlines():
        m = re.search(r"pts_time:([\d.]+)", ln)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"RMS_level=(-?[\d.]+|-inf)", ln)
        if m and t is not None:
            out.append((t, -90.0 if m.group(1) == "-inf" else float(m.group(1))))
    return out


def keep_ranges(levels, dur, s, protect=()):
    """Spans to keep: everything except quiet runs longer than trim_silence_s (minus a little padding each side).
    Nothing inside `protect` (punchline) and nothing in the first second (the hook) is cut."""
    quiet, start = [], None
    step = levels[1][0] - levels[0][0] if len(levels) > 1 else 0.1
    for t, db in levels + [(dur, 0.0)]:
        if db < s["silence_db"]:
            start = t if start is None else start
        elif start is not None:
            a, b = start + s["pad_s"], t - s["pad_s"]
            a = max(a, 1.0)
            for p0, p1 in protect:
                if a < p1 and b > p0:
                    b = min(b, p0) if a < p0 else a
            if b - a >= s["trim_silence_s"] - 2 * s["pad_s"] and b - a > step:
                quiet.append((a, b))
            start = None
    keep, at = [], 0.0
    for a, b in quiet:
        if a > at:
            keep.append((at, a))
        at = b
    if at < dur:
        keep.append((at, dur))
    return keep or [(0.0, dur)]


def peaks(levels, s, dur):
    """The loudest moments (reactions, crashes), at least min_gap_s apart, not in the first second or the end."""
    cands = sorted((x for x in levels if 1.0 <= x[0] <= dur - s["zoom_s"] - 0.3), key=lambda x: -x[1])
    out = []
    for t, db in cands:
        if len(out) >= s["zooms"]:
            break
        if db > -60 and all(abs(t - o) >= s["min_gap_s"] for o in out):
            out.append(t)
    return sorted(out)


def remap(t, keep):
    """A time in the original clip -> its time after the cuts (None if it was cut)."""
    at = 0.0
    for a, b in keep:
        if a <= t <= b:
            return at + (t - a)
        at += b - a
    return None


def music_track(s):
    d = Path(s["music_dir"])
    d = d if d.is_absolute() else ROOT / d
    tracks = [p for p in d.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac", ".ogg")] \
        if d.is_dir() else []
    return random.choice(tracks) if tracks else None


# ------------------------------------------------------------------ the pass
def polish(path, cfg=None, pick=None):
    """Edit the rendered clip in place. Returns a summary dict (what was done), or None if it was skipped/failed;
    a failure leaves the original clip untouched."""
    s = settings(cfg)
    if not s["enabled"]:
        return None
    path = Path(path)
    try:
        dur = duration(path)
        levels = loudness(path)
        if not levels:
            return None
        punch = float((pick or {}).get("punch_at") or 0) or None
        protect = [(punch - 0.4, punch + 1.5)] if punch else []
        keep = keep_ranges(levels, dur, s, protect)
        new_dur = sum(b - a for a, b in keep)
        # loudness on the cut timeline, for zoom points
        kept_levels = [(remap(t, keep), db) for t, db in levels]
        kept_levels = [(t, db) for t, db in kept_levels if t is not None]
        zooms = peaks(kept_levels, s, new_dur)
        boom = remap(punch, keep) if punch else None

        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path)]
        fc = []
        # 1. cuts
        n = len(keep)
        for i, (a, b) in enumerate(keep):
            fc.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                      f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        fc.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
        # 2. punch-in zooms (scale up a little, crop back to 1080x1920, ease in over 0.12 s)
        if zooms:
            z = s["zoom"] - 1
            parts = "+".join(f"{z:.3f}*clip((t-{t:.2f})/0.12,0,1)*clip(({t + s['zoom_s']:.2f}-t)/0.12,0,1)"
                             for t in zooms)
            fc.append(f"[vc]scale=w='2*trunc({W}*(1+{parts})/2)':h=-2:eval=frame,crop={W}:{H},setsar=1[vz]")
        else:
            fc.append("[vc]null[vz]")
        # 3. audio: voice + sound effects (+ music bed ducked under the voice)
        mix, inputs = ["[ac]"], 1
        hits = ([("whoosh", t - 0.15) for t in zooms] + ([("boom", boom)] if boom is not None else [])) \
            if s["sfx"] else []
        for k, (name, t) in enumerate(hits):
            cmd += ["-i", str(sfx(name))]
            ms = max(0, int(t * 1000))
            fc.append(f"[{inputs}:a]volume={s['sfx_db']}dB,adelay={ms}|{ms}[s{k}]")
            mix.append(f"[s{k}]")
            inputs += 1
        track = music_track(s)
        if track:
            cmd += ["-stream_loop", "-1", "-i", str(track)]
            fc.append(f"[ac]asplit=2[ac1][key]")
            mix[0] = "[ac1]"
            fc.append(f"[{inputs}:a]atrim=0:{new_dur:.3f},volume={s['music_db']}dB,aresample=44100,"
                      f"afade=t=out:st={max(0, new_dur - 1.5):.2f}:d=1.5[mus];"
                      "[mus][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[duck]")
            mix.append("[duck]")
            inputs += 1
        if len(mix) > 1:
            fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
        else:
            fc.append("[ac]anull[aout]")
        tmp = path.with_suffix(".edit.mp4")
        cmd += ["-filter_complex", ";".join(fc), "-map", "[vz]", "-map", "[aout]", "-t", f"{new_dur:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", str(tmp)]
        _run(cmd)
        shutil.move(str(tmp), str(path))
        done = {"cut_s": round(dur - new_dur, 1), "cuts": len(keep) - 1, "zooms": len(zooms),
                "sfx": len(hits), "music": track.name if track else None}
        log.info("edit pass: %s", done)
        return done
    except Exception as e:
        log.warning("edit pass skipped (%s); the clip stays as rendered", str(e)[:300])
        for p in (path.with_suffix(".edit.mp4"),):
            p.unlink(missing_ok=True)
        return None
