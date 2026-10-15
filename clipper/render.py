"""Turn a source section into a finished 1080x1920 clip with hook, commentary and word-by-word captions."""
from pathlib import Path

from .common import run

FONT = "DejaVu Sans"          # captions
HOOK_FONT = "Anton"           # top hook, bundled in fonts/
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
W, H = 1080, 1920
YELLOW = "&H0000E5FF&"  # ASS colors are &HAABBGGRR


def _esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _hook_text(h):
    parts = _esc(h).upper().split("*")
    # odd-numbered parts were inside *stars*
    return "".join(f"{{\\c{YELLOW}}}{p}{{\\c&H00FFFFFF&}}" if i % 2 else p for i, p in enumerate(parts))


def _ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(pick, words, layout, out_path, commentary=True):
    """words: absolute-time words; pick: has start/end/hook/commentary."""
    dur = pick["end"] - pick["start"]
    top_v = 230 if layout == "blur" else 640       # where hook/commentary sit
    cap_v = 430 if layout == "blur" else 260       # caption distance from bottom
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Hook,{HOOK_FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,7,4,8,70,70,{top_v},1",
        f"Style: Note,{FONT},52,&H00FFFFFF,&H00FFFFFF,&H40000000,&H00000000,-1,0,0,0,100,100,0,0,3,14,0,8,100,100,{top_v},1",
        f"Style: Cap,{FONT},80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{cap_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    # Hook: one line that stays up for the whole clip. Words wrapped in *stars* turn yellow.
    hook_end = dur if not commentary else min(3.0, dur)
    lines.append(f"Dialogue: 2,{_ts(0)},{_ts(hook_end)},Hook,,0,0,0,,{_hook_text(pick['hook'])}")

    # Commentary: each line shows until the next one (max 5s), never during the hook.
    notes = sorted(pick.get("commentary", []), key=lambda x: float(x["at"])) if commentary else []
    for i, n in enumerate(notes):
        s = max(float(n["at"]), hook_end)
        nxt = float(notes[i + 1]["at"]) if i + 1 < len(notes) else dur
        e = min(s + 8.0, nxt, dur)
        if e - s >= 1.0:
            lines.append(f"Dialogue: 1,{_ts(s)},{_ts(e)},Note,,0,0,0,,{_esc(n['text'])}")

    # Captions: 3-word groups, current word in yellow.
    rel = [dict(w, s=w["s"] - pick["start"], e=w["e"] - pick["start"]) for w in words
           if pick["start"] <= w["s"] < pick["end"]]
    groups, cur = [], []
    for w in rel:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    for g in groups:
        for j, w in enumerate(g):
            s = w["s"]
            e = g[j + 1]["s"] if j + 1 < len(g) else g[-1]["e"] + 0.15
            if e <= s:
                continue
            txt = " ".join(
                (f"{{\\c{YELLOW}}}{_esc(x['w']).upper()}{{\\c&H00FFFFFF&}}" if k == j else _esc(x["w"]).upper())
                for k, x in enumerate(g))
            lines.append(f"Dialogue: 0,{_ts(s)},{_ts(min(e, dur))},Cap,,0,0,0,,{txt}")
    Path(out_path).write_text("\n".join(lines) + "\n")
    return out_path


def render(src, src_offset, pick, words, out, facecam=None, commentary=True):
    """src: downloaded section; src_offset: seconds into src where the clip starts."""
    out = Path(out)
    layout = "stack" if facecam else "blur"
    ass = build_ass(pick, words, layout, out.with_suffix(".ass"), commentary)
    ass_arg = str(ass).replace(":", r"\:") + "':fontsdir='" + str(FONTS_DIR).replace(":", r"\:")
    if facecam:
        f = facecam
        vf = (f"[0:v]crop=iw*{f['w']}:ih*{f['h']}:iw*{f['x']}:ih*{f['y']},"
              f"scale={W}:608:force_original_aspect_ratio=increase,crop={W}:608[cam];"
              f"[0:v]crop='min(iw,ih*{W}/1312)':ih,scale={W}:1312[game];"
              f"[cam][game]vstack,ass='{ass_arg}',fps=30[v]")
    else:
        vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,"
              f"eq=brightness=-0.12[bg];[0:v]scale={W}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass='{ass_arg}',fps=30[v]")
    dur = pick["end"] - pick["start"]
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{src_offset:.2f}", "-i", src, "-t", f"{dur:.2f}",
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", out])
    return out
