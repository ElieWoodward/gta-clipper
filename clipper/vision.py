"""Look at a clip's frames before cropping it to vertical, so nothing that matters ends up off screen.

Claude sees a few frames and reports: is there a webcam overlay (and where), where the action is, and whether the
important stuff is spread across the screen (then the clip keeps a wider view instead of a tight crop).
"""
import base64
import json

from .common import log, run

PROMPT = """These are {n} frames from one short clip of a GTA livestream (16:9). It will be turned into a 9:16 vertical
video, so most of the width gets cropped away. Tell me how to crop it so nothing important is lost.

1. facecam: is there a webcam overlay showing the streamer's real face (a fixed rectangle, usually in a corner)? If yes,
   give its bounding box as fractions of the frame width/height (x, y = top-left corner). Include the whole webcam
   rectangle, not just the face. Game characters are NOT a facecam. If there's none, null.
2. focus_x: horizontal center (0 = left edge, 1 = right edge) of what the viewer must see: the characters talking or
   the action. Ignore the facecam, minimap and chat overlay when choosing it.
3. layout: "fill" if the important action fits in a tall narrow slice around focus_x (about a third of the width).
   "wide" if it's spread out (two people far apart, a chase across the screen, important on-screen text or menus), so
   the clip should show more of the frame.

Return ONLY JSON: {{"facecam": {{"x": float, "y": float, "w": float, "h": float}} or null, "focus_x": float, "layout": "fill" or "wide", "why": str}}"""


def frames(src, offset, dur, out_dir, n=3):
    paths = []
    for i in range(n):
        t = offset + dur * (i + 0.5) / n
        p = out_dir / f"frame_{i}.jpg"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
             "-vf", "scale=960:-2", "-q:v", "4", p])
        paths.append(p)
    return paths


def _clean(d):
    out = {"facecam": None, "focus_x": 0.5, "layout": "fill", "why": str(d.get("why", ""))[:200]}
    try:
        out["focus_x"] = min(0.9, max(0.1, float(d.get("focus_x", 0.5))))
    except (TypeError, ValueError):
        pass
    if d.get("layout") in ("fill", "wide"):
        out["layout"] = d["layout"]
    f = d.get("facecam")
    if isinstance(f, dict):
        try:
            x, y, w, h = (float(f[k]) for k in ("x", "y", "w", "h"))
            # a believable webcam box: not tiny, not most of the screen, inside the frame
            if 0.06 <= w <= 0.6 and 0.06 <= h <= 0.6 and x >= 0 and y >= 0 and x + w <= 1.01 and y + h <= 1.01:
                out["facecam"] = {"x": round(x, 3), "y": round(y, 3), "w": round(min(w, 1 - x), 3), "h": round(min(h, 1 - y), 3)}
        except (KeyError, TypeError, ValueError):
            pass
    return out


def analyze_frames(cfg, src, offset, dur, work_dir, client=None):
    """Returns {facecam, focus_x, layout, why}. On any failure: centered fill crop, no facecam."""
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        imgs = frames(src, offset, dur, work_dir)
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(p.read_bytes()).decode()}} for p in imgs]
        content.append({"type": "text", "text": PROMPT.format(n=len(imgs))})
        msg = client.messages.create(model=cfg["models"]["judge"], max_tokens=400,
                                     messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        return _clean(json.loads(text[start:end + 1]))
    except Exception as e:
        log.warning("frame check failed (%s); using a centered crop", e)
        return _clean({})
