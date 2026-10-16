"""Turn a source section into a finished 1080x1920 clip in the style of viral GTA RP clips:
gameplay filling the frame, the hook in a white box at the top (TikTok's own text style, with color emoji),
word-by-word captions, and an optional big punchline at the bottom when the payoff lands."""
import glob
import re
from pathlib import Path

from .common import run

FONT = "DejaVu Sans"          # captions (libass)
PUNCH_FONT = "Anton"          # bottom punchline (libass), bundled in fonts/
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
HOOK_FONT_FILE = FONTS_DIR / "TikTokSans.ttf"
EMOJI_FONT_CANDIDATES = ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                         "/usr/share/fonts/noto/NotoColorEmoji.ttf", str(FONTS_DIR / "NotoColorEmoji.ttf")]
W, H = 1080, 1920
YELLOW = "&H0000E5FF&"  # ASS colors are &HAABBGGRR

HOOK_TOP = 250          # px from the top: clear of TikTok's top bar
HOOK_MAX_W = 980
CAP_FROM_BOTTOM = 470   # captions sit above TikTok's caption/username area
PUNCH_FROM_BOTTOM = 700

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿]")
JOINERS = "‍️︎"


def _esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def strip_emoji(t):
    return re.sub(r"\s+", " ", EMOJI_RE.sub("", "".join(ch for ch in t if ch not in JOINERS)).replace("\U0001F3FB", "")).strip()


# ------------------------------------------------------------------ hook image
def _emoji_font():
    from PIL import ImageFont
    for path in EMOJI_FONT_CANDIDATES + glob.glob("/usr/share/fonts/**/NotoColorEmoji*.ttf", recursive=True):
        try:
            return ImageFont.truetype(path, 109)   # CBDT bitmap font: only size 109 works
        except OSError:
            continue
    return None


def _runs(token):
    """Split a word into text and emoji runs: [("t", "bro"), ("e", "😭")]."""
    out = []
    for ch in token:
        if ch in JOINERS or "\U0001F3FB" <= ch <= "\U0001F3FF":   # joiners and skin tones: drop
            continue
        kind = "e" if EMOJI_RE.match(ch) else "t"
        if kind == "t" and out and out[-1][0] == "t":
            out[-1] = ("t", out[-1][1] + ch)
        else:
            out.append((kind, ch))
    return out


