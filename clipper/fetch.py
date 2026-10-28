"""Find new VODs and download only what's needed: full audio for analysis, video just for chosen clips."""
import json
import re
import time
from datetime import datetime, timedelta, timezone

from .common import log, run, WORK

LISTING_URLS = {
    "twitch": "https://www.twitch.tv/{ch}/videos?filter=archives&sort=time",
    "kick": "https://kick.com/{ch}/videos",
    "youtube": "https://www.youtube.com/@{ch}/streams",
}


LISTING_NOTES = []   # why streams were skipped, for the run page


def list_new_vods(cfg, state):
    """Return [{id, url, title, streamer}] not yet processed."""
    f = cfg["fetch"]
    skipped = []
    seen = set(state["processed_vods"])
    out = []
    # Streamers who show their face go first: when the queue fills up, their clips are the ones that get made.
    cams = state.get("streamers", {})
    # Paid campaigns you joined go first, then other campaigns, then streamers who show their face.
    order = sorted(cfg.get("streamers", []), key=lambda s: (not s.get("campaign_joined"), not s.get("campaign"),
                                                            not (s.get("facecam") or cams.get(s["name"], {}).get("facecam"))))
    for s in order:
        if not s.get("enabled", True):
            continue
        entries = None
        if s["platform"] == "twitch":   # Twitch's own API: faster and steadier than scraping the videos page
            from . import twitch
            try:
                entries = twitch.list_archives(s["channel"], f["vods_per_streamer"]) or None
            except Exception as e:
                log.info("twitch listing for %s failed (%s); trying yt-dlp", s["name"], e)
        if entries is None:
            url = LISTING_URLS[s["platform"]].format(ch=s["channel"])
            try:
                raw = run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", f["vods_per_streamer"], url])
                entries = json.loads(raw).get("entries") or []
            except RuntimeError as e:
                print(f"::warning::couldn't list {s['name']}'s streams: {str(e)[:200]}", flush=True)
                continue
        for e in entries[: f["vods_per_streamer"]]:
            vid = f"{s['platform']}:{e.get('id')}"
            if vid in seen:
                continue
            dur = e.get("duration") or 0
            if dur and dur > f["max_vod_hours"] * 3600:
                skipped.append(f"{s['name']} {e.get('id')} too long")
                continue
            ts = e.get("timestamp")
            if ts and datetime.fromtimestamp(ts, timezone.utc) < datetime.now(timezone.utc) - timedelta(days=f["max_vod_age_days"]):
                skipped.append(f"{s['name']} {e.get('id')} too old")
                continue
            pat = f.get("title_match") or ""
            game, ranges = (twitch_game(vid, f.get("game_match") or "grand theft auto|gta")
                            if s["platform"] == "twitch" else (None, None))
            if pat and game:
                if not ranges:
                    log.info("skipping %s: category is %s", vid, game)
                    skipped.append(f"{s['name']} {e.get('id')} not GTA ({game})")
                    continue  # Twitch says it's another game for the whole stream
            elif pat and not re.search(pat, e.get("title") or "", re.I):
                skipped.append(f"{s['name']} {e.get('id')} title not GTA")
                continue  # title doesn't look like GTA
            out.append({"id": vid, "url": e.get("url") or e.get("webpage_url"), "title": e.get("title", ""),
                        "game": game, "gta_ranges": ranges, "streamer": s})
    LISTING_NOTES.append("; ".join(skipped[:8]) or "nothing skipped")
    for u in cfg.get("manual_urls") or []:
        vid = f"manual:{u}"
        if vid not in seen:
            out.append({"id": vid, "url": u, "title": "", "streamer": {"name": "manual", "platform": "manual"}})
    return out


def twitch_game(vid, game_match="grand theft auto|gta"):
    """(category, gta_ranges): the stream's main category and the [start, end] stretches (seconds, end None = to the
    end) Twitch has under GTA. (None, None) if Twitch can't tell."""
    from . import twitch
    try:
        ch = twitch.chapters(twitch.video_id({"id": vid}))
    except Exception:
        return None, None
    if not ch:
        return None, None
    ranges = [[s, e] for s, e, g in ch if re.search(game_match, g or "", re.I)]
    game = next((g for _, _, g in ch if re.search(game_match, g or "", re.I)), ch[0][2])
    return game, ranges


class StillLive(Exception):
    """The VOD belongs to a stream that hasn't ended yet; try again on a later run."""


def download_audio(vod, dest_dir):
    """Download audio only, as 16 kHz mono wav (small and enough for analysis)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(dest_dir / "audio.%(ext)s")
    t0 = time.time()
    # A VOD of a stream that's still live downloads at real-time speed, so skip it until the stream ends.
    run(["yt-dlp", "-N", "16", "--match-filter", "!is_live & live_status!=is_live & live_status!=post_live",
         "-f", "bestaudio/worst", "-x", "--audio-format", "m4a", "-o", tmpl, vod["url"]])
    if not list(dest_dir.glob("audio.*")):
        raise StillLive(vod["id"])
    print(f"::notice::audio downloaded in {time.time() - t0:.0f}s", flush=True)
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
