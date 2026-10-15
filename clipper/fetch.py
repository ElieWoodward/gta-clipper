"""Find new VODs and download only what's needed: full audio for analysis, video just for chosen clips."""
import json
from datetime import datetime, timedelta, timezone

from .common import log, run, WORK

LISTING_URLS = {
    "twitch": "https://www.twitch.tv/{ch}/videos?filter=archives&sort=time",
    "kick": "https://kick.com/{ch}/videos",
    "youtube": "https://www.youtube.com/@{ch}/streams",
}


def list_new_vods(cfg, state):
    """Return [{id, url, title, streamer}] not yet processed."""
    f = cfg["fetch"]
    seen = set(state["processed_vods"])
    out = []
    for s in cfg.get("streamers", []):
        if not s.get("enabled", True):
            continue
        url = LISTING_URLS[s["platform"]].format(ch=s["channel"])
        try:
            raw = run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", f["vods_per_streamer"], url])
        except RuntimeError as e:
            log.warning("listing %s failed: %s", s["name"], e)
            continue
        for e in (json.loads(raw).get("entries") or [])[: f["vods_per_streamer"]]:
            vid = f"{s['platform']}:{e.get('id')}"
            if vid in seen:
                continue
            dur = e.get("duration") or 0
            if dur and dur > f["max_vod_hours"] * 3600:
                continue
            ts = e.get("timestamp")
            if ts and datetime.fromtimestamp(ts, timezone.utc) < datetime.now(timezone.utc) - timedelta(days=f["max_vod_age_days"]):
                continue
            out.append({"id": vid, "url": e.get("url") or e.get("webpage_url"), "title": e.get("title", ""), "streamer": s})
    for u in cfg.get("manual_urls") or []:
        vid = f"manual:{u}"
        if vid not in seen:
            out.append({"id": vid, "url": u, "title": "", "streamer": {"name": "manual", "platform": "manual"}})
    return out


def download_audio(vod, dest_dir):
    """Download audio only, as 16 kHz mono wav (small and enough for analysis)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(dest_dir / "audio.%(ext)s")
    run(["yt-dlp", "-N", "8", "-f", "bestaudio/worst", "-x", "--audio-format", "m4a", "-o", tmpl, vod["url"]])
    src = next(dest_dir.glob("audio.*"))
    wav = dest_dir / "audio.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", wav])
    return wav


def download_section(vod, start, end, dest):
    """Download just [start, end] seconds of the video at up to 1080p."""
    sec = f"*{int(start)}-{int(end) + 1}"
    run(["yt-dlp", "-N", "8", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--download-sections", sec,
         "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "-o", str(dest), vod["url"]])
    return dest