def hook_image(text, out_path, size=76):
    """Render the hook TikTok-style: black bold text on white rounded boxes, one per line, color emoji inline."""
    from PIL import Image, ImageDraw, ImageFont
    efont = _emoji_font()
    if efont is None:
        text = strip_emoji(text)
    text = re.sub(r"\s+", " ", text.replace("*", "")).strip()
    while True:
        font = ImageFont.truetype(str(HOOK_FONT_FILE), size)
        try:
            font.set_variation_by_name("ExtraBold")
        except Exception:
            pass
        asc, desc = font.getmetrics()
        line_h = asc + desc
        esize = int(size * 1.08)
        EGAP = int(size * 0.08)
        space = font.getlength(" ")

        def width(tok):
            return sum(font.getlength(v) if k == "t" else esize + EGAP for k, v in _runs(tok))

        toks = []
        for tok in text.split(" "):   # keep emojis on the same line as the word before them
            if toks and all(k == "e" for k, _ in _runs(tok)) and _runs(tok):
                toks[-1] += " " + tok
            else:
                toks.append(tok)
        def wrap(limit):
            out, cur = [], []
            for tok in toks:
                if cur and width(" ".join(cur + [tok])) > limit:
                    out.append(cur)
                    cur = []
                cur.append(tok)
            return out + [cur] if cur else out

        lines = wrap(HOOK_MAX_W - 60)
        # balance the lines so the last one isn't a lonely word
        limit = width(" ".join(toks)) / len(lines)
        while limit < HOOK_MAX_W - 60:
            trial = wrap(limit)
            if len(trial) <= len(lines):
                lines = trial
                break
            limit += 12
        if len(lines) <= 3 or size <= 46:
            break
        size -= 4

    pad_x, pad_y, gap = 30, 16, 0
    box_h = line_h + 2 * pad_y
    img = Image.new("RGBA", (W, len(lines) * (box_h + gap) + 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    widths = [sum(width(t) for t in ln) + space * (len(ln) - 1) for ln in lines]
    # boxes first, so neighbouring lines merge into one shape like TikTok's text background
    for i, lw in enumerate(widths):
        x0 = (W - lw) / 2 - pad_x
        y0 = i * (box_h + gap)
        d.rounded_rectangle([x0, y0, x0 + lw + 2 * pad_x, y0 + box_h], radius=22, fill=(255, 255, 255, 255))
    for i, (ln, lw) in enumerate(zip(lines, widths)):
        x = (W - lw) / 2
        y = i * (box_h + gap) + pad_y
        for j, tok in enumerate(ln):
            for k, v in _runs(tok):
                if k == "t":
                    d.text((x, y), v, font=font, fill=(0, 0, 0, 255))
                    x += font.getlength(v)
                else:
                    em = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
                    ImageDraw.Draw(em).text((0, 0), v, font=efont, embedded_color=True)
                    bb = em.getbbox()
                    if bb:
                        em = em.crop(bb).resize((esize, int(esize * (bb[3] - bb[1]) / max(1, bb[2] - bb[0]))),
                                                Image.LANCZOS)
                        img.alpha_composite(em, (int(x + EGAP), int(y + (line_h - em.height) / 2 + desc * 0.15)))
                    x += esize + EGAP
            if j < len(ln) - 1:
                x += space
    img = img.crop(img.getbbox() or (0, 0, W, 10))
    canvas = Image.new("RGBA", (W, img.height), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((W - img.width) // 2, 0))
    canvas.save(out_path)
    return out_path


# ------------------------------------------------------------------ captions + punchline
def build_ass(pick, words, out_path, commentary=False):
    """words: absolute-time words; pick: start/end, optional punch_text/punch_at (seconds from clip start)."""
    dur = pick["end"] - pick["start"]
    punch = strip_emoji(pick.get("punch_text") or "").upper()
    punch_at = float(pick.get("punch_at") or 0)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{FONT},76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{CAP_FROM_BOTTOM},1",
        f"Style: Punch,{PUNCH_FONT},124,{YELLOW},{YELLOW},&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,8,4,2,60,60,{PUNCH_FROM_BOTTOM},1",
        f"Style: Note,{FONT},52,&H00FFFFFF,&H00FFFFFF,&H40000000,&H00000000,-1,0,0,0,100,100,0,0,3,14,0,8,100,100,520,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if punch and punch_at < dur - 0.5:
        # pops in slightly oversized, then settles
        lines.append(f"Dialogue: 3,{_ts(punch_at)},{_ts(min(dur, punch_at + 3.5))},Punch,,0,0,0,,"
                     f"{{\\fscx120\\fscy120\\t(0,150,\\fscx100\\fscy100)}}{_esc(punch)}")

    if commentary:  # optional context lines (off by default)
        notes = sorted(pick.get("commentary", []), key=lambda x: float(x["at"]))
        for i, n in enumerate(notes):
            s = float(n["at"])
            e = min(s + 8.0, float(notes[i + 1]["at"]) if i + 1 < len(notes) else dur, dur)
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


# ------------------------------------------------------------------ render
def _game(label, h, mode, fx, src="[0:v]"):
    """Gameplay area W x h. fill: tall crop centered on the action. wide: a square slice of the frame (much more of the
    screen stays visible) over a blurred copy, for clips where the important stuff is spread out."""
    def crop(aspect_w):  # crop width aspect_w*ih around fx, clamped inside the frame
        return f"crop='min(iw,{aspect_w}*ih)':ih:'max(0,min(iw-ow,iw*{fx:.3f}-ow/2))':0"
    if mode == "wide":
        fg_h = min(h, W)
        return (f"{src}split[{label}a][{label}b];"
                f"[{label}a]scale={W}:{h}:force_original_aspect_ratio=increase,crop={W}:{h},boxblur=20:2[{label}bg];"
                f"[{label}b]{crop(W / fg_h)},scale={W}:{fg_h}[{label}fg];"
                f"[{label}bg][{label}fg]overlay=0:(H-h)/2[{label}]")
    return f"{src}{crop(W / h)},scale={W}:{h}[{label}]"


def render(src, src_offset, pick, words, out, facecam=None, commentary=False, layout="fill", focus_x=0.5):
    """src: downloaded section; src_offset: seconds into src where the clip starts.
    layout: fill (gameplay fills the screen, cropped around focus_x), wide (more of the frame visible),
    blur (whole frame over a blurred copy). With a facecam box the cam goes on top and gameplay below."""
    out = Path(out)
    ass = build_ass(pick, words, out.with_suffix(".ass"), commentary)
    hook_png = hook_image(pick["hook"], out.with_suffix(".hook.png"))
    ass_arg = str(ass).replace(":", r"\:") + "':fontsdir='" + str(FONTS_DIR).replace(":", r"\:")
    mode = "wide" if layout == "wide" else "fill"
    if facecam:
        f = facecam
        cam_crop = f"crop=iw*{f['w']}:ih*{f['h']}:iw*{f['x']}:ih*{f['y']}"
        # The cam is already on top, so blur it out of the gameplay underneath instead of showing it twice.
        base = (f"[0:v]split=3[c0][g0][g1];[c0]{cam_crop},"
                f"scale={W}:608:force_original_aspect_ratio=increase,crop={W}:608[cam];"
                f"[g1]{cam_crop},boxblur=40:3,eq=brightness=-0.2[camblur];"
                f"[g0][camblur]overlay=main_w*{f['x']}:main_h*{f['y']}[gclean];"
                + _game("game", H - 608, mode, focus_x, "[gclean]") + ";[cam][game]vstack[base]")
        top = 560   # on the seam between cam and gameplay
    elif layout == "blur":
        base = (f"[0:v]split[b0][b1];[b0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,"
                f"eq=brightness=-0.12[bg];[b1]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
        top = HOOK_TOP
    else:
        base = _game("base", H, mode, focus_x)
        top = HOOK_TOP
    vf = (f"{base};[base][1:v]overlay=(W-w)/2:{top}[hk];"
          f"[hk]ass='{ass_arg}',fps=30,format=yuv420p[v]")
    dur = pick["end"] - pick["start"]
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{src_offset:.2f}", "-i", src, "-i", hook_png,
         "-t", f"{dur:.2f}", "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", out])
    return out
