"""python -m clipper {process|post|analytics|learn|accounts|probe}"""
import os
import sys

from . import jobs, services


def probe():
    """Check the Twitch signals for one channel's latest VOD without downloading or spending API credit.
    Uses TEST_SOURCE (twitch:<channel> or a VOD link)."""
    import json
    from . import twitch
    from .common import run
    src = os.environ.get("TEST_SOURCE", "").strip() or "twitch:benjiramos"
    if src.startswith("twitch:"):
        ch = src.split(":", 1)[1]
        raw = json.loads(run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", "3",
                              f"https://www.twitch.tv/{ch}/videos?filter=archives&sort=time"]))
        if not raw.get("entries"):
            print(f"::error::no past broadcasts found for twitch.tv/{ch}", flush=True)
            return
        e = raw["entries"][0]
        vod = {"id": f"twitch:{e['id']}", "url": e.get("url"), "title": e.get("title"), "streamer": {"channel": ch}}
    else:
        vod = {"id": "probe", "url": src, "title": "", "streamer": {}}
    vid = twitch.video_id(vod)
    info = twitch.video_info(vid) if vid else None
    print(f"::notice::probe video {vid}: {info}", flush=True)
    length = int((info or {}).get("lengthSeconds") or 3600)
    sig = twitch.signals(vod, length)
    print(f"::notice::clips scanned on the channel: {twitch.STATS.get('clips_seen', 0)}, from this VOD: {len(sig['clips'])}",
          flush=True)
    for c in sig["clips"][:8]:
        print(f"::notice::viewer clip at {c['offset']:.0f}s, {c['views']} views: {c['title']}", flush=True)
    if sig["laughs"] is not None:
        for t, st in twitch.chat_peaks(sig["laughs"], sig["total"], 8):
            print(f"::notice::chat spike at {t}s ({st:.1f}x): {' | '.join(twitch.chat_sample(sig['msgs'], t - 20, t + 10, 5))}",
                  flush=True)
    if not sig["clips"] and not sig["msgs"]:
        print("::error::no Twitch signals came back; the picker will fall back to loudness", flush=True)


CMDS = {"process": jobs.process, "post": jobs.post, "analytics": jobs.analytics, "learn": jobs.learn,
        "accounts": lambda: print(services.blotato_accounts()), "probe": probe}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CMDS:
        sys.exit(f"usage: python -m clipper [{'|'.join(CMDS)}]")
    try:
        CMDS[sys.argv[1]]()
    except Exception as e:  # put the real reason at the top of the run page
        print(f"::error::{type(e).__name__}: {str(e)[:600]}", flush=True)
        raise
