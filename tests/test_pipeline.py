"""Offline end-to-end test: synthetic stream -> picks -> rendered clips -> post -> analytics -> prune -> learn.
External services (Groq, Claude, Blotato, GitHub, yt-dlp) are faked. Run: python tests/test_pipeline.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

TMP = Path(tempfile.mkdtemp(prefix="cliptest_"))
import atexit  # noqa: E402
atexit.register(shutil.rmtree, TMP, True)   # each run leaves ~1 GB otherwise
REPO = Path(__file__).resolve().parent.parent
shutil.copy(REPO / "config.yaml", TMP / "config.yaml")
# Sections 1-8 test the Blotato auto-posting path; section 9 switches to email mode.
_cfg_text = (TMP / "config.yaml").read_text().replace("  mode: email ", "  mode: blotato ", 1)
assert "mode: blotato" in _cfg_text
_cfg_text = _cfg_text.replace("launch:                           # GTA 6", "launch:  # GTA 6", 1).replace(
    "  enabled: true\n  date: \"2026-11-19\"", "  enabled: false\n  date: \"2026-11-19\"", 1)
assert "enabled: false\n  date:" in _cfg_text   # launch mode is tested on its own in section 21
import re as _re  # noqa: E402
_cfg_text = _re.sub(r'tiktok_paused_until: "[^"]*"', 'tiktok_paused_until: ""', _cfg_text)   # tested in section 25
(TMP / "config.yaml").write_text(_cfg_text)
os.environ.update(CLIPPER_ROOT=str(TMP), CLIPPER_WORK=str(TMP / "work"), FORCE="1", BLOTATO_API_KEY="test-key")
sys.path.insert(0, str(REPO))

from clipper import fetch, highlights, jobs, services, common, twitch, render  # noqa: E402
import numpy as np  # noqa: E402

# ---------- synthetic 6-minute "stream": test pattern, quiet tone, two loud moments ----------
SRC = TMP / "stream.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=360",
                "-f", "lavfi", "-i", "aevalsrc='0.02*sin(2*PI*220*t)*(1+25*between(t,118,126)+25*between(t,248,253))':s=44100:d=360",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(SRC)], check=True)


def fake_audio(vod, d):
    d.mkdir(parents=True, exist_ok=True)
    wav = d / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(SRC), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    return wav


def fake_section(vod, s, e, dest):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(s), "-to", str(e), "-i", str(SRC),
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(dest)], check=True)
    return dest


SCRIPT = "no way no way he is actually doing it the cops are right behind him oh my god he made the jump"


def fake_transcribe(wav, model, start=0.0, end=None, **kw):
    words, t = [], start + 2
    while t < end - 1:
        for w in SCRIPT.split():
            if t >= end - 1:
                break
            words.append({"w": w, "s": round(t, 2), "e": round(t + 0.3, 2)})
            t += 0.38
        t += 1.5
    return words


class FakeClaude:
    def __init__(self, reply):
        self.reply = reply
        self.messages = self
        self.prompts = []

    def create(self, **kw):
        content = kw["messages"][0]["content"]
        if isinstance(content, list):  # frame check: images + question
            assert content[0]["type"] == "image" and "facecam" in content[-1]["text"]
            self.vision_calls = getattr(self, "vision_calls", 0) + 1
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(VISION.pop(0) if VISION else
                                   {"facecam": None, "focus_x": 0.5, "layout": "fill", "why": "default"}))])
        self.prompts.append(content)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply(kw))])


def judge_reply(kw):
    p = kw["messages"][0]["content"]
    if "ruthless short-form video editor" in p:
        assert "When you" in p and "{hook_guide}" not in p  # editor gets the hook guide
        return json.dumps({"clips": [{"i": 0, "best": "bro really thought the bridge would hold 😭💀", "score": 8}]})
    assert "{hook_guide}" not in p and ("[id 1]" not in p or ("viewers clipped this" in p and "Chat at that moment" in p))
    import re
    picks = []
    for i, s, e in [(int(a), float(b), float(c)) for a, b, c in re.findall(r"\[id (\d+)\] (\d+)s-(\d+)s", p)][:2]:
        picks.append({"id": i, "start": s + 5, "end": s + 30,
                      "score": 8 if i == 0 else 6, "hook": "he really thought the *bridge* would hold",
                      "punch_text": "HE MADE THE JUMP", "punch_at": s + 25,  # absolute on purpose: validator must fix it
                      "title": "Bridge jump escape", "caption": "He really thought this would work",
                      "tags": ["chaos", "police"], "why": "clear payoff",
                      "commentary": [{"at": 1, "text": "He has 4 stars and no plan"},
                                     {"at": 12, "text": "The bridge is closed for construction"},
                                     {"at": 30, "text": "Chat told him not to try it"}]})
    return json.dumps({"picks": picks})


VISION = [  # first clip: webcam found top-right + wide; second: tight crop on the left
    {"facecam": {"x": 0.72, "y": 0.03, "w": 0.25, "h": 0.25}, "focus_x": 0.4, "layout": "wide", "why": "two players far apart"},
    {"facecam": None, "focus_x": 0.2, "layout": "fill", "why": "action on the left"},
    {"facecam": {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}, "focus_x": 3, "layout": "nonsense"},  # invalid -> cleaned
]
import clipper.vision as vision  # noqa: E402
vision.anthropic = None
_real_analyze = vision.analyze_frames
CLAUDE = None
jobs.vision.analyze_frames = lambda cfg, src, off, dur, d, client=None: _real_analyze(cfg, src, off, dur, d, client=CLAUDE)
highlights.transcribe = fake_transcribe

# Fake Twitch: one viewer clip near the second loud moment, and chat laughing near the first.
_msgs = [(t, "hi chat") for t in range(0, 360, 3)] + [(t, "KEKW no way") for t in range(124, 132) for _ in range(6)]
_l, _t = twitch.laugh_curve(_msgs, 360)
twitch.signals = lambda vod, length: {"clips": [{"offset": 230.0, "duration": 30.0, "views": 420, "title": "BRIDGE JUMP"}],
                                      "msgs": _msgs, "laughs": _l, "total": _t}
assert twitch.video_id({"url": "https://www.twitch.tv/videos/2591234567"}) == "2591234567"
assert twitch.video_id({"id": "twitch:v2591234567", "url": None}) == "2591234567"
assert twitch.chat_peaks(_l, _t, 3)[0][0] in range(115, 130), twitch.chat_peaks(_l, _t, 3)
fetch.download_audio = fake_audio
fetch.download_section = fake_section

# Fake Blotato + GitHub
posted, issues = [], {}
services.blotato_upload = lambda path: f"https://database.blotato.io/media/{Path(path).name}"
services.gh_issue = lambda t, b, l: issues.setdefault(len(issues) + 1, {"title": t, "labels": l}) and len(issues)
services.gh_update = lambda n, **f: issues[n].update(f)
services.gh_comment = lambda n, b: None
services.gh_upsert_pinned = lambda label, title, body: issues.update({label: body})
services.blotato_account_id = lambda p: f"acct_{p}"
services.blotato_post = lambda p, a, url, text, title=None, ai_label=False: posted.append((p, url, text)) or f"sub{len(posted)}"
services.blotato_wait = lambda sub: ("published", f"https://www.youtube.com/shorts/vid{sub}")

# ---------- 1. process ----------
cfg, state = common.load_config(), common.load_state()
cfg["clip"]["min_per_vod"] = 2
vod = {"id": "twitch:123", "url": "https://twitch.tv/videos/123", "title": "GTA 6 day 1",
       "streamer": {"name": "teststreamer", "platform": "twitch"}}
claude = FakeClaude(judge_reply)
CLAUDE = claude
d = TMP / "work" / "vod"
d.mkdir(parents=True)
wav = fake_audio(vod, d)
picks, words = highlights.analyze(cfg, vod, wav, client=claude, keep_all=True)
print("loudness picks ->", [(p["start"], p["end"], p["score"]) for p in picks])
assert len(picks) == 2, picks
for p in picks:
    assert cfg["clip"]["min_seconds"] <= p["end"] - p["start"] <= cfg["clip"]["max_seconds"], p
    assert "*" not in p["hook"] and 0 < p["punch_at"] < p["end"] - p["start"], p
assert picks[0]["hook"].endswith("😭💀"), picks[0]["hook"]
for p in picks:
    jobs.make_clip(cfg, state, vod, p, words, d)

# Same picks with a facecam layout
vod2 = dict(vod, id="twitch:456", streamer={"name": "camstreamer", "platform": "twitch",
                                          "facecam": {"x": 0.7, "y": 0.02, "w": 0.28, "h": 0.28}})
jobs.make_clip(cfg, state, vod2, picks[0], words, d)

state = common.load_state()
assert len(state["clips"]) == 3
layouts = sorted(c["layout"] for c in state["clips"].values())
for c in state["clips"].values():  # punchline found in the clip's own audio and timed to it
    assert c["punch_text"] == "HE MADE THE JUMP" and 0 < c["punch_at"] < c["duration"], (c["punch_text"], c["punch_at"])
from clipper.jobs import place_punchline  # noqa: E402
assert place_punchline({"start": 0, "end": 30, "punch_text": "Don't say my name!"},
                       [{"w": "my", "s": 3, "e": 3.2}, {"w": "name", "s": 3.3, "e": 3.5}, {"w": "is", "s": 3.6, "e": 3.7}])["punch_text"] == ""
print("layouts:", layouts)
assert layouts == ["cam+fill", "cam+wide", "fill"], layouts  # 3rd: bad vision answer -> cleaned to fill, config cam
assert state["streamers"]["teststreamer"]["facecam"] is True
for c in state["clips"].values():
    out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_type:format=duration",
                                     "-of", "json", c["local_file"]], capture_output=True, text=True).stdout)
    v = [s for s in out["streams"] if s["codec_type"] == "video"][0]
    dur = float(out["format"]["duration"])
    print(c["id"], c["status"], f"{v['width']}x{v['height']}", f"{dur:.1f}s", c["score"])
    assert (v["width"], v["height"]) == (1080, 1920)
    assert 8 <= dur <= 59.5, dur
statuses = sorted(c["status"] for c in state["clips"].values())
assert statuses == ["approved", "approved", "rejected"], statuses  # score 6 is below min_score 7

# Save preview frames for a visual check
for c in state["clips"].values():
    for t in (1, 22):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", c["local_file"], "-frames:v", "1",
                        "-vf", "scale=360:-1", str(TMP / f"frame_{c['streamer']}_{c['status']}_{t}.png")], check=True)

# ---------- 2. post (twice: second run should post the next clip) ----------
jobs.post()
jobs.post()
state = common.load_state()
print("posted:", [(p, u.split("/")[-1]) for p, u, _ in posted])
assert {p for p, _, _ in posted} == {"tiktok", "youtube"}
assert all("Streamer:" in t and "#gta6" in t for _, _, t in posted)
assert not any("rejected" in u for _, u, _ in posted)

# ---------- 3. analytics + prune ----------
from datetime import timedelta  # noqa: E402
fake_items = []
for c in state["clips"].values():
    for pl, p in c["posts"].items():
        p["posted_at"] = (common.now_utc() - timedelta(hours=100)).isoformat()
        fake_items.append({"platform": pl, "mediaUrls": [c["media_url"]],
                           "state": {"type": "published", "postUrl": p["url"],
                                     "analytics": {"latest": {"metrics": {"viewsCount": "12000", "likesCount": "800"}}}}})
common.save_state(state)
services.blotato_published = lambda since: fake_items
jobs.analytics()
state = common.load_state()
assert all(p["views"] == 12000 for c in state["clips"].values() for p in c["posts"].values())
assert "status" in issues and "| 12000 |" in issues["status"]   # (total depends on how many posts today allows)
print("dashboard ok")

# ---------- 4. learn ----------
for i in range(6):  # pad with history so learn has enough rows
    state["clips"][f"old{i}"] = {"id": f"old{i}", "streamer": "teststreamer", "tags": ["fail"], "hook": f"old hook {i}",
                                 "duration": 70, "score": 7, "status": "posted", "created": common.now_utc().isoformat(),
                                 "posts": {"tiktok": {"views": 200 * i, "hour": "15:00",
                                                      "posted_at": (common.now_utc() - timedelta(days=5)).isoformat()}}}
common.save_state(state)
learn_claude = FakeClaude(lambda kw: json.dumps({"playbook": "- Police chases win\n- Menus lose", "summary": "Chases do best.",
                                                 "best_slots": ["12:00", "19:00"], "target_seconds": [14, 28]}))
os.environ["DRY_RUN"] = "1"
jobs.DRY_RUN = True
jobs.learn(client=learn_claude)
assert (TMP / "data" / "playbook.md").read_text().startswith("- Police")
assert jobs._slots(cfg, 2) == ["12:00", "19:00"]
# an answer cut short (no summary, no playbook) keeps last week's rules instead of crashing the report
jobs.learn(client=FakeClaude(lambda kw: json.dumps({"best_slots": ["12:00"]})))
assert (TMP / "data" / "playbook.md").read_text().startswith("- Police")
# Playbook now feeds the judge prompt
highlights.judge(cfg, vod, [{"id": 0, "start": 0, "end": 120, "spike": 5, "text": "x"}], client=claude)
assert "Police chases win" in claude.prompts[-1]
assert "clips of 14-28 seconds have been doing best" in claude.prompts[-1]
print("learn loop ok")
print("ALL TESTS PASSED")
print("artifacts in", TMP)

# ---------- 5. test-run output: clips + contact-sheet previews ----------
vod3 = dict(vod, id="twitch:789")
jobs._process_vod(cfg, common.load_state(), vod3, client=FakeClaude(judge_reply))
prev = sorted((TMP / "test-output" / "previews").glob("*.jpg"))
print("previews:", [p.name for p in prev])
assert prev, "no previews"
from PIL import Image  # noqa: E402
im = Image.open(prev[0])
assert im.width == 1500 and im.height > 500, im.size
shutil.copy(prev[0], TMP / "preview_sample.jpg")
print("PREVIEWS OK")

# ---------- 6. streams that start in Just Chatting and switch to GTA ----------
from clipper import fetch as _fetch, twitch as _tw  # noqa: E402
_orig = _tw.chapters
_tw.chapters = lambda vid: [(0.0, 2418.0, "Just Chatting"), (2418.0, None, "Grand Theft Auto V")]
assert _fetch.twitch_game("twitch:1") == ("Grand Theft Auto V", [[2418.0, None]])
_tw.chapters = lambda vid: [(0.0, None, "Just Chatting")]
assert _fetch.twitch_game("twitch:1") == ("Just Chatting", [])
_tw.chapters = _orig
print("CHAPTERS OK")

# ---------- 7. Twitch listing through the API ----------
_g = _tw._gql
_tw._gql = lambda body, timeout=15: {"data": {"user": {"videos": {"edges": [
    {"node": {"id": "2900842637", "title": "GTA V NOPIXEL", "lengthSeconds": 43425, "createdAt": "2026-10-14T20:49:43Z", "status": "RECORDED"}}]}}}}
lst = _tw.list_archives("xqc", 5)
assert lst[0]["id"] == "v2900842637" and lst[0]["duration"] == 43425 and lst[0]["timestamp"] > 1.7e9, lst
assert _tw.video_id({"id": "twitch:" + lst[0]["id"]}) == "2900842637"
_tw._gql = _g
print("LISTING OK")

# ---------- 8. live: spike detection, viewer clip clusters, state merge ----------
from clipper import live as _live  # noqa: E402
now = 1_000_000.0
calm = [(now - 600 + i * 0.5, "chatting about cars") for i in range(1170)]          # 2 msgs/s for ~10 min
boom = [(now - 14 + i * 0.1, "KEKW NO WAY") for i in range(140)]                   # 10 msgs/s, laughing
assert _live.chat_spike(calm, now) is None
r = _live.chat_spike(calm + boom, now)
assert r and r > 3, r
assert _live.chat_spike(calm[-200:] + boom, now) is None, "needs 3+ minutes of history"
raid = [(now - 14 + i * 0.1, "raid hype welcome raiders") for i in range(140)]
assert _live.chat_spike(calm + raid, now) is None, "raid spam isn't a reaction"
hit = _live.viewer_clip_cluster([{"offset": 1000, "duration": 30}, {"offset": 1010, "duration": 30},
                                 {"offset": 3000, "duration": 30}])
assert hit and 1015 < hit[0] < 1035 and hit[1] == 2, hit
assert _live.viewer_clip_cluster([{"offset": 1000, "duration": 30}]) is None
# the live watcher writes only data/live.json; the other jobs see its clips and posts
os.environ["CLIPPER_LIVE"] = "1"
st = common.load_state()
st["clips"]["livex"] = {"id": "livex", "live": True, "vod": "twitch:v9", "start": 100, "end": 130, "status": "posted",
                        "score": 9, "posts": {"tiktok": {"date": "2026-10-16"}}, "created": "2026-10-16T00:00:00+00:00"}
before = common.STATE_FILE.read_text() if common.STATE_FILE.exists() else None
common.save_state(st)
assert (common.STATE_FILE.read_text() if common.STATE_FILE.exists() else None) == before, "live must not touch state.json"
os.environ.pop("CLIPPER_LIVE")
merged = common.load_state()
assert merged["clips"]["livex"]["posts"]["tiktok"]["date"] == "2026-10-16"
assert jobs.overlaps_existing(merged, "twitch:v9", 105, 140) and not jobs.overlaps_existing(merged, "twitch:v9", 200, 240)
print("LIVE OK")

# ---------- 9. email mode: queue, emails, posted detection, spend cap, alerts ----------
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
from clipper import clipqueue, mail, health  # noqa: E402
(TMP / "config.yaml").write_text(_cfg_text.replace("  mode: blotato ", "  mode: email ", 1)
                                 .replace("  max_size: 8 ", "  max_size: 5 ", 1))   # the queue tests assume 5
os.environ.pop("DRY_RUN", None)
jobs.DRY_RUN = False
os.environ.update(GMAIL_ADDRESS="bot@example.com", GMAIL_APP_PASSWORD="abcd efgh ijkl mnop", NOTIFY_EMAIL="me@example.com")
cfg = common.load_config()
assert clipqueue.email_mode(cfg) and jobs._posting_ready(cfg)
st = common.load_state()
st["clips"] = {}
for k in ("spend", "health", "alerts", "email"):
    st.pop(k, None)
common.save_state(st)
if common.LIVE_FILE.exists():
    common.LIVE_FILE.unlink()

uploads, deleted, sent = {}, [], []
services.release_upload = lambda path, name=None: uploads.setdefault(name, (len(uploads) + 100, f"https://gh/dl/{name}"))
services.release_delete = lambda aid: deleted.append(aid)
services.download = lambda url, dest: shutil.copy(next(iter(rendered)), dest) and dest


def fake_send(subject, text, html=None, attachment=None, attachment_name=None):
    size = os.path.getsize(attachment) if attachment else 0
    sent.append({"subject": subject, "text": text, "html": html, "size": size})
    return f"<mid{len(sent)}@example.com>"


_orig_mail_send = mail.send
mail.send = fake_send
replies = []
mail.read_replies = lambda since_days=7: list(replies)
tt_uploads = []
clipqueue.tiktok_uploads = lambda handle, n=15: list(tt_uploads)
clipqueue.youtube_channel_id = lambda handle: "UC" + "x" * 22
clipqueue.youtube_uploads = lambda cid: []
clipqueue.REPLY_CHECK_MINUTES = 0   # read replies on every check in tests (live: about hourly)

# one real clip through make_clip: stored on the release, enters the queue
p8 = dict(picks[0], score=8)
real = jobs.make_clip(cfg, st, dict(vod, id="twitch:900"), p8, words, d)
rendered = [real["local_file"]]
assert real["status"] == "approved" and real["asset_id"] == 100 and real["media_url"].endswith(".mp4"), real
assert len(clipqueue.queue(st)) == 1 and clipqueue.room(cfg, st) == 4


def fake_clip(cid, score, hours_old, caption, facecam=False):
    c = dict(real, id=cid, score=score, hook=f"hook {cid}", title=f"title {cid}", caption=caption, facecam=facecam,
             created=(common.now_utc() - timedelta(hours=hours_old)).isoformat(), posts={}, asset_id=200 + len(st["clips"]),
             vod="twitch:v77", start=100.0 * len(st["clips"]), end=100.0 * len(st["clips"]) + 30, status="approved")
    for k in ("emailed_at", "email_message_id"):
        c.pop(k, None)
    st["clips"][cid] = c
    return c


fake_clip("a", 7, 5, "bro really thought the cops wouldn't notice 😭")
fake_clip("b", 9, 3, "the way he said I don't speak brexit brother 💀", facecam=True)
fake_clip("c", 8, 1, "he crashed into the only tree in the city 😭")
fake_clip("old", 10, 20, "this one will be too old to post")
assert len(clipqueue.queue(st)) == 5 and clipqueue.room(cfg, st) == 0
# full queue: no Claude work for free streams (the process job only lists paid campaign streams)
assert not jobs.claude_allowed(cfg, st)
common.save_state(st)
_orig_list = fetch.list_new_vods
_listed = []
fetch.list_new_vods = lambda c, *a, **k: (_listed.append([x["name"] for x in c["streamers"]]), [])[1]
os.environ.pop("FORCE")
jobs.FORCE = False
_ep, _ep_calls = jobs.email_post, []
jobs.email_post = lambda cfg: _ep_calls.append(1)
jobs.process()
jobs.email_post = _ep
assert _ep_calls == [1], "the stream check also runs the posting check"
# a full queue only stops free streams: paid campaign streamers are still checked
assert _listed == [[x["name"] for x in cfg["streamers"] if x.get("campaign") and x.get("enabled", True)]] or \
    (_listed == [] and not any(x.get("campaign") for x in cfg["streamers"])), _listed
fetch.list_new_vods = _orig_list
# expiry frees a slot
st = common.load_state()
st["clips"]["old"]["created"] = (common.now_utc() - timedelta(hours=50)).isoformat()
gone = clipqueue.expire(cfg, st)
assert [c["id"] for c in gone] == ["old"] and st["clips"]["old"]["status"] == "expired"
assert clipqueue.room(cfg, st) == 1 and jobs.claude_allowed(cfg, st)
# best clip goes first, then the best one made since the last email, never the same one twice
assert clipqueue.pick_for_email(st)["id"] == "b"
common.save_state(st)
jobs.FORCE = True
jobs.email_post(cfg)
st = common.load_state()
assert len(sent) == 1 and sent[0]["subject"] == "Post now: hook b (9/10)", sent[0]["subject"]
assert st["clips"]["b"]["emailed_at"] and st["clips"]["b"]["email_message_id"] == "<mid1@example.com>"
for label in ("TIKTOK CAPTION", "YOUTUBE TITLE", "YOUTUBE DESCRIPTION", "WHAT'S IN IT", "SOURCE",
              "STILL IN THE QUEUE"):
    assert f"=== {label} ===" in sent[0]["text"], label
assert "YOUTUBE TAGS" not in sent[0]["text"]
assert "#shorts" in sent[0]["text"] and "Streamer:" in sent[0]["text"] and "twitch.tv/" in sent[0]["text"]
assert 0 < sent[0]["size"] <= mail.ATTACH_LIMIT and "user-select:all" in sent[0]["html"]
fake_clip("d", 7, 0.1, "brand new moment after the email 💀")   # newer than the last email beats an older 8
st["clips"]["d"]["created"] = common.now_utc().isoformat()
common.save_state(st)
jobs.email_post(cfg)
st = common.load_state()
assert sent[1]["subject"] == "Post now: hook d (7/10)", sent[1]["subject"]
jobs.email_post(cfg)
jobs.email_post(cfg)
st = common.load_state()
subjects = [x["subject"] for x in sent]
assert len(subjects) == len(set(subjects)), subjects
# slots: without FORCE, nothing more goes out once today's emails are done
jobs.FORCE = False
n = len(sent)
due, done = jobs.emails_due(cfg, st, common.now_local(cfg))
jobs.email_post(cfg)
assert len(sent) == n + (1 if done < due and clipqueue.pick_for_email(common.load_state()) else 0)
# a big attachment gets shrunk under the limit
_full = os.path.getsize(real["local_file"])
_lim = max(int(_full * 0.6), 25 * 900 * 1000 // 8 + 600_000)   # still above the 800 kbps floor for a 25 s clip
small = clipqueue.fit_for_email(real["local_file"], TMP / "small.mp4", _lim)
assert os.path.getsize(small) <= _lim, (os.path.getsize(small), _lim, _full)
print("shrink:", _full, "->", os.path.getsize(small), "limit", _lim)
(TMP / "sample_email.txt").write_text(sent[0]["text"])
(TMP / "sample_email.html").write_text(sent[0]["html"])
# TikTok shows the emailed clip -> posted; an unrelated upload matches nothing
st = common.load_state()
tt_uploads[:] = [{"text": "the way he said I don't speak brexit brother 💀 #gtarp #fyp", "duration": st["clips"]["b"]["duration"],
                  "timestamp": common.now_utc().timestamp(), "url": "https://www.tiktok.com/@wantedlevelclips8/video/1"},
                 {"text": "my cat doing a backflip", "timestamp": common.now_utc().timestamp(), "url": "x"}]
notes = clipqueue.refresh_posted(cfg, st)
assert st["clips"]["b"]["status"] == "posted" and st["clips"]["b"]["posts"]["tiktok"]["url"].endswith("/1"), notes
assert st["clips"]["d"]["status"] == "approved"
assert clipqueue.similarity("he crashed into the only tree 😭", "my cat doing a backflip") < 0.6
# replying "skip" to an email drops that clip
replies[:] = [(st["clips"]["d"]["email_message_id"], "skip")]
clipqueue.refresh_posted(cfg, st)
assert st["clips"]["d"]["status"] == "skipped"
replies[:] = []
# a skipped clip doesn't count toward today's emails, so the next run sends a replacement
_today = common.now_local(cfg).date().isoformat()
_n_before = clipqueue.emailed_on(st, _today, cfg.get("timezone", "UTC"))
st["clips"]["d"]["status"] = "approved"
assert clipqueue.emailed_on(st, _today, cfg.get("timezone", "UTC")) == _n_before + 1
st["clips"]["d"]["status"] = "skipped"
assert mail.reply_verdict("Posted!\n\nOn Fri, bot wrote:\n> Post now") == "posted"
assert mail.reply_verdict("skip this one") == "skip"
assert mail.reply_verdict("> posted\nwhat is this") is None
# stored videos of clips that left the queue are deleted
jobs.cleanup_assets(st)
assert st["clips"]["b"]["asset_id"] in deleted and st["clips"]["d"]["asset_id"] in deleted
assert st["clips"]["c"]["asset_id"] not in deleted or st["clips"]["c"]["status"] != "approved"
common.save_state(st)
# daily Claude budget
st = common.load_state()
assert highlights.budget_left(cfg, st) > 0
common.add_spend(float(cfg["limits"]["claude_usd_per_day"]) + 0.25)   # the cap is $1 or $2 depending on the day
assert highlights.budget_left(cfg, st) <= 0 and not jobs.claude_allowed(cfg, st)
common.save_state(st)
st = common.load_state()
assert abs(common.spend_today(st) - float(cfg["limits"]["claude_usd_per_day"]) - 0.25) < 0.01 and not jobs.claude_allowed(cfg, st)
st["spend"] = {}
common.save_state(st)
highlights.record_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000,
                                                              cache_creation_input_tokens=0)))
assert abs(common._SPEND["pending"] - 3.0) < 1e-6
common._SPEND["pending"] = 0.0
# alerts: one email after two failures in a row, not again the same day
n = len(sent)
st = common.load_state()
health.job_failed(st, "post", "RuntimeError: GMAIL login failed")
assert len(sent) == n
health.job_failed(st, "post", "RuntimeError: GMAIL login failed")
assert len(sent) == n + 1 and "post job failed 2 times" in sent[-1]["subject"] and "app password" in sent[-1]["text"]
health.job_failed(st, "post", "RuntimeError: GMAIL login failed")
assert len(sent) == n + 1
health.record(st, "post")
assert st["health"]["post"]["fails"] == 0
# live watcher's email marks reach the main state
os.environ["CLIPPER_LIVE"] = "1"
st = common.load_state()
lv = fake_clip("lv", 9, 0.2, "live moment caption")
lv["live"] = True
common.save_state(st)
os.environ.pop("CLIPPER_LIVE")
st = common.load_state()
common.save_state(st)    # main state now holds a copy of the live clip
os.environ["CLIPPER_LIVE"] = "1"
st = common.load_state()
st["clips"]["lv"]["emailed_at"] = common.now_utc().isoformat()
common.save_state(st)
os.environ.pop("CLIPPER_LIVE")
assert common.load_state()["clips"]["lv"]["emailed_at"]
print("EMAIL QUEUE OK")

# ---------- 10. an empty TikTok account is readable, not an error ----------
import importlib  # noqa: E402
import clipper.clipqueue as _cq  # noqa: E402
_cq = importlib.reload(_cq)
_run = _cq.run
_cq.run = lambda cmd, **kw: (_ for _ in ()).throw(RuntimeError("yt-dlp failed (1): ERROR: [tiktok:user] x: This account does not have any videos posted"))
assert _cq.tiktok_uploads("x") == []
_cq.run = _run
print("TIKTOK EMPTY OK")

# ---------- 11. paid campaign scan: match GTA streamers, email, "join", clip them, claim links ----------
from clipper import campaigns  # noqa: E402
clipqueue.tiktok_uploads = lambda handle, n=15: list(tt_uploads)   # section 10 reloaded the module; fake again
clipqueue.youtube_channel_id = lambda handle: "UC" + "x" * 22
clipqueue.youtube_uploads = lambda cid: []
clipqueue.REPLY_CHECK_MINUTES = 0   # read replies on every check in tests (live: about hourly)
LIST_HTML = """<html><body>
<article><a href="/campaigns/clippedin/big-rp">x</a>Board: Content RewardsFAIR50$2,000 Bigrp Streamer Clipping Campaign
Platforms TikTok, Instagram, YouTubeCLIPPINGCPM$1.50$1.50 per 1K views$1.2K of $2K left</article>
<article><a href="/campaigns/clipster/flower">x</a>Board: Clipster FAIR 43 Flower In A Hurricane [GTA 6 CLIPPING]
Platforms TikTok, Instagram, YouTubeMUSICCPM$1.00$1.00 per 1K views$1.6K of $1.6K left</article>
<article><a href="/campaigns/boxabl/clipping">x</a>Board: Content RewardsLOW10Boxabl Official Clipping
Platforms TikTokCLIPPINGCPM$0.50$0.50 per 1K views$64K of $85K left</article>
<article><a href="/campaigns/clippedin/fortnite-guy">x</a>Board: Content RewardsLOW10$1,000 Fortniteguy Streamer Clipping
Platforms TikTokCLIPPINGCPM$1.00$1.00 per 1K views$500 of $1K left</article>
</body></html>"""
DETAIL = {"/campaigns/clippedin/big-rp": '<a href="https://whop.com/big-rp/">Join</a> THRESHOLDS AND CAPS MIN VIEWS 2K '
          'Clip from twitch.tv/bigrp_live VODs. Use #bigrp in every caption.',
          "/campaigns/clipster/flower": '<a href="https://clippingalpha.com/go/1063378?via=detail">Join on Clipster</a> THRESHOLDS AND CAPS Niche: GTA VI trailer edits',
          "/campaigns/clippedin/fortnite-guy": '<a href="https://whop.com/fg/">Join</a> THRESHOLDS AND CAPS twitch.tv/fortniteguy'}
campaigns._get = lambda url: LIST_HTML if url.endswith("/campaigns") else ("" if "/page/" in url else DETAIL[url.replace(campaigns.BASE, "")])
_orig_la, _orig_ch = twitch.list_archives, twitch.chapters
twitch.list_archives = lambda login, n=10: ([{"id": "v1", "title": "NOPIXEL day 3", "duration": 3600}] if login == "bigrp_live"
                                            else [{"id": "v2", "title": "fortnite arena", "duration": 3600}])
twitch.chapters = lambda vid: [(0.0, None, "Fortnite")]
cards = campaigns.list_campaigns()
assert len(cards) == 4 and cards[0]["rate"] == 1.5 and "Bigrp" in cards[0]["name"], cards[0]
st = common.load_state()
st.pop("campaigns", None); st.pop("campaigns_seen", None)
n = len(sent)
found = campaigns.scan(cfg, st)
names = sorted(f["name"] for f in found)
assert len(found) == 2 and any("Bigrp" in x for x in names) and any("Flower" in x for x in names), names
big = next(f for f in found if "Bigrp" in f["name"])
flower = next(f for f in found if "Flower" in f["name"])
assert big["auto_clip"] and big["twitch_login"] == "bigrp_live" and not flower["auto_clip"]
assert flower["name"] == "Flower In A Hurricane [GTA 6 CLIPPING]", flower["name"]
assert flower["join_url"] == "https://clippingalpha.com/go/1063378?via=detail", flower["join_url"]
assert len(sent) == n + 1 and sent[n]["subject"].startswith("Campaign:") and "Bigrp" in sent[n]["subject"]
assert 'reply "join"' in sent[n]["text"] and "bigrp_live" in sent[n]["text"]
# found campaign streamers are clipped right away (before you join), but don't get priority yet
st0 = dict(st, clips={})
common.save_state(st)
_cfg_found = common.load_config()
_bs = [s for s in _cfg_found["streamers"] if s["channel"] == "bigrp_live"]
assert _bs and _bs[0]["campaign"] and not _bs[0]["campaign_joined"], _bs
assert st["campaigns"][flower["id"]]["status"] == "noted", "edit campaigns are recorded, not emailed"
assert campaigns.scan(cfg, st) == [] and len(sent) == n + 1, "a campaign is only emailed once"
# replying "join" adds the streamer to the ones the bot clips
replies[:] = [(big["email_message_id"], "join")]
clipqueue.refresh_posted(cfg, st)
replies[:] = []
assert st["campaigns"][big["id"]]["status"] == "joined"
common.save_state(st)
cfg2 = common.load_config()
assert any(s["channel"] == "bigrp_live" and s.get("campaign_joined") for s in cfg2["streamers"]), cfg2["streamers"]
# joined campaign streamers are checked first
from clipper import fetch as _f  # noqa: E402
_order = sorted(cfg2["streamers"], key=lambda s: (not s.get("campaign_joined"), not s.get("campaign")))
assert _order[0]["channel"] == "bigrp_live"
assert mail.reply_verdict("join") == "join"
# a clip of that streamer: the email says how to claim, and a claim email goes out once it's posted
st = common.load_state()
cc = fake_clip("camp", 9, 0.5, "bigrp caption for the campaign clip 💀")
cc["streamer"] = "bigrp_live"
pkg = clipqueue.package(cfg2, cc, clipqueue.queue(st), st)
assert pkg["blocks"][0][0] == "Paid campaign" and "whop.com/big-rp" in pkg["blocks"][0][1]
cc["emailed_at"] = (common.now_utc() - timedelta(minutes=5)).isoformat()
cc["email_message_id"] = "<camp@x>"
tt_uploads[:] = [{"text": "bigrp caption for the campaign clip 💀 #bigrp", "timestamp": common.now_utc().timestamp(),
                  "url": "https://www.tiktok.com/@wantedlevelclips8/video/99", "duration": cc["duration"]}]
n = len(sent)
clipqueue.refresh_posted(cfg2, st)
assert cc["status"] == "posted" and len(sent) == n + 1 and sent[-1]["subject"].startswith("Claim:")
assert "video/99" in sent[-1]["text"] and "whop.com/big-rp" in sent[-1]["text"]
clipqueue.refresh_posted(cfg2, st)
assert len(sent) == n + 1, "claim email only once"
tt_uploads[:] = []
# a campaign named only after the streamer ("Benji Ramos") is checked against Twitch, best paying first
_la2 = twitch.list_archives
twitch.list_archives = lambda login, n=10: ([{"id": "v9", "title": "the city | !socials", "duration": 3600}] * 6
                                            if login == "lysium" else _la2(login, n))
twitch.chapters = lambda vid: [(0.0, None, "Grand Theft Auto V")] if vid == "9" else [(0.0, None, "Fortnite")]
_benji = '<article><a href="/campaigns/lysium/benji-ramos">x</a>Board: Content Rewards SOLID 60 Lysium/Benji Ramos Clips Post on TikTok\nPlatforms TikTok, YouTubeCLIPPINGCPM$2.00$2.00 per 1K views$3K of $5K left</article>'
campaigns._get = lambda url: (LIST_HTML.replace("</body>", _benji + "</body>") if url.endswith("/campaigns")
                              else ("" if "/page/" in url else
                                    '<a href="https://clippingalpha.com/go/777">Join</a> THRESHOLDS AND CAPS clip his streams at twitch.tv/lysium'
                                    if "benji" in url else DETAIL[url.replace(campaigns.BASE, "")]))
st = common.load_state()
st.pop("campaigns_recheck_v3", None)
st["campaigns_seen"]["lysium__benji-ramos"] = "2026-10-16"   # rejected by the old filter: checked again once
_n = len(sent)
campaigns.scan(cfg2, st)
_b = st["campaigns"].get("lysium__benji-ramos")
assert _b and _b["twitch_login"] == "lysium" and _b["auto_clip"] and _b["status"] == "notified", _b
assert any("Benji Ramos" in m["subject"] for m in sent[_n:])
assert campaigns._slug_guess({"id": "lysium__benji-ramos"})[0] == "lysium"
# a talk-show campaign named "Harry Jowsey x Shane Parton" isn't the GTA streamer "harry"
assert "harry" not in [g for g in campaigns._slug_guess({"id": "harry-jowsey__boyfriend-material"})]
_card = {"id": "harry-jowsey__boyfriend-material", "name": "Harry Jowsey x Shane Parton, Boyfriend Material Post on TikTok",
         "kind": "CLIPPING"}
_la3 = twitch.list_archives
twitch.list_archives = lambda login, n=10: [{"id": "v9", "title": "GTA RP", "duration": 3600}] * 6 if login == "harry" else []
campaigns.QUICK["left"] = 50
_gq = twitch._gql
twitch._gql = lambda body, timeout=15: {"data": {"user": None}}   # those channels don't exist
assert campaigns.quick_gta_login(_card, "grand theft auto|gta") is None
twitch._gql = _gq
twitch.list_archives = _la3
# out of lookups: the campaign waits for a later scan instead of being marked seen
st["campaigns"].pop("lysium__benji-ramos"); st["campaigns_seen"].pop("lysium__benji-ramos")
cfg2["campaigns"] = {"lookups_per_scan": 0}
campaigns.scan(cfg2, st)
assert "lysium__benji-ramos" not in st["campaigns_seen"]
cfg2.pop("campaigns")
twitch.list_archives, twitch.chapters = _orig_la, _orig_ch
print("CAMPAIGNS OK")

# ---------- 12. the directory's "page past the end" 404 just ends the listing ----------
import requests as _rq  # noqa: E402


def _get404(url):
    if "/page/" in url:
        r = _rq.Response()
        r.status_code = 404
        raise _rq.HTTPError("404", response=r)
    return LIST_HTML


campaigns._get = _get404
assert len(campaigns.list_campaigns()) == 4
print("CAMPAIGN PAGES OK")

# ---------- 13. view counts from TikTok/YouTube feed the weekly report ----------
st = common.load_state()
for cid in ("w1", "w2", "w3", "w4", "w5", "w6", "w7"):
    c = fake_clip(cid, 8, 30, f"caption number {cid} for the weekly test")
    c["emailed_at"] = (common.now_utc() - timedelta(hours=29)).isoformat()
ts0 = (common.now_utc() - timedelta(hours=28)).timestamp()
tt_uploads[:] = [{"text": f"caption number {cid} for the weekly test #gtarp", "timestamp": ts0, "duration": 25.0,
                  "url": f"https://www.tiktok.com/@wantedlevelclips8/video/{i}", "views": 1000 * i, "likes": 10 * i}
                 for i, cid in enumerate(("w1", "w2", "w3", "w4", "w5", "w6", "w7"), 1)]
clipqueue.refresh_posted(cfg, st)
assert st["clips"]["w7"]["posts"]["tiktok"]["views"] == 7000 and st["clips"]["w7"]["posts"]["tiktok"]["hour"]
assert st["clips"]["w1"]["posts"]["tiktok"]["likes"] == 10
tt_uploads[0]["views"] = 1500   # numbers grow on the next check
clipqueue.refresh_posted(cfg, st)
assert st["clips"]["w1"]["posts"]["tiktok"]["views"] == 1500 and len(st["clips"]["w1"]["posts"]["tiktok"]["history"]) == 2
assert clipqueue._vid_key("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
common.save_state(st)
tt_uploads[:] = []
learn_reply = {"playbook": "- more peer-pressure moments", "summary": "Peer pressure clips won.",
               "best_slots": ["12:00"], "target_seconds": [20, 35]}
n = len(sent)
jobs.learn(client=FakeClaude(lambda kw: json.dumps(learn_reply)))
wk = sent[n:]
assert len(wk) == 1 and wk[0]["subject"].startswith("Weekly:") and "Best:" in wk[0]["text"], [x["subject"] for x in wk]
assert "7,000 views" in wk[0]["text"] and "Peer pressure clips won." in wk[0]["text"] and "12:00" in wk[0]["text"]
print("WEEKLY STATS OK")

# ---------- 14. posting-time experiment ----------
from clipper import timing  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
tz = cfg.get("timezone", "UTC")
_cfg_real = cfg   # the rotation and winner tests run without a favorite time, on TikTok numbers
cfg = dict(cfg, posting=dict(cfg["posting"], time_test=dict(cfg["posting"]["time_test"], favorite="", platform="tiktok")))
p = {"posted_at": "2026-10-10T18:00:00+00:00", "history": [["2026-10-10T19:00:00+00:00", 100], ["2026-10-10T22:00:00+00:00", 400]]}
assert abs(timing.views_at(p, 3) - 300) < 1e-6           # interpolated between 1h (100) and 4h (400)
assert timing.views_at(dict(p, history=[["2026-10-10T19:00:00+00:00", 100]]), 3) is None   # too early to tell
assert timing.nearest(["08:00", "10:00", "12:00"], "11:20") == "12:00"
st = {"clips": {}}
d1 = timing.plan(cfg, st, "2026-10-20", 2, tz)
assert len(d1) == 2 and abs(timing._minutes(d1[0]) - timing._minutes(d1[1])) >= 180, d1
assert timing.plan(cfg, st, "2026-10-20", 2, tz) == d1, "same day, same plan"
d2 = timing.plan(cfg, st, "2026-10-21", 2, tz)
assert d2 != d1, "untested times rotate from day to day"
# give every time results: 20:00 is the clear winner
cands = timing.settings(cfg)["candidates"]
for i, c in enumerate(cands):
    for k in range(2):
        h, m = map(int, c.split(":"))
        t0 = _dt(2026, 10, 1 + k, h, m, tzinfo=ZoneInfo(tz)).astimezone(_tz.utc)
        v = 5000 if c == "20:00" else 500 + i * 10
        st["clips"][f"t{i}{k}"] = {"posts": {"tiktok": {"posted_at": t0.isoformat(),
                                                        "history": [[(t0 + timedelta(hours=4)).isoformat(), v]]}}}
res = timing.results(cfg, st, tz)
assert all(len(res[c]) == 2 for c in cands), res
d3 = timing.plan(cfg, st, "2026-10-25", 2, tz)
assert "20:00" in d3 and len(d3) == 2, d3                 # best time + one still testing
assert timing.plan(cfg, st, "2026-10-26", 1, tz) == ["20:00"]
assert timing.report(cfg, st, tz)[0].startswith("- 20:00")
# ramp: two emails a day from day 3
assert any(r["from_day"] == 3 and r["per_day"] == 2 for r in cfg["posting"]["ramp"])
cfg = _cfg_real
# the favorite time (11:00) gets a slot every day; the rest keep testing; results come from YouTube views
assert timing.settings(cfg)["platform"] == "youtube"
for _day in ("2026-11-06", "2026-11-07"):
    _pl = timing.plan(cfg, {"clips": {}}, _day, 3, tz)
    assert "11:00" in _pl and len(_pl) == 3, _pl
assert timing.plan(cfg, {"clips": {}}, "2026-11-06", 1, tz) == ["11:00"]
print("TIMING OK")

# ---------- 15. half-price batch judging: submit, wait, finish on a later run ----------
from clipper import batch  # noqa: E402


class FakeBatches:
    def __init__(self):
        self.reqs, self.status, self.cancelled = {}, "in_progress", []

    def create(self, requests):
        bid = f"msgbatch_{len(self.reqs) + 1}"
        self.reqs[bid] = requests
        return SimpleNamespace(id=bid)

    def retrieve(self, bid):
        return SimpleNamespace(id=bid, processing_status=self.status)

    def cancel(self, bid):
        self.cancelled.append(bid)

    def results(self, bid):
        for r in self.reqs[bid]:
            msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=judge_reply(r["params"]))],
                                  usage=SimpleNamespace(input_tokens=100_000, output_tokens=2_000))
            yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=msg))


bclient = FakeClaude(judge_reply)
bclient.batches = FakeBatches()
cfg_b = dict(cfg2, queue={"max_size": 50, "max_age_hours": 48})
bvod = dict(vod, id="twitch:v555", url="https://twitch.tv/videos/555")
bd = TMP / "work" / "bvod"
bwav = fake_audio(bvod, bd)
wins, bwords, mp = highlights.prepare(cfg_b, bvod, bwav)
assert wins
rec = batch.submit(bclient, bvod, wins, bwords, mp, highlights.judge_params(cfg_b, bvod, wins, mp))
assert batch.pending() and rec["batch_id"] == "msgbatch_1"
st = common.load_state()
before = len(st["clips"])
_spent0 = common.spend_today(st)
jobs.finish_batches(cfg_b, st, client=bclient)          # still running: nothing happens, stays pending
assert batch.pending() and len(st["clips"]) == before
bclient.batches.status = "ended"
jobs.finish_batches(cfg_b, st, client=bclient)          # done: clips made, pending cleared
new = [c for c in st["clips"].values() if c.get("vod") == "twitch:v555"]
assert new and not batch.pending(), (len(new), batch.pending())
_spent = common.spend_today(st) - _spent0
assert 0.10 < _spent < 0.2, _spent   # $0.22 at full price, half in a batch
bclient.batches.status = "in_progress"
rec2 = batch.submit(bclient, dict(bvod, id="twitch:v556"), wins, bwords, mp, highlights.judge_params(cfg_b, bvod, wins, mp))
p2 = batch._path("twitch:v556")
r2 = json.loads(p2.read_text())
r2["submitted"] = (common.now_utc() - timedelta(hours=7)).isoformat()
p2.write_text(json.dumps(r2))
jobs.finish_batches(cfg_b, st, client=bclient)          # stuck too long: cancelled, judged the normal way
assert not batch.pending() and bclient.batches.cancelled == [rec2["batch_id"]]
assert any(c.get("vod") == "twitch:v556" for c in st["clips"].values())
print("BATCH OK")


# ---------- 16. campaign priority, campaign end, reply commands ----------
st = common.load_state()
pc = fake_clip("paid8", 7, 1, "paid streamer caption")
pc["streamer"] = "bigrp_live"
fc = fake_clip("free8", 8, 1, "free streamer caption")
fc["streamer"] = "someoneelse"
ids = [c["id"] for c in clipqueue.queue(st)]
assert ids.index("paid8") < ids.index("free8"), "a joined-campaign 7 ranks with an 8"
# the campaign disappears from the directory: streamer stops being clipped, you get told
campaigns._get = lambda url: ('<article><a href="/campaigns/other/x">x</a>Board: Content Rewards LOW 1 Other thing '
                              'Platforms TikTok CLIPPING $1.00 per 1K views $1K of $1K left</article>'
                              if url.endswith("/campaigns") else "")
n = len(sent)
campaigns.scan(cfg2, st)
assert st["campaigns"][big["id"]]["status"] == "ended" and any("campaign ended" in x["subject"] for x in sent[n:])
common.save_state(st)
assert not any(s["channel"] == "bigrp_live" for s in common.load_config()["streamers"])
# reply commands: pause / resume / next
assert mail.reply_verdict("pause please") == "pause" and mail.reply_verdict("resume") == "resume"
assert mail.reply_verdict("next") == "next"
st = common.load_state()
replies[:] = [("<x@y>", "pause")]
clipqueue.refresh_posted(cfg, st)
assert st["email"]["paused"]
common.save_state(st)
replies[:] = []
n = len(sent)
jobs.FORCE = False
jobs.email_post(cfg)
assert len(sent) == n, "paused: no posting email"
st = common.load_state()
replies[:] = [("<x@y>", "resume"), ("<x@y>", "next")]
clipqueue.refresh_posted(cfg, st)
replies[:] = []
assert not st["email"]["paused"] and st["email"]["send_next"]
common.save_state(st)
_today = common.now_local(cfg).date().isoformat()
_before = clipqueue.emailed_on(common.load_state(), _today, cfg.get("timezone", "UTC"))
jobs.email_post(cfg)
st = common.load_state()
assert len(sent) == n + 1 and sent[-1]["subject"].startswith("Post now"), [x["subject"] for x in sent[n:]]
assert clipqueue.emailed_on(st, _today, cfg.get("timezone", "UTC")) == _before, "an extra clip doesn't use up a slot"
# dashboard renders in email mode without crashing (posts missing times included)
st["clips"]["paid8"]["posts"] = {"tiktok": {"url": "u", "views": 5}}
body = jobs._status_issue(cfg, st)
assert "Queue (" in body and "Today's posting times" in body and "Streamers being clipped" in body
print("PRIORITY + REPLIES OK")

# ---------- 17. below-bar picks don't get the (paid) hook polish ----------
_calls = []
_orig_edit = highlights.edit_hooks
highlights.edit_hooks = lambda cfg, picks, windows, client=None: _calls.append(len(picks)) or picks
_w, _wd = fake_audio(vod, TMP / "work" / "kv"), None
res, _ = highlights.analyze(cfg, vod, _w, client=FakeClaude(judge_reply))
assert all(p["score"] >= cfg["clip"]["min_score"] for p in res) and _calls == [len(res)], (_calls, res)
highlights.edit_hooks = _orig_edit
print("NO WASTED POLISH OK")

# ---------- 18. a Twitch hiccup doesn't make the live watcher think a stream ended ----------
_g = _tw._gql
_tw._gql = lambda body, timeout=15: {"errors": [{"message": "service error"}], "data": {"user": None}}
assert _live.live_info("xqc") == _live.UNKNOWN
# the stream's own recording link is enough (no video-list query needed)
_tw._gql = lambda body, timeout=15: {"data": {"user": {"stream": {"title": "t", "createdAt": "2026-10-17T00:00:00Z",
                                                                  "game": {"name": "Grand Theft Auto V"},
                                                                  "archiveVideo": {"id": "77", "createdAt": "2026-10-17T00:00:05Z"}}}}}
assert _live.live_info("xqc")["vod_id"] == "77"
_tw._gql = lambda body, timeout=15: None
assert _live.live_info("xqc") == _live.UNKNOWN
_tw._gql = lambda body, timeout=15: {"data": {"user": {"stream": None, "videos": {"edges": []}}}}
assert _live.live_info("xqc") is None
def _two(body, timeout=15):   # no archiveVideo: falls back to the newest past broadcast
    if "archiveVideo" in body["query"]:
        return {"data": {"user": {"stream": {"title": "t", "game": {"name": "Grand Theft Auto V"}, "archiveVideo": None,
                                             "createdAt": "2026-10-17T00:00:00Z"}}}}
    return {"data": {"user": {"videos": {"edges": [{"node": {"id": "9", "status": "RECORDING",
                                                             "createdAt": "2026-10-17T00:00:00Z"}}]}}}}


_tw._gql = _two
assert _live.live_info("xqc")["vod_id"] == "9"
_tw._gql = _g
print("LIVE HICCUP OK")

# ---------- 19. heartbeat: starts post every 15 min, process hourly, catches up skipped daily jobs ----------
hb = _live.Heartbeat(cfg)
calls = []
hb.dispatch = lambda wf, job=None: calls.append((wf, job)) or True
t0 = hb.next_post - 60
for minute in range(0, 61):
    hb.tick(t0 + minute * 60)
posts = [c for c in calls if c == ("clipper.yml", "post")]
procs = [c for c in calls if c == ("clipper.yml", "process")]
assert len(posts) == 4 and len(procs) == 1, calls
assert 1 <= calls.count(("live.yml", None)) <= 2, calls   # a spare watcher stays queued
st = common.load_state()
st["last_run"] = {"analytics": (common.now_utc() - timedelta(hours=30)).isoformat()}
common.save_state(st)
calls.clear()
hb._catch_up()
_local_hour = common.now_local(cfg).hour
assert (("clipper.yml", "analytics") in calls) == (_local_hour >= 7), (calls, _local_hour)
calls.clear()
hb._catch_up()   # already started it once: no second dispatch even though the local state still looks stale
assert ("clipper.yml", "analytics") not in calls, calls
hb.hand_over()
assert calls[-1] == ("live.yml", None)
print("HEARTBEAT OK")

# ---------- 20. TikTok upload time read from the video id when the listing has no timestamp ----------
from clipper.clipqueue import upload_time as _ut
assert _ut({"url": "https://www.tiktok.com/@a/video/7697808721518267678"}).isoformat().startswith("2026-10-18T01:09")
assert _ut({"url": "https://www.youtube.com/shorts/appSZJXvGMc"}) is None
print("UPLOAD TIME OK")

# ---------- 21. GTA 6 launch: reminder, Blotato check, more posts, automatic posting with email fallback ----------
from clipper import launch  # noqa: E402
import yaml as _yaml  # noqa: E402
_c = _yaml.safe_load((TMP / "config.yaml").read_text())
_today = common.now_local(_c).date()
_c["launch"].update(enabled=True, date=(_today + timedelta(days=5)).isoformat(), remind_hour=0)
(TMP / "config.yaml").write_text(_yaml.safe_dump(_c, sort_keys=False))
os.environ.pop("BLOTATO_API_KEY", None)
cfg = common.load_config()
assert not cfg["_launch"]["on"] and max(r["per_day"] for r in cfg["posting"]["ramp"]) == 4
st = common.load_state()
st.pop("launch", None)
sent.clear()
launch.daily(cfg, st)
assert len(sent) == 1 and "set up automatic posting" in sent[0]["subject"] and "BLOTATO_API_KEY" in sent[0]["text"], sent
assert "Set up auto-reload" in sent[0]["text"] and "$4 a day" in sent[0]["text"]
launch.daily(cfg, st)
assert len(sent) == 1   # only once
# Elie adds the key: the next check confirms the connections by email
os.environ["BLOTATO_API_KEY"] = "test-key"
services.blotato_accounts = lambda: [{"platform": "tiktok", "id": 1}]
launch.daily(cfg, st)
assert "isn't connected yet" in sent[-1]["subject"] and "youtube" in sent[-1]["text"] and not st["launch"].get("blotato_ok")
st["launch"]["blotato_checked"] = (common.now_utc() - timedelta(hours=21)).isoformat()
services.blotato_accounts = lambda: [{"platform": "tiktok", "id": 1}, {"platform": "youtube", "id": 2}]
launch.daily(cfg, st)
assert sent[-1]["subject"] == "Clip bot: automatic posting is ready" and st["launch"]["blotato_ok"]
common.save_state(st)
# a reply moves the date
assert mail.reply_verdict("launch 2026-12-3") == "launch:2026-12-03"
# launch day: x3 with automatic posting, applying twice doesn't multiply twice, hourly posting times
_c["launch"]["date"] = _today.isoformat()
(TMP / "config.yaml").write_text(_yaml.safe_dump(_c, sort_keys=False))
cfg = common.load_config()
assert cfg["_launch"] == dict(cfg["_launch"], on=True, auto=True, multiplier=3), cfg["_launch"]
launch.apply(cfg, common.load_state())
assert max(r["per_day"] for r in cfg["posting"]["ramp"]) == 12 and cfg["queue"]["max_size"] == 15
assert len(timing.plan(cfg, {"clips": {}}, _today.isoformat(), 12, cfg["timezone"])) == 12
# with results, launch days use only the best times: 18:00 did best, so the hours around it come first
_ztz = ZoneInfo(cfg["timezone"])
def _post_at(hh, views):
    t0 = _dt(2026, 10, 20, hh, 5, tzinfo=_ztz).astimezone(_tz.utc)
    hist = [[(t0 + timedelta(hours=4)).isoformat(), views]]
    return {"posts": {timing.settings(cfg)["platform"]: {"posted_at": t0.isoformat(), "history": hist, "views": views,
                                                          "likes": views // 20}}}
_st = {"clips": {f"c{i}": _post_at(hh, v) for i, (hh, v) in enumerate([(18, 900), (18, 1100), (10, 200), (10, 300),
                                                                       (14, 500), (14, 400)])}}
_best = timing.plan(cfg, _st, "2030-01-01", 3, cfg["timezone"])
assert _best == ["17:00", "18:00", "19:00"], _best
assert _st["slot_plan_mode"]["2030-01-01"] == "best"
# Claude budget: $1, $2 from 3 posts a day, $4 from launch day
assert cfg["limits"]["claude_usd_per_day"] == 4.0, cfg["limits"]
_c2 = dict(_c, launch=dict(_c["launch"], enabled=False))
assert launch.daily_cap(dict(_c2, start_date=_today.isoformat())) == 1.0
assert launch.daily_cap(dict(_c2, start_date=(_today - timedelta(days=8)).isoformat())) == 2.0
os.environ.pop("BLOTATO_API_KEY")
assert common.load_config()["_launch"]["multiplier"] == 2   # no key: still by email, doubled
os.environ["BLOTATO_API_KEY"] = "test-key"
sent.clear()
launch.daily(cfg, st)
assert sent and "launch mode is on" in sent[-1]["subject"]
# a slot's clip is posted through Blotato; if Blotato fails, it's emailed instead
st = common.load_state()
_clip = dict(next(iter(st["clips"].values())), id="launchclip", status="approved", posts={}, emailed_at=None,
             created=common.now_utc().isoformat(), local_file=str(next(iter(rendered))), media_url="https://gh/dl/x.mp4")
st["clips"]["launchclip"] = _clip
posted.clear()
jobs.deliver(cfg, st, _clip)
assert {p for p, *_ in posted} == {"tiktok", "youtube"} and _clip["status"] == "posted" and _clip["emailed_at"]
_clip2 = dict(_clip, id="launchclip2", status="approved", posts={}, emailed_at=None)
st["clips"]["launchclip2"] = _clip2
_bp = services.blotato_post
services.blotato_post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blotato down"))
sent.clear()
jobs.deliver(cfg, st, _clip2)
assert sent and _clip2["emailed_at"] and _clip2["status"] == "approved"   # emailed instead
services.blotato_post = _bp
# the live watcher also picks the biggest English GTA 6 streams that are recording
def _top(q):
    edges = [{"node": {"viewersCount": v, "broadcaster": {"login": l, "broadcastSettings": {"language": lang},
              "videos": {"edges": [{"node": {"status": s}}]}}}}
             for l, v, lang, s in [("bigES", 9e5, "ES", "RECORDING"), ("xqc", 8e5, "EN", "RECORDING"),
                                   ("novod", 7e5, "EN", "RECORDED"), ("kai", 6e5, "EN", "RECORDING"),
                                   ("jynxzi", 5e5, "EN", "RECORDING")]]
    return {"data": {"game": {"streams": {"edges": edges}}}}
_tw._gql = _top
assert launch.top_streams(cfg, n=2, exclude={"xqc"}) == ["kai", "jynxzi"]
# launch day re-picks the streamers: paid campaign streamers first (best rate), then the biggest GTA 6 streams
st = common.load_state()
st["campaigns"] = {"a": {"id": "a", "name": "Low RP", "status": "notified", "twitch_login": "lowrp", "rate": 0.5,
                         "auto_clip": True},
                   "b": {"id": "b", "name": "Big GTA 6 Clips", "status": "found", "gta6": True, "rate": 3.0},
                   "c": {"id": "c", "name": "Top RP", "status": "joined", "twitch_login": "toprp", "rate": 1.0,
                         "auto_clip": True}}
st.pop("launch", None)
sent.clear()
picked = launch.refresh_streamers(cfg, st)
assert [x["channel"] for x in picked][:2] == ["toprp", "lowrp"] and "kai" in [x["channel"] for x in picked], picked
assert "xqc" not in [x["channel"] for x in picked]   # already one of your streamers
assert len(sent) == 1 and "launch day" in sent[0]["subject"] and sent[0]["text"].index("Big GTA 6") < sent[0]["text"].index("Top RP")
assert launch.refresh_streamers(cfg, st) == picked and len(sent) == 1   # once a day, one digest
common.save_state(st)
assert "kai" in [x["channel"] for x in common.load_config()["streamers"]]
_tw._gql = _g
print("LAUNCH OK")

# ---------- 22. Gmail refuses the login: the bot backs off instead of retrying every run ----------
import smtplib as _smtplib  # noqa: E402
mail.send = _orig_mail_send
_SS = _smtplib.SMTP_SSL
class _Refuse:
    def __init__(self, *a, **k):
        raise _smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
_smtplib.SMTP_SSL = _Refuse
try:
    mail.send("x", "y")
    raise AssertionError("should have failed")
except mail.MailDown:
    pass
assert mail.down_until()
_tries = []
class _Count(_Refuse):
    def __init__(self, *a, **k):
        _tries.append(1)
        super().__init__()
_smtplib.SMTP_SSL = _Count
for _ in range(3):
    try:
        mail.send("x", "y")
    except mail.MailDown:
        pass
assert not _tries, "no login attempts while backing off"
st = common.load_state()
_c3 = dict(_clip, id="mailclip", status="approved", posts={}, emailed_at=None)
st["clips"]["mailclip"] = _c3
os.environ.pop("BLOTATO_API_KEY", None)
jobs.deliver(common.load_config(), st, _c3)   # doesn't raise; the clip waits in the queue
assert _c3["status"] == "approved" and not _c3.get("emailed_at")
_smtplib.SMTP_SSL = _SS
mail.BACKOFF.unlink()
print("GMAIL BACKOFF OK")

# ---------- 23. a streamer whose last 3 TikTok posts stalled is ranked lower ----------
_now = common.now_utc()
_q = {"clips": {}}
for i in range(3):
    _q["clips"][f"x{i}"] = {"streamer": "xQc", "status": "posted", "score": 7, "created": _now.isoformat(),
                            "posts": {"tiktok": {"posted_at": (_now - timedelta(hours=10 + i)).isoformat(), "views": i}}}
_q["clips"]["new_x"] = {"streamer": "xQc", "status": "approved", "score": 8, "created": _now.isoformat(), "posts": {}}
_q["clips"]["new_c"] = {"streamer": "Cinna", "status": "approved", "score": 7, "created": _now.isoformat(), "posts": {}}
assert clipqueue.dead_on_tiktok(_q) == {"xqc"}
assert [c["streamer"] for c in clipqueue.queue(_q)] == ["Cinna", "xQc"]
_q["clips"]["x2"]["posts"]["tiktok"]["views"] = 400   # one did well again: back to normal
assert not clipqueue.dead_on_tiktok(_q) and clipqueue.queue(_q)[0]["streamer"] == "xQc"
print("TIKTOK STALL OK")

# ---------- 24. clips play 1.3x faster with the voice at the same pitch; old clips are sped up before sending ----------
from clipper import render as _render  # noqa: E402
_cfgs = common.load_config()
assert jobs.clip_speed(_cfgs) == 1.3
_made = [c for c in common.load_state()["clips"].values() if c.get("speed") == 1.3 and c.get("local_file")
         and os.path.exists(c["local_file"])]
assert _made, "new clips are made at 1.3x"
_c = _made[0]
_dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                             _c["local_file"]], capture_output=True, text=True).stdout)
assert abs(_dur - (_c["end"] - _c["start"]) / 1.3) < 0.6, (_dur, _c["end"] - _c["start"])
_tone = TMP / "tone.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-c:a", "aac", "-shortest",
                str(_tone)], check=True)
_fast = _render.speed_up(_tone, TMP / "tone_fast.mp4", 1.3)
_pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(_fast), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                      capture_output=True).stdout
_x = np.frombuffer(_pcm, dtype=np.int16).astype(float)
_peak = np.argmax(np.abs(np.fft.rfft(_x))) * 16000 / len(_x)
assert abs(_peak - 440) < 15, _peak   # same pitch
assert abs(len(_x) / 16000 - 4 / 1.3) < 0.2
# an older 1.0x clip in the queue is sped up (and its stored copy replaced) before it's emailed
st = common.load_state()
_old = dict(_c, id="slowclip", speed=None, status="approved", emailed_at=None, posts={}, local_file=str(_tone),
            duration=4.0, asset_id=555, created=common.now_utc().isoformat())
st["clips"]["slowclip"] = _old
_sentn = len(sent)
mail.send = fake_send
deleted.clear()
jobs.send_package(_cfgs, st, _old)
assert _old["speed"] == 1.3 and abs(_old["duration"] - 3.1) < 0.1 and 555 in deleted and "_x1.3" in _old["media_url"]
assert len(sent) == _sentn + 1
print("SPEED OK")

# ---------- 25. TikTok's days off: YouTube-only emails, back on by itself ----------
_cp = dict(_cfgs, posting=dict(_cfgs["posting"], tiktok_paused_until=(common.now_local(_cfgs).date() + timedelta(days=2)).isoformat()))
assert clipqueue.tiktok_paused(_cp)
_pkg = clipqueue.package(_cp, dict(_c, duration=30.0), [], {})
assert _pkg["subject"].startswith("Post now (YouTube):") and not any(b[0] == "TikTok caption" for b in _pkg["blocks"])
assert _pkg["blocks"][0][0] == "YouTube only"
_cp["posting"]["tiktok_paused_until"] = common.now_local(_cfgs).date().isoformat()   # the day it ends
assert not clipqueue.tiktok_paused(_cp)
assert any(b[0] == "TikTok caption" for b in clipqueue.package(_cp, dict(_c, duration=30.0), [], {})["blocks"])
print("TIKTOK PAUSE OK")

# ---------- 26. the "no email for 36h" alert ignores clips that only just arrived ----------
_alerts = []
_ha = health.alert
health.alert = lambda st, key, subj, text: _alerts.append(key)
_h = {"clips": {"q": {"status": "approved", "score": 7, "created": common.now_utc().isoformat(), "streamer": "x"},
                "e": {"status": "posted", "score": 7, "created": "2026-01-01T00:00:00+00:00", "streamer": "x",
                      "emailed_at": (common.now_utc() - timedelta(hours=50)).isoformat()}}}
health.check_quiet(cfg, _h, 1)
assert not _alerts, "a clip made an hour ago isn't a stuck queue"
_h["clips"]["q"]["created"] = (common.now_utc() - timedelta(hours=10)).isoformat()
health.check_quiet(cfg, _h, 1)
assert _alerts == ["quiet"]
health.alert = _ha
print("QUIET ALERT OK")

# ---------- 27. weekly long video: most-viewed clips of the week, chat replay, hype meter, chapters, email ----------
from clipper import weekly  # noqa: E402
import random as _rnd  # noqa: E402
_wclips = [{"slug": f"s{i}", "title": t, "views": v, "duration": d, "created": "", "login": l, "name": n, "lang": g,
            "vid": "123", "offset": 1000 + i * 500} for i, (t, v, d, l, n, g) in enumerate([
    ("bro got pulled over for the 5th time 💀", 412000, 12, "xqc", "xQc", "EN"),
    ("CINNA HEIST GONE WRONG", 233000, 11, "cinna", "Cinna", "EN"),
    ("same moment again", 150000, 11, "cinna", "Cinna", "EN"),          # same moment as #2: skipped
    ("clip en español", 120000, 15, "auronplay", "auronplay", "ES"),     # not English: skipped
    ("he really said that to a cop", 98000, 10, "vanillamace", "vanillamace", "EN"),
    ("too short", 90000, 5, "kai", "Kai", "EN")])]
_wclips[2]["offset"] = _wclips[1]["offset"] + 20
_picked = weekly.pick(list(_wclips), dict(weekly.DEFAULTS, target_minutes=10))
assert [c["login"] for c in sorted(_picked, key=lambda c: c["rank"])] == ["xqc", "cinna", "vanillamace"], _picked
assert [c["rank"] for c in _picked] == [3, 2, 1]   # counted down to #1
weekly.top_clips = lambda games, n=60: [dict(c) for c in _wclips]
_wrun = weekly.run
def _fake_ytdlp(cmd, **kw):
    if cmd[0] == "yt-dlp":
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=13",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=13", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", cmd[cmd.index("-o") + 1]], check=True)
        return ""
    return _wrun(cmd, **kw)
weekly.run = _fake_ytdlp
def _fake_chat(vid, start, stop, max_pages=300):
    out, t = [], start
    while t < stop:
        t += _rnd.uniform(0.05, 0.2) if t > start + 92 else _rnd.uniform(0.8, 1.6)
        out.append((t, "viewer", "#1E90FF", [(_rnd.choice(["KEKW", "NO WAY", "he really did that"]), None)]))
    return out
weekly.chat_window = _fake_chat
_wc = _yaml.safe_load((TMP / "config.yaml").read_text())
_wc["launch"]["enabled"] = False   # section 21 turned launch day on; the GTA 6 series is checked at the end
(TMP / "config.yaml").write_text(_yaml.safe_dump(_wc, sort_keys=False))
_wcfg = common.load_config()
import clipper.vision as _vis  # noqa: E402
_vis.analyze_frames = lambda cfg, src, off, dur, d: {"facecam": {"x": 0.7, "y": 0.05, "w": 0.25, "h": 0.25},
                                                     "layout": "wide", "focus_x": 0.5, "why": "test"}
if weekly.STATE.exists():
    weekly.STATE.unlink()
_ep = weekly.build(_wcfg, TMP / "weekly_out", ff="ffmpeg", test=True)
_info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                                   "-of", "json", str(_ep["video"])], capture_output=True, text=True).stdout)
assert _info["streams"][0]["width"] == 1920 and _info["streams"][0]["height"] == 1080
assert abs(float(_info["format"]["duration"]) - _ep["seconds"]) < 1.0, (_info, _ep["seconds"])
assert _ep["title"] == "GTA RP MOST VIEWED Twitch Clips of The Week! #1"
_lines = _ep["description"].splitlines()
assert "0:00 #3 vanillamace: he really said that to a cop" in _lines and any(l.startswith("0:") and "#1 xQc" in l for l in _lines)
assert len(_ep["thumbs"]) == 3 and all(Path(x).exists() for x in _ep["thumbs"]) and len(_ep["titles"]) == 3
# the cold open is the #1 clip: its first frames come from the #1 clip, not a title card
assert _ep["seconds"] > 5 + 2 + 12
assert "xQc: https://twitch.tv/xqc" in _lines
_st = weekly.stats(_fake_chat("1", 900, 1030), 1000, 12)
assert _st["peak"] > 3 and _st["top_word"] and _st["have_chat"]
# the real job (not a preview): uploads, emails the link with the thumbnail, counts the episode
weekly.build = (lambda _b: (lambda cfg, out, test=False, ff=None: _b(cfg, out, test=True, ff="ffmpeg")))(weekly.build)
os.environ.pop("TEST_SOURCE", None)
os.environ["FORCE"] = "1"
sent.clear()
mail.send = lambda subject, text, html=None, attachment=None, attachment_name=None, maintype="video", subtype="mp4": \
    sent.append({"subject": subject, "text": text, "attach": attachment, "type": maintype}) or "<w@x>"
weekly.job()
assert sent[-1]["subject"].startswith("Weekly video ready: GTA RP MOST VIEWED") and sent[-1]["type"] == "image"
assert len(sent[-1]["attach"]) == 3 and "A/B Testing" in sent[-1]["text"] and "=== TITLE 3 ===" in sent[-1]["text"]
assert "studio.youtube.com" in sent[-1]["text"] and "=== DESCRIPTION ===" in sent[-1]["text"]
assert weekly.load()["series"]["GTA RP"]["episode"] == 1
weekly.job()
assert weekly.load()["series"]["GTA RP"]["episode"] == 2   # numbered series
# due on Sunday morning, once a week
from datetime import datetime as _d2  # noqa: E402
_sun = _d2(2026, 11, 1, 18, 0, tzinfo=_tz.utc)   # Sunday 10am Pacific
assert weekly.due(_wcfg, {}, _sun) and not weekly.due(_wcfg, {"last_made": "2026-11-01T17:30:00+00:00"}, _sun)
assert not weekly.due(_wcfg, {}, _sun - timedelta(days=1))
assert not weekly.due(_wcfg, {"fail": {"date": "2026-11-01", "n": 3}}, _sun)
_wc["launch"].update(enabled=True, date=common.now_local(_wcfg).date().isoformat())
(TMP / "config.yaml").write_text(_yaml.safe_dump(_wc, sort_keys=False))
assert weekly.series(common.load_config())[0] == "GTA 6"   # from launch day the series is GTA 6, from #1
weekly.run = _wrun
print("WEEKLY OK")

# ---------- 28. campaign rules per streamer (Lysium): context line, handle on screen, title rule, pre-approval ----------
from clipper import render as _r2  # noqa: E402
_ass = _r2.build_ass({"start": 0, "end": 10, "commentary": [{"at": 0.0, "text": "Benji owes the mechanic 40K"}]}, [],
                     TMP / "lys.ass", commentary=True, handle="twitch.tv/Lysium")
_txt = Path(_ass).read_text()
assert "twitch.tv/Lysium" in _txt and "Benji owes the mechanic 40K" in _txt
_lys = next(x for x in common.load_config()["streamers"] if x["channel"] == "lysium")
assert _lys["preapproval"] and not _lys.get("context") and _lys["handle"] == "twitch.tv/Lysium"
assert _re.search(_lys["title_match"], "BENJI RAMOS WHO IS YOUR BIA", _re.I) and not _re.search(_lys["title_match"], "Fortnite w/ the boys", _re.I)
_jp = highlights.judge_prompt(_cfgs, {"streamer": _lys, "title": "x"}, [{"id": 0, "start": 0, "end": 60, "text": "hi"}])
assert "No drama baiting" in _jp and "Give context through the hook" in _jp
_pc = dict(_c, id="lysclip", streamer="Lysium", handle="twitch.tv/Lysium", preapproval=True, duration=30.0)
_pk = clipqueue.package(dict(_cfgs, posting=dict(_cfgs["posting"], tiktok_paused_until="")), _pc, [], {})
assert _pk["blocks"][0][0] == "Campaign approval first" and "twitch.tv/Lysium" in _pk["tiktok_caption"]
# waiting for approval: not expired after 2 days, and never auto-posted
_old = common.now_utc() - timedelta(hours=60)
_q = {"clips": {"w": dict(_pc, status="approved", score=7, created=_old.isoformat(), emailed_at=_old.isoformat())}}
assert not clipqueue.expire(_cfgs, _q) and _q["clips"]["w"]["status"] == "approved"
print("CAMPAIGN RULES OK")

# ---------- 29. a joined campaign's clip always goes first, even against a higher score or a newer clip ----------
_now = common.now_utc()
_pq = {"campaigns": {"l": {"status": "joined", "twitch_login": "lysium"}},
       "clips": {"x": {"streamer": "xQc", "status": "approved", "score": 10, "created": _now.isoformat(), "posts": {}},
                 "l": {"streamer": "Lysium", "status": "approved", "score": 7,
                       "created": (_now - timedelta(hours=5)).isoformat(), "posts": {}}},
       "email": {}}
assert [c["streamer"] for c in clipqueue.queue(_pq)] == ["Lysium", "xQc"]
_pq["clips"]["e"] = {"streamer": "Cinna", "status": "posted", "score": 7, "posts": {},
                     "created": (_now - timedelta(hours=8)).isoformat(), "emailed_at": (_now - timedelta(hours=3)).isoformat()}
assert clipqueue.pick_for_email(_pq)["streamer"] == "Lysium"   # older than the xQc clip, still first
print("CAMPAIGN FIRST OK")

# ---------- 30. Mac side: Content Rewards page reading and the Telegram messages it produces ----------
from clipper import contentrewards as cr, mac  # noqa: E402

_list = "Your\nCampaigns\nwantedlevelclips1\nApproved\nLysium\n·\n1mo ago\nLysium/Benji Ramos Clips\n$0\n/$1,000\n0\n$1.50/1K\nSubmit clip"
assert cr.parse_campaign_list(_list) == [{"status": "approved", "creator": "Lysium", "name": "Lysium/Benji Ramos Clips"}]
_page = """Back
wantedlevelclips1
Lysium
Lysium/Benji Ramos Clips
Pushing clips of Lysium. All clips require pre-approval before posting. Campaign ended in drama is not allowed.
Joined
Launched 1 month ago
Submit clip
Application approved
You can now submit content for this campaign.
Budget
$150 / $1,000
Content requirements
Add link in bio
twitch.tv/Lysium
Drafts require pre-approval before posting
Top clippers
Rank"""
_info = cr.parse_campaign_page(_page)
assert _info["joined"] and _info["approved"] and _info["preapproval"] and not _info["ended"], _info
assert _info["bio"] == ["twitch.tv/Lysium"] and _info["budget_used"] == 150 and _info["budget_total"] == 1000, _info
assert cr.parse_campaign_page(_page.replace("$150 / $1,000", "$1,000 / $1,000"))["ended"]
assert cr.parse_campaign_page(_page.replace("Joined\n", "Ended\n"))["ended"]
assert cr.parse_draft_counts("Your drafts\nwantedlevelclips1\nNeeds changes\n1\nPending\n1\nApproved\n0\nPosted\n2\nRejected\n0") == \
    {"needs changes": 1, "pending": 1, "approved": 0, "posted": 2, "rejected": 0}
_tl = cr.parse_timeline("Timeline\nRevision 1\nthorsty\nNov 3, 6:48 AM\n00:03\nremove the line\nBrand reviewer\n"
                        "Nov 3, 6:48 AM\nRequested changes\nremove the benji brags line, it was mickey\nLeave a comment...\nUpload revision")
assert _tl == {"who": "Brand reviewer", "when": "Nov 3, 6:48 AM", "note": "remove the benji brags line, it was mickey"}, _tl

_st = {}
_camp = dict(_info, name="Lysium/Benji Ramos Clips", status="approved", url="https://contentrewards.com/c/campaigns/x")
_msgs = mac.compare(_st, [_camp], {"counts": {"approved": 0, "rejected": 0, "posted": 2}}, ["Lysium/Benji Ramos Clips"])
assert len(_msgs) == 1 and "approved for Lysium" in _msgs[0] and "twitch.tv/Lysium" in _msgs[0], _msgs
assert mac.compare(_st, [_camp], {"counts": {"approved": 0, "rejected": 0, "posted": 2}}, ["Lysium/Benji Ramos Clips"]) == []
_msgs = mac.compare(_st, [_camp], {"counts": {"approved": 1, "rejected": 0, "posted": 2},
                                   "needs_changes": [dict(_tl, campaign="Lysium/Benji Ramos Clips", card="x")]},
                    ["Lysium/Benji Ramos Clips"])
assert any("1 draft approved" in m for m in _msgs) and any("mickey" in m for m in _msgs), _msgs
assert not any("mickey" in m for m in mac.compare(_st, [_camp], {"counts": {"approved": 1},
           "needs_changes": [dict(_tl, campaign="Lysium/Benji Ramos Clips", card="x")]}, ["Lysium/Benji Ramos Clips"]))
_st2 = {}
_msgs = mac.compare(_st2, [_camp], {"counts": {}}, ["xQc"])   # joined on the site but not prioritized by the clipper
assert any("isn't putting its streamer's clips first" in m for m in _msgs), _msgs
_msgs = mac.compare(_st, [], {"counts": {"approved": 1}}, [])   # dropped off the list: ended, bio can come out
assert len(_msgs) == 1 and "dropped off" in _msgs[0] and "twitch.tv/Lysium" in _msgs[0], _msgs
assert mac.same_campaign("Lysium/Benji Ramos Clips", "lysium benji ramos clips") and not mac.same_campaign("a", "")
assert mac.awake(__import__("datetime").datetime(2026, 11, 4, 9)) and not mac.awake(__import__("datetime").datetime(2026, 11, 4, 3))
os.environ.pop("TELEGRAM_TOKEN", None)
from clipper import tg  # noqa: E402
_real_info = tg.info
tg.info = lambda text, rows=None, silent=True: tg.send(text, rows, silent=silent)   # tests see progress notes too
assert not tg.configured() and tg.send("hi") is None and tg.poll() == []
assert json.loads(tg.buttons([[("Join", "camp:join:" + "x" * 80)]]))["inline_keyboard"][0][0]["callback_data"] == "camp:join:" + "x" * 54
print("MAC OK")

# ---------- 31. Relay: the GitHub bot's emails, links and problems reach Telegram exactly once ----------
from clipper import relay  # noqa: E402
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402
_now = _dt(2026, 11, 4, 18, 0, tzinfo=_tz.utc)
_rcfg = yaml_cfg = __import__("yaml").safe_load((REPO / "config.yaml").read_text())
_rstate = {"clips": {
    "old": {"id": "old", "streamer": "xQc", "hook": "old one", "score": 7, "emailed_at": (_now - _td(days=1)).isoformat(),
            "posts": {"tiktok": {"url": "https://tiktok.com/@w/video/1"}}, "status": "posted"},
}, "campaigns": {"c1": {"name": "Old campaign", "status": "notified", "rate": 1.0}}}
_rst = {}
assert relay.relay(_rst, _rcfg, _rstate, {}, _now.isoformat(), now=_now) == []   # first run: nothing old re-sent
_clip = {"id": "new1", "streamer": "Lysium", "hook": "he started a boy band 💀", "score": 8, "title": "boy band moment",
         "caption": "the boy band reveal 😭", "duration": 29.0, "start": 4868, "vod_url": "https://www.twitch.tv/videos/1",
         "why": "tense meeting derailed", "status": "approved", "posts": {}, "created": _now.isoformat(),
         "emailed_at": (_now + _td(minutes=1)).isoformat(), "media_url": "https://example.invalid/new1.mp4",
         "handle": "twitch.tv/Lysium", "preapproval": True}
_rstate["clips"]["new1"] = _clip
_rstate["campaigns"]["c2"] = {"name": "Benji Ramos clips", "status": "notified", "rate": 1.5, "why": "GTA RP",
                              "join_url": "https://example.invalid/join"}
_items = relay.relay(_rst, _rcfg, _rstate, {}, _now.isoformat(), now=_now + _td(minutes=3))
_kinds = sorted(i[0] for i in _items)
assert _kinds == ["post", "text"], _items
_post = [i for i in _items if i[0] == "post"][0]
assert "boy band" in _post[2] and [f[0] for f in _post[4]] == ["TikTok caption", "YouTube title", "YouTube description"]
assert "TIKTOK CAPTION" not in _post[3] and "Next messages" in _post[3], _post[3]   # fields aren't in the info message
assert all("▸" not in f[1] and "===" not in f[1] for f in _post[4])            # each field is only the text to paste
assert "twitch.tv/Lysium" in _post[4][0][1] and _post[4][1][1].endswith("#shorts")
assert 'Reply "posted" here' not in _post[3] and "Still in the queue".upper() not in _post[3]
assert "Benji Ramos clips" in [i for i in _items if i[0] == "text"][0][1]
assert relay.relay(_rst, _rcfg, _rstate, {}, _now.isoformat(), now=_now + _td(minutes=6)) == []   # nothing twice
_clip["posts"]["tiktok"] = {"url": "https://www.tiktok.com/@wantedlevelclips8/video/99"}
_items = relay.relay(_rst, _rcfg, _rstate, {}, _now.isoformat(), now=_now + _td(minutes=9))
assert len(_items) == 1 and "video/99" in _items[0][1] and "Lysium/Benji Ramos Clips" in _items[0][1], _items
_rstate["clips"]["x"] = {"id": "x", "streamer": "Cinna", "hook": "h", "posts": {"youtube": {"url": "https://y/1"}}}
assert relay.relay(_rst, _rcfg, _rstate, {}, _now.isoformat(), now=_now + _td(minutes=12)) == []   # no campaign: no link
_rstate["health"] = {"process": {"fails": 2, "last_error": "AuthenticationError (anthropic)", "last_fail": "2026-11-04T17"}}
_p = relay.relay(_rst, _rcfg, _rstate, {"until": "2099-01-01", "error": "535"}, (_now - _td(hours=4)).isoformat(),
                 now=_now + _td(minutes=15))
assert sorted(i[1].split(":")[0] for i in _p) == ["fail", "gmail", "stalled"], _p
assert "console.anthropic.com" in [i for i in _p if i[1].startswith("fail")][0][3]
assert not [i for i in relay.relay(_rst, _rcfg, dict(_rstate, health={}), {}, (_now - _td(hours=2)).isoformat(),
                                   now=_now + _td(minutes=18)) if i[0] == "problem"]   # 2h quiet isn't "stopped"
assert all(len(c) <= 3900 for c in relay._chunks("x" * 3000 + "\n\n" + "y" * 3000)) and len(relay._chunks("a\n\nb")) == 1
_ps = {}
os.environ.pop("TELEGRAM_TOKEN", None)
relay.send(_ps, [("problem", "k", "w", "f")])   # Telegram off: no crash
print("RELAY OK")

# ---------- 32. The Mac updates itself from GitHub (fast-forward only, restarts on new code) ----------
_g = TMP / "upd"
_g.mkdir()
def _sh(*a, cwd=_g):  # noqa: E302
    return subprocess.run(list(a), cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
_sh("git", "init", "-q", "--bare", "-b", "mac-server", "origin.git")
_sh("git", "clone", "-q", str(_g / "origin.git"), "work")
_w = _g / "work"
for _k, _v in (("user.email", "t@t"), ("user.name", "t")):
    _sh("git", "config", _k, _v, cwd=_w)
_sh("git", "checkout", "-q", "-b", "mac-server", cwd=_w)
(_w / "requirements.txt").write_text("requests\n")
_sh("git", "add", "-A", cwd=_w); _sh("git", "commit", "-qm", "one", cwd=_w); _sh("git", "push", "-q", "origin", "mac-server", cwd=_w)  # noqa: E702
_sh("git", "clone", "-q", "-b", "mac-server", str(_g / "origin.git"), "mac")
_old_root = mac.ROOT
mac.ROOT = _g / "mac"
assert mac.self_update() is False   # nothing new
(_w / "new.txt").write_text("v2")
_sh("git", "add", "-A", cwd=_w); _sh("git", "commit", "-qm", "two", cwd=_w); _sh("git", "push", "-q", "origin", "mac-server", cwd=_w)  # noqa: E702
assert mac.self_update() is True and (_g / "mac" / "new.txt").read_text() == "v2"
assert mac.self_update() is False
mac.ROOT = _old_root
print("SELF UPDATE OK")

# ---------- 33. Jellyfin clean-up: watched 3+ days ago is deleted, warned the day before, favorites kept ----------
from clipper import mediaclean  # noqa: E402
_n = _dt.now(_tz.utc)   # run() uses the real clock
assert mediaclean._when("2026-11-04T10:22:33.1234567Z") == _dt(2026, 11, 4, 10, 22, 33, 123456, tzinfo=_tz.utc)
assert mediaclean._when("2026-11-04T10:22:33Z").hour == 10 and mediaclean._when("2026-11-04T10:22:33.5+00:00").microsecond == 500000
def _it(i, days, **kw):  # noqa: E302
    return dict({"Id": i, "Type": "Movie", "Name": i, "ProductionYear": 1925,
                 "UserData": {"Played": True, "LastPlayedDate": (_n - _td(days=days)).isoformat().replace("+00:00", "Z")}}, **kw)
_items = [_it("old", 4), _it("warnme", 2.5), _it("fresh", 1), _it("fav", 9),
          _it("ep", 5, Type="Episode", SeriesId="S1", SeriesName="Show", ParentIndexNumber=1, IndexNumber=2),
          _it("ep2", 5, Type="Episode", SeriesId="S2", SeriesName="Other", ParentIndexNumber=1, IndexNumber=3)]
_items[3]["UserData"]["IsFavorite"] = True
_d, _w = mediaclean.plan(_items, {"S1"}, now=_n, days=3)
assert [x["Id"] for x in _d] == ["old", "ep2"] and [x["Id"] for x in _w] == ["warnme"], (_d, _w)
assert mediaclean._label(_items[5]) == "Other S01E03" and mediaclean._label(_items[0]) == "old (1925)"
_calls, _sent = [], []
def _fake_req(method, path, **kw):  # noqa: E302
    _calls.append((method, path))
    if path == "/Users":
        return [{"Id": "u1", "Name": "elie", "Policy": {"IsAdministrator": True}}]
    if path.endswith("/Items") and "IsFavorite" in str(kw.get("params")):
        return {"Items": [{"Id": "S1"}]}
    if path.endswith("/Items"):
        return {"Items": _items}
    return None
mediaclean._req, _real_req = _fake_req, mediaclean._req
os.environ["JELLYFIN_API_KEY"] = "test"
_jst = {}
_done, _told = mediaclean.run({}, lambda t, r: _sent.append((t, r)), days=3)   # default: silent
assert _done == ["old (1925)", "Other S01E03"] and _told == [] and _sent == [], _sent
_calls.clear()
_done, _told = mediaclean.run(_jst, lambda t, r: _sent.append((t, r)), days=3, notify=True)
assert ("DELETE", "/Items/old") in _calls and ("DELETE", "/Items/ep2") in _calls
assert not [c for c in _calls if c[0] == "DELETE" and c[1] in ("/Items/fav", "/Items/ep", "/Items/fresh", "/Items/warnme")]
assert _told == ["warnme (1925)"] and _sent[0][1][0][0][1] == "jf:keep:warnme" and "Cleared 2" in _sent[1][0], _sent
_sent.clear()
mediaclean.run(_jst, lambda t, r: _sent.append((t, r)), days=3, notify=True)
assert not [s for s in _sent if "deleted tomorrow" in s[0]]   # warned once only
mediaclean._req = _real_req
os.environ.pop("JELLYFIN_API_KEY")
print("JELLYFIN CLEANUP OK")

# ---------- 34. Mac clipping mode: clips stored on the Mac, everything via Telegram with buttons, Mac job schedule ----------
from clipper import mail as _ml  # noqa: E402
import importlib.util as _ilu  # noqa: E402
_mspec = _ilu.spec_from_file_location("clipper._mail_real", REPO / "clipper" / "mail.py")
_mreal = _ilu.module_from_spec(_mspec)
_mreal.__package__ = "clipper"
_mspec.loader.exec_module(_mreal)
_saved_mail = (_ml.send, _ml.read_replies)
_ml.send, _ml.read_replies = _mreal.send, _mreal.read_replies   # earlier sections fake these; use the real ones here
_spec = _ilu.spec_from_file_location("clipper._services_real", REPO / "clipper" / "services.py",
                                     submodule_search_locations=None)
_sv = _ilu.module_from_spec(_spec)   # earlier sections replace services' storage functions with fakes; test the real ones
_sv.__package__ = "clipper"
_spec.loader.exec_module(_sv)
os.environ.update(CLIP_STORAGE="local", CLIP_STORE=str(TMP / "store"))
_f = TMP / "c1.mp4"
_f.write_bytes(b"video")
_aid, _url = _sv.release_upload(_f, "c1.mp4")
assert _aid == "local:c1.mp4" and _url.startswith("file://") and (TMP / "store" / "c1.mp4").exists()
assert [a["id"] for a in _sv.queue_release()["assets"]] == ["local:c1.mp4"]
assert _sv.download(_url, TMP / "back.mp4").read_bytes() == b"video"
_sv.release_delete(_aid)
assert not (TMP / "store" / "c1.mp4").exists()
os.environ.pop("CLIP_STORAGE")
_tgcalls = []
_real_call = tg._call
tg._call = lambda m, data=None, files=None, timeout=30: (_tgcalls.append((m, data)), {"message_id": len(_tgcalls)})[1]
os.environ.update(NOTIFY_VIA="telegram", TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
_dry = os.environ.pop("DRY_RUN", None)
assert _ml.telegram_mode() and _ml.configured() and _ml.down_until() is None
_mid = _ml.send("Post now: boy band (8/10)", 'blah\n\nReply "posted" once it\'s up, or "skip" to drop it (a skipped clip '
                'gets replaced). It also notices on its own when it shows up on your TikTok.', attachment=str(_f))
assert _mid.startswith("tg:") and [c[0] for c in _tgcalls] == ["sendVideo", "sendMessage"], _tgcalls
_kb = json.loads(_tgcalls[-1][1]["reply_markup"])["inline_keyboard"]
assert _kb[0][0]["callback_data"] == f"r:posted:{_mid[3:]}" and "Tap Posted" in _tgcalls[-1][1]["text"]
_tgcalls.clear()
_body = ("=== TIKTOK CAPTION ===\nfunny caption\n#gta\n\n=== YOUTUBE TITLE ===\nboy band #shorts\n\n"
         "=== YOUTUBE DESCRIPTION ===\ndesc here\n\n=== WHAT'S IN IT ===\na meeting\n\n"
         "=== STILL IN THE QUEUE ===\n- other\n\nThe video is attached.")
_ml.send("Post now: boy band (8/10)", _body)
_texts = [c[1]["text"] for c in _tgcalls if c[0] == "sendMessage"]
assert _texts[1:] == ["⬇️ TikTok caption", "funny caption\n#gta", "⬇️ YouTube title", "boy band #shorts",
                      "⬇️ YouTube description", "desc here"], _texts
assert "WHAT'S IN IT" in _texts[0] and "funny caption" not in _texts[0] and "other" not in _texts[0]
assert "reply_markup" in [c for c in _tgcalls if c[0] == "sendMessage"][-1][1]   # buttons on the last field
_ml.send("Campaign: Benji ($1.50 per 1K)", "fits")
assert json.loads(_tgcalls[-1][1]["reply_markup"])["inline_keyboard"][0][0]["callback_data"].startswith("r:join:")
_ml.send("Clip bot problem: x", "y")
assert "reply_markup" not in _tgcalls[-1][1]
_ml.queue_reply(_mid, "posted")
_ml.queue_reply("", "pause")
assert _ml.read_replies() == [(_mid, "posted"), ("", "pause")] and _ml.read_replies() == []
_ml.check_login()
assert _tgcalls[-1][0] == "getMe"
# replies are read on every posting check in Telegram mode (Gmail spaces its logins out to ~hourly)
from clipper import clipqueue as _cq  # noqa: E402
_rs = {"clips": {}, "email": {"replies_at": __import__("datetime").datetime.now(_tz.utc).isoformat()}}
_ml.queue_reply("", "next")
_cq.refresh_posted({"accounts": {}}, _rs)
assert _rs["email"].get("send_next") is True, _rs
# the Mac loop turns a tap into a reply and marks the message
os.environ["MAC_RUNS_CLIPPER"] = "1"
mac.handle({}, "tap", f"r:skip:{_mid[3:]}", 7, "📲 Post now")
assert _ml.read_replies() == [(_mid, "skip")] and _tgcalls[-1][0] == "editMessageText"
mac.handle({}, "text", "pause", 8, "")
assert _ml.read_replies() == [("", "pause")]
# the job schedule: process first, then posting checks, never two main jobs at once, live watcher kept running
_started = []
class _P:  # noqa: E302
    def __init__(self, j): self.j, self.done = j, False  # noqa: E704
    def poll(self): return 0 if self.done else None  # noqa: E704
    pid = 1
_J = mac.Jobs()
_J._start = lambda j: (_started.append(j), _P(j))[1]
_J.next_process = _J.next_post = 0
_J.tick({})
assert _started == ["live", "process"], _started
_J.tick({})
assert _started == ["live", "process"]   # process still running: nothing else starts
_J.cur.done = True
_J.next_post = 0
_J.tick({})
assert _started[-1] == "post" and _J.busy()
_J.live.done = True
_J.cur.done = True
_J.tick({})
assert _started.count("live") == 2   # the watcher ended (6-hour limit): restarted
from clipper import weekly as _wk  # noqa: E402
_real_due, _wk.due = _wk.due, lambda cfg, st=None, now=None: True
if _J.cur is not None:
    _J.cur.done = True
_J.next_daily, _J.next_post, _J.next_process = 0, 1e18, 1e18
_jst = {"jobs_last": {"analytics": mac.local_now().date().isoformat(), "learn": mac.local_now().date().isoformat()}}
_J.tick(_jst)
assert _started[-1] != "weekly"   # the long video stays on GitHub unless MAC_RUNS_WEEKLY=1
os.environ["MAC_RUNS_WEEKLY"] = "1"
_J.next_daily = 0
_J.tick(_jst)
assert _started[-1] == "weekly", _started
os.environ.pop("MAC_RUNS_WEEKLY")
_wk.due = _real_due
(TMP / "config.yaml").write_text((TMP / "config.yaml").read_text())
_old_root2, mac.ROOT = mac.ROOT, TMP
assert "Lysium/Benji Ramos Clips" in mac.bot_campaigns()   # reads its own config when clipping here
mac.ROOT = _old_root2
os.environ["CLIPPER_WORK"] = "/Volumes/NoSuchDrive/clipper"
_ast = {}
assert _J.drive_ok(_ast) is False and "Work SSD" in json.dumps(_tgcalls[-1])
for _k in ("MAC_RUNS_CLIPPER", "CLIPPER_WORK", "NOTIFY_VIA", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "CLIP_STORE"):
    os.environ.pop(_k, None)
if _dry is not None:
    os.environ["DRY_RUN"] = _dry
tg._call = _real_call
_ml.send, _ml.read_replies = _saved_mail
_saved_path = os.environ.get("PATH", "")
os.environ["PATH"] = "/usr/bin:/bin"
mac.load_env(TMP / "no.env")
assert os.path.dirname(sys.executable) in os.environ["PATH"].split(os.pathsep)   # yt-dlp etc. are found by name
os.environ["PATH"] = _saved_path
print("MAC CLIPPING OK")

# ---------- 35. Campaign clips on the Mac: draft -> approved package -> Posted -> links submitted ----------
from clipper import crflow, contentrewards as _cr, jobs as _jobs  # noqa: E402
_cards_txt = ("Approved\n1\nLysium/Benji Ramos Clips\nCPM\n0/2 posted\n00:00\n/\n00:29\nSubmit link\n"
              "Lysium/Benji Ramos Clips\nCPM\n1/2 posted\n00:00\n/\n00:41\nSubmit link")
_cards = _cr.parse_approved_cards(_cards_txt)
assert _cards == [{"campaign": "Lysium/Benji Ramos Clips", "posted": 0, "of": 2, "duration": 29},
                  {"campaign": "Lysium/Benji Ramos Clips", "posted": 1, "of": 2, "duration": 41}], _cards
assert _cr.pick_card(_cards, "Lysium/Benji Ramos Clips", 40.5) == 1 and _cr.pick_card(_cards, "Other", 29) is None
assert _cr.pick_card(_cards, "Lysium/Benji Ramos Clips", 90) is None and _cr._secs("1:02:03") == 3723

_tgcalls2 = []
_real_call2 = tg._call
tg._call = lambda m, data=None, files=None, timeout=30: (_tgcalls2.append((m, data)), {"message_id": len(_tgcalls2)})[1]
os.environ.update(NOTIFY_VIA="telegram", TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1", MAC_RUNS_CLIPPER="1")
_dry2 = os.environ.pop("DRY_RUN", None)
_ccfg = common.load_config()
_vid = TMP / "camp.mp4"
_vid.write_bytes(b"x" * 100)
_cclip = {"id": "camp1", "streamer": "Lysium", "hook": "he started a boy band 💀", "score": 8, "duration": 29.0,
          "title": "They called a meeting and he said boy band", "caption": "the boy band reveal 😭",
          "start": 100, "vod_url": "https://www.twitch.tv/videos/1", "why": "meeting derailed", "status": "approved",
          "posts": {}, "created": common.now_utc().isoformat(), "preapproval": True, "handle": "twitch.tv/Lysium",
          "local_file": str(_vid), "speed": _jobs.clip_speed(_ccfg)}
assert crflow.applies(_ccfg, _cclip) and not crflow.applies(_ccfg, dict(_cclip, preapproval=False))
_cstate = {"clips": {"camp1": _cclip}}
_sent_mail = []
_ml.send = lambda *a, **k: _sent_mail.append(a) or "mail"
_mid = _jobs.send_package(_ccfg, _cstate, _cclip)   # the post job's slot: queued as a draft, nothing sent to Elie
assert _mid.startswith("tg:") and not _sent_mail and _cclip["emailed_at"] and _cclip["email_message_id"] == _mid
_q = crflow.load()
assert len(_q) == 1 and _q[0]["status"] == "queued" and _q[0]["campaign"] == "Lysium/Benji Ramos Clips"
assert os.path.exists(_q[0]["file"]) and _q[0]["platforms"] == ["tiktok", "youtube"]
_ml.send = _saved_mail[0]

class _FakeSite:  # noqa: E302
    drafts_sent, links_sent, cards = [], [], []
    def __enter__(self): return self  # noqa: E704
    def __exit__(self, *a): return False  # noqa: E704
    def submit_draft(self, url, video, platforms=("tiktok", "youtube"), dry_run=False):
        _FakeSite.drafts_sent.append((url, video, tuple(platforms)))
        return True
    def campaigns(self):
        return [dict(_info, name="Lysium/Benji Ramos Clips", status="approved",
                     url="https://contentrewards.com/c/campaigns/x")]
    def drafts(self): return {"counts": {"approved": 1, "posted": 2}}  # noqa: E704
    def approved(self): return list(_FakeSite.cards)  # noqa: E704
    def submit_link(self, campaign, duration, url, dry_run=False, draft_id=None):
        _FakeSite.links_sent.append((campaign, duration, url))
        return True
    picks = []   # what Content Rewards' "Submit clip" list offers (empty: fall back to reading TikTok/YouTube)
    def submit_pick(self, campaign, duration, texts, dry_run=False, draft_id=None):
        i = _cr.best_pick(_FakeSite.picks, texts)
        if i is None:
            return None
        return _FakeSite.picks.pop(i)
_real_site, _cr.Site = _cr.Site, _FakeSite
_mst = {"campaigns": {"Lysium/Benji Ramos Clips": {"name": "Lysium/Benji Ramos Clips", "approved": True,
                                                  "url": "https://contentrewards.com/c/campaigns/x"}},
        "drafts": {"approved": 0, "posted": 2}, "listed_once": True}
_real_bc, mac.bot_campaigns = mac.bot_campaigns, lambda: ["Lysium/Benji Ramos Clips"]
mac.process_drafts(_mst)   # 1. uploaded as a draft
assert _FakeSite.drafts_sent == [("https://contentrewards.com/c/campaigns/x", _q[0]["file"], ("tiktok", "youtube"))]
assert crflow.load()[0]["status"] == "submitted" and "for approval" in _tgcalls2[-1][1]["text"]
mac.process_drafts(_mst)
assert len(_FakeSite.drafts_sent) == 1   # never twice
_tgcalls2.clear()
mac.run_check(_mst)   # 2. not approved yet: no package
assert not [c for c in _tgcalls2 if c[0] == "sendVideo"]
_FakeSite.cards = _cr.parse_approved_cards(_cards_txt)[:1]
mac.run_check(_mst)   # approved: the package goes out with Posted / Skip
_e = crflow.load()[0]
assert _e["status"] == "approved", _e
_vids = [c for c in _tgcalls2 if c[0] == "sendVideo"]
_msgs = [c[1]["text"] for c in _tgcalls2 if c[0] == "sendMessage"]
assert len(_vids) == 1 and any("approved your draft" in m for m in _msgs), _msgs
assert "⬇️ TikTok caption" in _msgs and not any(m.startswith("🟢") for m in _msgs)
assert not any("Submit the attached video as a draft" in m or "CAMPAIGN APPROVAL FIRST" in m for m in _msgs)
_kb2 = json.loads([c for c in _tgcalls2 if c[0] == "sendMessage"][-1][1]["reply_markup"])["inline_keyboard"]
assert _kb2[0][0]["callback_data"] == f"r:posted:{_e['token']}"
_tgcalls2.clear()
mac.run_check(_mst)
assert not [c for c in _tgcalls2 if c[0] == "sendVideo"]   # the same approval isn't sent twice
mac.handle(_mst, "tap", f"r:posted:{_e['token']}", 9, "📲 Post now")   # 3. Posted
_e = crflow.load()[0]
assert _e["status"] == "posted" and _mst["hunt_next"] == 0 and _mreal.read_replies() == [(f"tg:{_e['token']}", "posted")]
_tt_url = "https://www.tiktok.com/@wantedlevelclips8/video/7569000000000000001"
_now_ts = common.now_utc().timestamp()
_real_up, mac._uploads = mac._uploads, lambda plats: {
    "tiktok": [{"text": "old one", "url": "https://www.tiktok.com/@w/video/1", "timestamp": _now_ts - 86400},
               {"text": "the boy band reveal 😭 Streamer: twitch.tv/Lysium #gta", "url": _tt_url, "timestamp": _now_ts}],
    "youtube": []}
mac.hunt(_mst)   # TikTok found and submitted; YouTube not up yet
_e = crflow.load()[0]
assert _FakeSite.links_sent == [("Lysium/Benji Ramos Clips", 29.0, _tt_url)] and _e["links"] == {"tiktok": _tt_url}
assert _e["status"] == "posted" and "Submitted your tiktok post" in _tgcalls2[-1][1]["text"]
_yt_url = "https://www.youtube.com/shorts/abcdefghijk"
mac.handle(_mst, "text", f"here {_yt_url}", 10, "")   # pasting the link works too
_e = crflow.load()[0]
assert _e["links"]["youtube"] == _yt_url and _e["status"] == "done", _e
mac.hunt(_mst)
assert len(_FakeSite.links_sent) == 2   # nothing submitted twice
# a post that never shows up: after 25 minutes Elie is told to paste it
_q2 = crflow.load()
_q2.append(dict(_q2[0], id="camp2", token="tok2", status="posted", links={},
                posted_at=(common.now_utc() - __import__("datetime").timedelta(minutes=26)).isoformat()))
crflow.save(_q2)
mac._uploads = lambda plats: {"tiktok": [], "youtube": []}
mac.hunt(_mst)
assert crflow.by_token(crflow.load(), "tok2")["status"] == "manual" and "in time" in _tgcalls2[-1][1]["text"]
# Content Rewards now lists your recent posts in the Submit form: the bot picks the matching one(s) from there
_lab = "He got asked one question and went full locked-in mode 💀 #shorts, @wantedlevelclips8, Posted 1 min ago, 28 min left"
assert _cr.parse_pick_label(_lab) == {"text": "He got asked one question and went full locked-in mode 💀 #shorts",
                                      "handle": "wantedlevelclips8", "posted": "1 min ago", "left": 28}
_labs = ["old thing nobody cares about, @wantedlevelclips8, Posted 2 min ago, 27 min left", _lab]
assert _cr.best_pick(_labs, ["He got asked one question and went full locked-in mode 💀"]) == 1
assert _cr.best_pick(_labs, ["completely different words here"]) is None
_q3 = crflow.load()
_q3.append(dict(_q3[0], id="camp9", token="tok9", status="posted", links={}, platforms=["tiktok", "youtube"],
                title="He got asked one question and went full locked-in mode 💀",
                caption="bro said are you pressing me young man 😭 Streamer: twitch.tv/Lysium #gta6",
                posted_at=common.now_utc().isoformat()))
crflow.save(_q3)
_FakeSite.picks = [_lab, "bro said are you pressing me young man 😭 Streamer: twitch.tv/Lysium #gta6 #gtarp, @wantedlevelclips8, "
                         "Posted 1 min ago, 28 min left"]
_n_links = len(_FakeSite.links_sent)
mac.hunt(_mst)
_e9 = crflow.by_token(crflow.load(), "tok9")
assert _e9["status"] == "done" and set(_e9["links"]) == {"tiktok", "youtube"}, _e9
assert _e9["links"]["youtube"].startswith("picked:He got asked") and len(_FakeSite.links_sent) == _n_links
assert "Submitted your" in _tgcalls2[-1][1]["text"]
# at most posting.daily_total (5) campaign clips a day: drafts and post packages, best score first
_real_dt = crflow.daily_total
crflow.daily_total = lambda cfg: 5   # an ordinary day (this test's state may sit in launch week)
_tgcalls2.clear()
_now_iso = common.now_utc().isoformat()
_base = {k: v for k, v in dict(_q2[0], links={}, campaign="Lysium/Benji Ramos Clips").items()
         if k not in ("submitted_at", "approved_at", "posted_at", "packaged_at")}
crflow.save([dict(_base, id=f"d{i}", token=f"dt{i}", status="queued", score=s_, queued_at=_now_iso, file=str(_vid))
             for i, s_ in enumerate([6, 9, 7, 8, 6.5, 5, 7.5])])
_FakeSite.drafts_sent.clear()
for _ in range(4):
    mac.process_drafts(_mst)
assert len(_FakeSite.drafts_sent) == 5, _FakeSite.drafts_sent
_sub = sorted(e["score"] for e in crflow.load() if e["status"] == "submitted")
assert _sub == [6.5, 7, 7.5, 8, 9], _sub            # the five best went (posting.daily_total)
_old_q = crflow.load()
_old_q.append(dict(_base, id="stale", token="st0", status="queued", score=10, file=str(TMP / "stale.mp4"),
                   queued_at=(common.now_utc() - __import__("datetime").timedelta(days=4)).isoformat()))
crflow.save(_old_q)
mac.process_drafts(_mst)
assert crflow.by_token(crflow.load(), "st0")["status"] == "dropped" and len(_FakeSite.drafts_sent) == 5
_ap = [dict(_base, id=f"a{i}", token=f"at{i}", status="approved", score=s_, approved_at=_now_iso)
       for i, s_ in enumerate([6, 9, 7, 8, 5, 7.5])]
crflow.save(_ap)
_mst["pkg_migrated"] = True
_mrc8 = os.environ.get("MAC_RUNS_CLIPPER")
os.environ["MAC_RUNS_CLIPPER"] = "1"
assert jobs._quota(_ccfg) == 0                      # 6 approved waiting claim the whole day: no regular posts
assert mac.send_packages_due(_mst) == 5
assert sorted(e["score"] for e in crflow.load() if e.get("packaged_at")) == [6, 7, 7.5, 8, 9]
assert mac.send_packages_due(_mst) == 0             # the 6th waits for tomorrow
crflow.save(_ap[:2])
assert jobs._quota(_ccfg) == 3                      # 2 campaign clips today: 3 regular posting times left
os.environ.pop("MAC_RUNS_CLIPPER", None)
assert jobs._quota(_ccfg) >= 3                      # GitHub: the ramp as before
if _mrc8 is not None:
    os.environ["MAC_RUNS_CLIPPER"] = _mrc8
crflow.daily_total = _real_dt
# YouTube through Blotato (posting.auto_post), TikTok still by hand
from clipper import services as _svc6  # noqa: E402
_bl = []
_real6 = (_svc6.blotato_upload, _svc6.blotato_account_id, _svc6.blotato_post, _svc6.blotato_wait)
_svc6.blotato_upload = lambda path: (_bl.append(("upload", str(path))), "https://media/x.mp4")[1]
_svc6.blotato_account_id = lambda platform: "acct-" + platform
_svc6.blotato_post = lambda platform, acct, url, text, title=None, ai_label=False: (_bl.append((platform, title)), "sub1")[1]
_svc6.blotato_wait = lambda sub, limit=180: ("published", "https://youtube.com/shorts/AUTO1")
assert jobs.auto_platforms(_ccfg) == []                # no key: nothing automatic
os.environ["BLOTATO_API_KEY"] = "k"
_mrc6 = os.environ.get("MAC_RUNS_CLIPPER")
assert jobs.auto_platforms(_ccfg) == [] or _mrc6 == "1"   # GitHub: off
os.environ["MAC_RUNS_CLIPPER"] = "1"
_saved_dry6 = jobs.DRY_RUN
jobs.DRY_RUN = False
assert jobs.auto_platforms(_ccfg) == ["youtube"]
_c6 = {"id": "c6", "hook": "he froze", "title": "He froze mid heist", "caption": "bro froze 😭", "streamer": "Lysium",
       "score": 8, "duration": 20.0, "start": 0, "end": 20, "local_file": str(_vid), "status": "approved",
       "created": common.now_utc().isoformat()}
_st6 = {"clips": {"c6": _c6}}
_sent6 = []
_real_sp6 = jobs.send_package
jobs.send_package = lambda cfg, state, clip: (_sent6.append(clipqueue.package(cfg, clip, [], state)), "mid")[1]
_real_ss6, jobs.save_state = jobs.save_state, lambda st: None
_real_es6, jobs.ensure_speed = jobs.ensure_speed, lambda cfg, state, clip, src, d: src
jobs.deliver(_ccfg, _st6, _c6)
assert [b for b in _bl if b[0] == "youtube"] == [("youtube", "He froze mid heist")] and not [b for b in _bl if b[0] == "tiktok"]
# with TikTok on its once-a-day schedule (posting.tiktok_daily) there's nothing left for Elie: no package at all
assert _c6["posts"]["youtube"]["url"] == "https://youtube.com/shorts/AUTO1" and _c6["status"] == "posted" and not _sent6
# without the daily TikTok, the package is TikTok only
_cfg_nt = dict(_ccfg, posting=dict(_ccfg["posting"], tiktok_daily=""))
_c6b = dict(_c6, id="c6b", status="approved", posts={})
_st6["clips"]["c6b"] = _c6b
jobs.deliver(_cfg_nt, _st6, _c6b)
_labels6 = [b[0] for b in _sent6[0]["blocks"]]
assert "TikTok caption" in _labels6 and "YouTube title" not in _labels6 and "YouTube: already posted" in _labels6, _labels6
assert "(TikTok)" in _sent6[0]["subject"]
# a campaign clip: on approval YouTube goes out through Blotato, its link is submitted, the package is TikTok only
_links6 = []
_real_sub6, mac._submit = mac._submit, lambda st, e, platform, url: (_links6.append((platform, url)),
                                                                   e.setdefault("links", {}).__setitem__(platform, url))
_real_tgsv, tg.send_video = tg.send_video, lambda *a, **k: None
_e6 = dict(_base, id="cy", token="cy1", status="approved", approved_at=_now_iso, file=str(_vid), platforms=["tiktok", "youtube"],
           draft_id="d-cy",
           blocks=[("TikTok caption", "bro froze"), ("YouTube title", "He froze #shorts"), ("YouTube description", "desc")])
crflow.save([_e6])
_tgcalls2.clear()
mac.send_post_package(crflow.load()[0], st=_mst)
assert _links6 == [("youtube", "https://youtube.com/shorts/AUTO1")] and crflow.load()[0]["links"]["youtube"].endswith("AUTO1")
_txt6 = " ".join(c[1].get("text", "") for c in _tgcalls2 if c[0] == "sendMessage")
assert "He froze #shorts" not in _txt6 and "post it on TikTok" not in _txt6   # TikTok is the bot's daily post now
mac._submit = _real_sub6
tg.send_video = _real_tgsv
jobs.send_package, jobs.save_state, jobs.ensure_speed = _real_sp6, _real_ss6, _real_es6
_svc6.blotato_upload, _svc6.blotato_account_id, _svc6.blotato_post, _svc6.blotato_wait = _real6
jobs.DRY_RUN = _saved_dry6
os.environ.pop("BLOTATO_API_KEY", None)
if _mrc6 is None:
    os.environ.pop("MAC_RUNS_CLIPPER", None)
# no Posted tap: an approved draft whose package went out is still watched, and its post gets submitted
_FakeSite.picks = ["he wanted one drive-by and got zero kills 😭 Streamer: twitch.tv/Lysium #gta6, @wantedlevelclips8, "
                   "Posted 3 min ago, 26 min left"]
crflow.save([dict(_base, id="w1", token="w1", status="approved", approved_at=_now_iso, packaged_at=_now_iso,
                  platforms=["tiktok"], links={}, caption="he wanted one drive-by and got zero kills 😭 Streamer: twitch.tv/Lysium",
                  title="He wanted one drive-by", hook="zero kills")])
mac.hunt(_mst)
_w1 = crflow.by_token(crflow.load(), "w1")
assert _w1["status"] == "done" and _w1["links"]["tiktok"].startswith("picked:"), _w1
assert mac.hunt_every() == 300
crflow.save([dict(_w1, status="approved", links={}, packaged_at=(common.now_utc() - __import__("datetime").timedelta(hours=30)).isoformat())])
assert not crflow.watching(crflow.load())   # stops looking after a day
assert _cr.pick_card([{"campaign": "Lysium/Benji Ramos Clips", "duration": 23.0, "posted": 0, "of": 2}],
                     "Lysium/Benji Ramos Clips", 26.9) == 0                       # a sped-up render still matches
assert _cr.pick_card([{"campaign": "Lysium/Benji Ramos Clips", "duration": 23.0, "posted": 0, "of": 2}],
                     "Lysium/Benji Ramos Clips", 31.0) is None
# "resend": every approved clip's package again, watched for a day
crflow.save([dict(_base, id=f"r{i}", token=f"rs{i}", status=st_, approved_at=_now_iso, file=str(_vid), score=7, links={},
                  blocks=[("TikTok caption", "c"), ("YouTube title", "t")]) for i, st_ in enumerate(["approved", "approved", "done"])])
_real_tgsv7, tg.send_video = tg.send_video, lambda *a, **k: None
_tgcalls2.clear()
mac.handle(_mst, "text", "resend", 20, "")
assert any("Sending 2 approved clips again" in (c[1] or {}).get("text", "") for c in _tgcalls2 if c[0] == "sendMessage")
assert len(crflow.watching(crflow.load())) == 2
tg.send_video = _real_tgsv7
# "youtube": the approved backlog goes up on YouTube through Blotato, one an hour, links submitted right after
_yt7 = []
_real_ay7, mac._auto_youtube = mac._auto_youtube, lambda st, e: (_yt7.append(e["token"]), True)[1]
_real_ap7, jobs.auto_platforms = jobs.auto_platforms, lambda cfg: ["youtube"]
crflow.save([dict(_base, id=f"y{i}", token=f"yt{i}", status="approved", approved_at=_now_iso, file=str(_vid), score=sc,
                  platforms=["tiktok", "youtube"], links={}, draft_id=f"d{i}") for i, sc in enumerate([6, 9, 7])]
            + [dict(_base, id="y9", token="yt9", status="approved", file=str(_vid), score=10, links={},
                    platforms=["tiktok", "youtube"])])   # no draft id: never posted (its link couldn't be matched)
_tgcalls2.clear()
_real_ready7, crflow.youtube_ready = crflow.youtube_ready, lambda *a, **k: True   # spacing is tested on its own
mac.handle(_mst, "text", "youtube", 21, "")
assert any("Posting 3 approved clips on YouTube" in (c[1] or {}).get("text", "") for c in _tgcalls2 if c[0] == "sendMessage")
mac.process_youtube_queue(_mst)
mac.process_youtube_queue(_mst)
assert _yt7 == ["yt1"], _yt7                       # best first, and only one until the next hour
_ql = crflow.load()
_due7 = common.now_utc().isoformat()
for _x in _ql:
    if _x.get("yt_at"):
        _x["yt_at"] = _due7
crflow.save(_ql)
mac.process_youtube_queue(_mst)
assert _yt7 == ["yt1", "yt2"], _yt7
mac._auto_youtube, jobs.auto_platforms = _real_ay7, _real_ap7
crflow.youtube_ready = _real_ready7
# drafts are tied to clips by Content Rewards' own id, learned from the video's exact size
_fa = TMP / "fa.mp4"; _fa.write_bytes(b"a" * 1000)  # noqa: E702
_fb = TMP / "fb.mp4"; _fb.write_bytes(b"b" * 2000)  # noqa: E702
_ents = [dict(_base, id="m1", token="m1", status="submitted", file=str(_fa), links={}),
         dict(_base, id="m2", token="m2", status="approved", file=str(_fb), links={})]
_cards = [{"id": "11111111-1111-1111-1111-111111111111", "size": 2000, "posted": 1, "of": 2, "duration": 23},
          {"id": "22222222-2222-2222-2222-222222222222", "size": 1000, "posted": 0, "of": 2, "duration": 23},
          {"id": "33333333-3333-3333-3333-333333333333", "size": 5555, "posted": 0, "of": 2, "duration": 23}]
_new, _odd = crflow.match_drafts(_ents, _cards)
assert _ents[0]["draft_id"].startswith("2222") and _ents[1]["draft_id"].startswith("1111")   # same length, told apart
assert [e["token"] for e in _new] == ["m1"] and [e["token"] for e in _odd] == ["m2"]   # m2 shows a post we didn't send
_new, _odd = crflow.match_drafts(_ents, _cards)
assert not _new                                          # ids remembered; nothing approved twice
# TikTok: exactly one post a day at posting.tiktok_daily, the clip that did best on YouTube lately
os.environ.update(BLOTATO_API_KEY="k", MAC_RUNS_CLIPPER="1")
_bl.clear()
_svc6.blotato_upload = lambda path: "https://media/t.mp4"
_svc6.blotato_account_id = lambda platform: "acct-" + platform
_svc6.blotato_post = lambda platform, acct, url, text, title=None, ai_label=False: (_bl.append((platform, text[:20])), "s")[1]
_svc6.blotato_wait = lambda sub, limit=180: ("published", "https://tiktok.com/@w/video/1")
jobs.save_state = lambda st: None
jobs.ensure_speed = lambda cfg, state, clip, src, d: src
assert clipqueue.tiktok_daily(_ccfg) == "11:00"
_recent = common.now_utc().isoformat()
_td = {"clips": {
    "lo": dict(_c6, id="lo", hook="meh", posts={"youtube": {"posted_at": _recent, "views": 300}}, local_file=str(_vid)),
    "hi": dict(_c6, id="hi", hook="banger", posts={"youtube": {"posted_at": _recent, "views": 9000}}, local_file=str(_vid)),
    "camp": dict(_c6, id="camp", hook="paid", preapproval=True,
                 posts={"youtube": {"posted_at": _recent, "views": 50000}}, local_file=str(_vid))}}
from zoneinfo import ZoneInfo as _ZI  # noqa: E402
_d11 = _dt(2026, 11, 9, 10, 40, tzinfo=_ZI(_ccfg["timezone"]))
assert jobs.tiktok_daily_post(_ccfg, _td, now=_d11) is None and not _bl            # not before 11:00
_d11b = _d11.replace(hour=11, minute=7)
assert jobs.tiktok_daily_post(_ccfg, _td, now=_d11b)["id"] == "hi"                 # best on YouTube, no campaign clip
assert [b[0] for b in _bl] == ["tiktok"] and _td["clips"]["hi"]["posts"]["tiktok"]["url"].endswith("/1")
assert jobs.tiktok_daily_post(_ccfg, _td, now=_d11b.replace(hour=12)) is None and len(_bl) == 1   # once a day
# a recent clip whose video is gone is skipped for the next one instead of losing the day
_td2 = {"clips": {
    "gone": dict(_c6, id="gone", hook="gone", posts={"youtube": {"posted_at": _recent, "views": 99999}},
                 local_file=str(TMP / "nope.mp4"), media_url=(TMP / "nope.mp4").as_uri()),
    "ok": dict(_c6, id="ok", hook="still here", posts={"youtube": {"posted_at": _recent, "views": 10}}, local_file=str(_vid))}}
assert jobs.tiktok_daily_post(_ccfg, _td2, now=_d11b)["id"] == "ok"
# posted clips keep their video for 3 days while they're TikTok candidates
assert jobs._keep_for_tiktok({"posts": {"youtube": {"posted_at": _recent}}})
assert not jobs._keep_for_tiktok({"posts": {"youtube": {"posted_at": _recent}, "tiktok": {}}})
_pk = clipqueue.package(_ccfg, dict(_c6, posts={}), [], {"clips": {}})
assert "TikTok caption" not in [b[0] for b in _pk["blocks"]]                       # nothing for Elie to post on TikTok
assert crflow.platforms(_ccfg) == ["youtube"]                                      # campaign drafts: YouTube only
os.environ.pop("BLOTATO_API_KEY", None)
if _mrc6 is None:
    os.environ.pop("MAC_RUNS_CLIPPER", None)
jobs.send_package, jobs.save_state, jobs.ensure_speed = _real_sp6, _real_ss6, _real_es6
_svc6.blotato_upload, _svc6.blotato_account_id, _svc6.blotato_post, _svc6.blotato_wait = _real6
# a clip queued before TikTok went once-a-day is still uploaded for YouTube only (no TikTok tick on the draft)
os.environ.update(BLOTATO_API_KEY="k", MAC_RUNS_CLIPPER="1")
crflow.save([dict(_base, id="pt", token="pt1", status="queued", score=9, queued_at=common.now_utc().isoformat(),
                  file=str(_vid), platforms=["tiktok", "youtube"], links={})])
_FakeSite.drafts_sent.clear()
mac.process_drafts(_mst)
assert _FakeSite.drafts_sent and _FakeSite.drafts_sent[-1][2] == ("youtube",), _FakeSite.drafts_sent
os.environ.pop("BLOTATO_API_KEY", None)
if _mrc6 is None:
    os.environ.pop("MAC_RUNS_CLIPPER", None)
# a campaign whose page isn't known: after 3 tries it's handed over with the manual steps
crflow.save([dict(_q2[0], id="camp3", token="tok3", status="queued", campaign="Unknown Campaign", tries=2, links={})])
mac.process_drafts(_mst)
assert crflow.by_token(crflow.load(), "tok3")["status"] == "manual_draft"
assert any("submitted by hand" in (c[1] or {}).get("text", "") for c in _tgcalls2 if c[0] == "sendMessage")
# the old "Claim" reminder stays quiet for clips the Mac submits itself
from clipper import clipqueue as _cq2, campaigns as _camps  # noqa: E402
_real_cf = _camps.campaign_for
_camps.campaign_for = lambda state, streamer, joined_only=False: {"name": "Lysium/Benji Ramos Clips", "url": "u"}
_tgcalls2.clear()
_cclip2 = dict(_cclip, posts={"tiktok": {"url": _tt_url}})
crflow.save([dict(_q2[0], id="camp1", token="t1", status="done")])
_cq2.claim_email(_ccfg, {"clips": {}}, _cclip2, "tiktok")
assert not _tgcalls2 and _cclip2["claim_sent"]["tiktok"] == "mac submits it"
_camps.campaign_for = _real_cf
_cr.Site, mac._uploads, mac.bot_campaigns = _real_site, _real_up, _real_bc
for _k in ("MAC_RUNS_CLIPPER", "NOTIFY_VIA", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_k, None)
if _dry2 is not None:
    os.environ["DRY_RUN"] = _dry2
tg._call = _real_call2
crflow.QUEUE.unlink(missing_ok=True)
print("CAMPAIGN FLOW OK")

# ---------- 36. Tapping Join joins the campaign on Content Rewards; a bio requirement waits for "Added" ----------
_page = ("Back\nwantedlevelclips1\nXposure\nNYZ Clipping\nPut twitch.tv/imnyzzz in your bio so viewers can find him.\n"
         "Joined\nLaunched 1 week ago\nSubmit clip\nBudget\n$105 / $1,000")
_pinfo = _cr.parse_campaign_page(_page)
assert _pinfo["joined"] and _pinfo["bio"] == ["Put twitch.tv/imnyzzz in your bio so viewers can find him."], _pinfo
assert _cr.parse_campaign_page("Add link in bio\nlinktr.ee/x\nJoined")["bio"] == ["linktr.ee/x"]
assert _cr.parse_campaign_page("Join Campaign\nClip his biology streams")["bio"] == []
_tgcalls3 = []
_real_call3 = tg._call
tg._call = lambda m, data=None, files=None, timeout=30: (_tgcalls3.append((m, data)), {"message_id": len(_tgcalls3)})[1]
os.environ.update(NOTIFY_VIA="telegram", TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1", MAC_RUNS_CLIPPER="1")
_sf = common.DATA / "state.json"
_sf_old = _sf.read_text() if _sf.exists() else None
_sf.write_text(json.dumps({"campaigns": {
    "c1": {"id": "c1", "name": "NYZ Clipping", "join_url": "https://clippingalpha.com/go/1", "email_message_id": "tg:jt1",
           "status": "notified"},
    "c2": {"id": "c2", "name": "Plain One", "join_url": "https://clippingalpha.com/go/2", "email_message_id": "tg:jt2",
           "status": "notified"},
    "c3": {"id": "c3", "name": "Whop One", "join_url": "https://clippingalpha.com/go/3", "email_message_id": "tg:jt3",
           "status": "notified"}}}))
_mreal.REPLIES.unlink(missing_ok=True)


class _JoinSite:  # noqa: E302
    opened = []
    def __enter__(self): return self  # noqa: E704
    def __exit__(self, *a): return False  # noqa: E704
    def join(self, url):
        _JoinSite.opened.append(url)
        if url.endswith("/1"):
            return dict(_pinfo, url="https://contentrewards.com/c/campaigns/nyz", name="NYZ Clipping", joined=True)
        if url.endswith("/2"):
            return dict(_cr.parse_campaign_page("Joined\npre-approval required"), url="https://contentrewards.com/c/campaigns/p",
                        name="Plain One", joined=True)
        return {"elsewhere": True, "url": "https://whop.com/x", "joined": False, "bio": []}
_real_site3, _cr.Site = _cr.Site, _JoinSite  # noqa: E305
_jst = {}
mac.handle(_jst, "tap", "r:join:jt1", 5, "Campaign: NYZ Clipping")
assert mac.joins_load()[0]["status"] == "todo" and not _mreal.read_replies()   # not "joined" until it really is
assert "Joining it on Content Rewards" in _tgcalls3[-1][1]["text"]
mac.process_joins(_jst)
assert _JoinSite.opened == ["https://clippingalpha.com/go/1"] and mac.joins_load()[0]["status"] == "bio_wait"
_m = _tgcalls3[-1][1]
assert "twitch.tv/imnyzzz" in _m["text"] and json.loads(_m["reply_markup"])["inline_keyboard"][0][0]["callback_data"] == "cj:added:jt1"
assert not _mreal.read_replies() and _jst["campaigns"]["NYZ Clipping"]["bio_wait"]
assert not [x for x in mac.compare(_jst, [dict(_pinfo, name="NYZ Clipping", status="approved")], {}, []) if "isn't putting" in x]
mac.process_joins(_jst)
assert len(_JoinSite.opened) == 1   # never joined twice
mac.handle(_jst, "tap", "cj:added:jt1", 7, "✅ Joined NYZ Clipping")
assert _mreal.read_replies() == [("tg:jt1", "join")] and not _jst["campaigns"]["NYZ Clipping"]["bio_wait"]
assert "All set" in _tgcalls3[-1][1]["text"] and mac._campaign_url(_jst, "NYZ Clipping").endswith("/nyz")
_mreal.REPLIES.unlink(missing_ok=True)
mac.handle(_jst, "tap", "r:join:jt2", 8, "Campaign: Plain One")   # no bio: joined straight away
mac.process_joins(_jst)
assert _mreal.read_replies() == [("tg:jt2", "join")] and "approval first" in _tgcalls3[-1][1]["text"]
assert mac._campaign_url({}, "Plain One") == "https://contentrewards.com/c/campaigns/p"
_mreal.REPLIES.unlink(missing_ok=True)
mac.handle(_jst, "tap", "r:join:jt3", 9, "Campaign: Whop One")   # another board: link + "Joined" button
mac.process_joins(_jst)
assert "isn't on Content Rewards" in _tgcalls3[-1][1]["text"] and not _mreal.read_replies()
mac.handle(_jst, "tap", "cj:done:jt3", 10, "x")
assert _mreal.read_replies() == [("tg:jt3", "join")]
_mreal.REPLIES.unlink(missing_ok=True)
_cr.Site = _real_site3
mac.JOINS.unlink(missing_ok=True)
if _sf_old is None:
    _sf.unlink(missing_ok=True)
else:
    _sf.write_text(_sf_old)
for _k in ("MAC_RUNS_CLIPPER", "NOTIFY_VIA", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_k, None)
tg._call = _real_call3
print("CAMPAIGN JOIN OK")

# ---------- 37. Monday earnings report, one-tap fix for "needs changes", launch-week budget ----------
_earn_txt = ("Earnings\nwantedlevelclips1\nLifetime earned\n$0.00\nWithdraw\n$0.00\nToday\n$0.00\nThis week\n$0.00\n"
             "This month\n$0.00\nBalance\nAll time\nQueued\n(2)\nStill accumulating: $3.51\nLysium/Benji Ramos Clips\nCPM\n"
             "$1.88\nPays Nov 14\nAccumulating\nLysium/Benji Ramos Clips\nCPM\n$1.63\nPays Nov 14\nAccumulating\n"
             "Arriving\n(0)\nTotal: $0.00\nNo payouts on the way\nReceived\n(1)\nTotal: $12.40")
_earn = _cr.parse_earnings(_earn_txt)
assert _earn["queued"] == 3.51 and _earn["received"] == 12.40 and _earn["balance"] == 0.0 and len(_earn["items"]) == 2
assert _earn["items"][0] == {"section": "queued", "campaign": "Lysium/Benji Ramos Clips", "amount": 1.88,
                             "pays": "Nov 14", "state": "Accumulating"}
_et = mac.earnings_text(_earn, last=10.0, claude=1.234, budget=5.0)
assert "$15.91 so far (+$5.91 since last Monday)" in _et and "$3.51 from 2 clips" in _et and "Nov 14" in _et, _et
assert "$1.23 of $5.00" in _et

_tgcalls4 = []
_real_call4 = tg._call
tg._call = lambda m, data=None, files=None, timeout=30: (_tgcalls4.append((m, data)), {"message_id": len(_tgcalls4)})[1]
os.environ.update(NOTIFY_VIA="telegram", TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1", MAC_RUNS_CLIPPER="1")
_fst = {"campaigns": {}, "listed_once": True, "drafts": {}}
_note = {"who": "Brand reviewer", "when": "Nov 3, 6:48 AM", "note": "start it 5 seconds later", "campaign": "Lysium/Benji Ramos Clips",
         "card": "Lysium | 0:29"}
_fm = mac.compare(_fst, [], {"counts": {}, "needs_changes": [_note]}, [])
assert len(_fm) == 1 and "5 seconds later" in _fm[0] and _fm[0].rows[0][0][1].startswith("fx:")
_fkey = _fm[0].rows[0][0][1][3:]
crflow.save([{"id": "fixclip", "token": "ftok", "campaign": "Lysium/Benji Ramos Clips", "status": "submitted",
              "submitted_at": "2026-11-03T10:00:00+00:00", "platforms": ["tiktok", "youtube"], "links": {}}])


class _FakeJobs:  # noqa: E302
    started = []
    running = False
    def busy(self): return _FakeJobs.running  # noqa: E704
    def _start(self, job):
        _FakeJobs.started.append(job)
        _FakeJobs.running = True
        return object()
mac.handle(_fst, "tap", f"fx:{_fkey}", 3, _fm[0])  # noqa: E305
assert _fst["fixes"][_fkey]["status"] == "todo" and "Working on it" in _tgcalls4[-1][1]["text"]
_fj = _FakeJobs()
mac.process_fixes(_fst, _fj)
from clipper import fixer as _fixer  # noqa: E402
_job = json.loads(_fixer.JOB.read_text())
assert _FakeJobs.started == ["revise"] and _job["clip_id"] == "fixclip" and _job["token"] == "ftok"
assert _fst["fixes"][_fkey]["status"] == "running"
mac.process_fixes(_fst, _fj)
assert _FakeJobs.started == ["revise"]   # not started twice
_fixer.JOB.write_text(json.dumps(dict(_job, status="ready", file=str(_vid))))
mac.process_fixes(_fst, _fj)
assert _fst["fixes"][_fkey]["status"] == "ready" and _fst["fixes"][_fkey]["file"] == str(_vid)
# what Claude's answer does to the cut (trims clamped, never shorter than 8s)
_p = _fixer.apply({"start": 100, "end": 130, "hook": "old", "title": "t", "caption": "c"},
                  {"start_shift": 5, "end_shift": -50, "hook": "new hook"})
assert (_p["start"], _p["end"]) == (100, 130) and _p["hook"] == "new hook" and _p["title"] == "t"   # too short: cut kept
_p = _fixer.apply({"start": 100, "end": 130, "hook": "old", "title": "t", "caption": "c"},
                  {"start_shift": 5, "end_shift": -15, "hook": "new hook"})
assert _p["start"] == 105 and _p["end"] == 115 and _p["hook"] == "new hook" and _p["title"] == "t"
_p = _fixer.apply({"start": 100, "end": 112, "hook": "h"}, {"start_shift": 10})
assert (_p["start"], _p["end"]) == (100, 112)


class _RevSite:  # noqa: E302
    sent = []
    def __enter__(self): return self  # noqa: E704
    def __exit__(self, *a): return False  # noqa: E704
    def upload_revision(self, campaign, video): _RevSite.sent.append((campaign, video)); return True  # noqa: E704,E702
_real_site4, _cr.Site = _cr.Site, _RevSite  # noqa: E305
mac.handle(_fst, "tap", f"fxs:{_fkey}", 4, "Send it?")
mac.process_fixes(_fst, _fj)
assert _RevSite.sent == [("Lysium/Benji Ramos Clips", str(_vid))] and _fst["fixes"][_fkey]["status"] == "sent"
assert "Revision sent" in _tgcalls4[-1][1]["text"]
mac.handle(_fst, "tap", f"fxs:{_fkey}", 4, "Send it?")
mac.process_fixes(_fst, _fj)
assert len(_RevSite.sent) == 1   # never sent twice
_cr.Site = _real_site4
_fixer.JOB.unlink(missing_ok=True)
crflow.QUEUE.unlink(missing_ok=True)
for _k in ("MAC_RUNS_CLIPPER", "NOTIFY_VIA", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_k, None)
tg._call = _real_call4

# launch week: $4/day instead of the monthly budget, and those days don't eat next month's budget
_today = common.now_utc().date()
_lcfg = {"limits": {"claude_usd_per_day": 1.0, "claude_usd_per_month": 5.0, "claude_usd_day_max": 0.5,
                    "boost_dates": [_today.isoformat()], "claude_usd_boost_per_day": 4.0}}
_lst = {"spend": {_today.isoformat(): 1.0}}
assert abs(highlights.budget_left(_lcfg, _lst) - 3.0) < 0.01
_yday = (_today - __import__("datetime").timedelta(days=1)).isoformat()
_lcfg["limits"]["boost_dates"] = [_yday]
_lst = {"spend": {_yday: 20.0, _today.isoformat(): 0.1}}
assert abs(highlights.budget_left(_lcfg, _lst) - 0.4) < 0.01   # back to normal: day max 0.50 minus 0.10 spent
_lcfg["limits"]["boost_dates"] = []
assert highlights.budget_left(_lcfg, _lst) < 0                  # without the exemption the month would be gone
_l2 = {"launch": {"enabled": True, "date": "2026-11-19", "boost_days": 8, "claude_usd_per_day": 4.0}, "limits": {}}
from clipper import launch as _launch  # noqa: E402
_launch.apply(_l2, {})
assert _l2["limits"]["boost_dates"][0] == "2026-11-19" and _l2["limits"]["boost_dates"][-1] == "2026-11-26"
print("EARNINGS FIX BUDGET OK")

# ---------- 38. Campaign streams: lower bar, more clips, never blocked by a full queue; drafts go up right away ----------
_ccfg = common.load_config()
_lys = next(x for x in _ccfg["streamers"] if x.get("campaign"))
_plain = next(x for x in _ccfg["streamers"] if not x.get("campaign"))
assert jobs.for_stream(_ccfg, _plain) is _ccfg
_cc = jobs.for_stream(_ccfg, _lys)["clip"]
assert _cc["min_score"] == 6 and _cc["clips_per_hour"] == 3 and _cc["min_per_vod"] == 5 and _ccfg["clip"]["min_score"] == 7
assert highlights.clip_budget(jobs.for_stream(_ccfg, _lys), 2 * 3600)[0] == 6
assert highlights.clip_budget(_ccfg, 2 * 3600)[0] == 4
assert jobs.for_stream(_ccfg, dict(_lys, min_score=5))["clip"]["min_score"] == 5
from clipper import live as _live  # noqa: E402
assert _live._settings(_ccfg, _lys)["chat_ratio"] == 2.5 and _live._settings(_ccfg, _plain)["chat_ratio"] == 3.0
assert "campaign" not in _live._settings(_ccfg)
_qst = {"clips": {"a": {"id": "a", "status": "approved", "streamer": "Burn", "score": 9, "created": "2026-11-05T10:00:00+00:00"},
                  "b": {"id": "b", "status": "approved", "streamer": "Lysium", "score": 6, "campaign": "Lysium/Benji Ramos Clips",
                        "created": "2026-11-05T09:00:00+00:00"}}}
assert clipqueue.pick_for_email(_qst)["id"] == "b" and clipqueue.queue(_qst)[0]["id"] == "b"
assert not jobs.is_campaign({"campaign": "X", "campaign_joined": False}) and jobs.is_campaign(_lys)
from clipper import campaigns as _cmp3  # noqa: E402
assert _cmp3.clean_name("Lysium/Benji Ramos Clips Post on TikTok, Instagram, YouTube, X CLIPPING CPM $1.50") == \
    "Lysium/Benji Ramos Clips"
_qst["clips"]["b"].update(preapproval=True, emailed_at="2026-11-05T10:00:00+00:00")
assert clipqueue.room(_ccfg, _qst) == clipqueue.settings(_ccfg)["max_size"] - 1   # a draft out for approval doesn't count
# the daily posting time moves by itself once another time clearly beats it
from clipper import timing as _tm  # noqa: E402
_tz5 = _ccfg["timezone"]
def _yt(day, hh, views):  # noqa: E302
    t0 = _dt(2026, 10, day, hh, 2, tzinfo=ZoneInfo(_tz5)).astimezone(_tz.utc)
    return {"posts": {"youtube": {"posted_at": t0.isoformat(), "history": [[(t0 + timedelta(hours=4)).isoformat(), views]]}}}
_fst5 = {"clips": {}}
for _d in range(1, 4):  # noqa: E305
    _fst5["clips"][f"e{_d}"] = _yt(_d, 11, 300)
    _fst5["clips"][f"t{_d}"] = _yt(_d, 10, 900)
assert _tm.favorite(_ccfg, _fst5, _tz5) == "10:00" and _fst5["timing_favorite"]["moves"][-1]["from"] == "11:00"
assert "10:00" in _tm.plan(_ccfg, _fst5, "2026-11-20", 2, _tz5)
_fst5["clips"]["t1"] = _yt(1, 10, 320)   # a close race doesn't move it back and forth
_fst6 = {"clips": {k: v for k, v in _fst5["clips"].items()}}
_fst6["clips"].update({f"t{_d}": _yt(_d, 10, 330) for _d in range(1, 4)})
assert _tm.favorite(_ccfg, _fst6, _tz5) == "11:00"
print("CAMPAIGN MAX OK")

# ---------- 39. Nightly backup to the Work SSD: settings and the bot's memory, never media ----------
from clipper import backup as _bk  # noqa: E402
_bh = TMP / "bk_home"
(_bh / "servarr" / "radarr").mkdir(parents=True, exist_ok=True)
(_bh / "servarr" / "radarr" / "radarr.db").write_text("db")
(_bh / "servarr" / "radarr" / "MediaCover").mkdir(exist_ok=True)
(_bh / "servarr" / "radarr" / "MediaCover" / "poster.jpg").write_text("x" * 100)
(_bh / "servarr" / "data" / "media" / "movies").mkdir(parents=True, exist_ok=True)
(_bh / "servarr" / "data" / "media" / "movies" / "Film.mkv").write_text("big")
(_bh / "servarr" / "compose.yaml").write_text("services: {}")
(_bh / "jellyfin" / "data").mkdir(parents=True, exist_ok=True)
(_bh / "jellyfin" / "data" / "jellyfin.db").write_text("users")
(_bh / "jellyfin" / "metadata").mkdir(exist_ok=True)
(_bh / "jellyfin" / "metadata" / "art.jpg").write_text("art")
(_bh / "jellyfin" / "log").mkdir(exist_ok=True)
(_bh / "jellyfin" / "log" / "x.log").write_text("log")
os.environ.update(BACKUP_DIR=str(TMP / "bk_dest"), SERVARR_DIR=str(_bh / "servarr"), JELLYFIN_DIR=str(_bh / "jellyfin"),
                  ADGUARD_CONF=str(_bh / "nope.yaml"))
(common.DATA / "state.json").exists() or (common.DATA / "state.json").write_text("{}")
_r = _bk.run()
_L = TMP / "bk_dest" / "latest"
assert _r["ok"] and "bot/data" in _r["copied"] and "adguard/AdGuardHome.yaml" in _r["missing"], _r
assert (_L / "servarr" / "radarr" / "radarr.db").exists() and (_L / "servarr" / "compose.yaml").exists()
assert not (_L / "servarr" / "data").exists() and not (_L / "servarr" / "radarr" / "MediaCover").exists()   # no media
assert (_L / "jellyfin" / "data" / "jellyfin.db").exists()                                                  # users kept
assert not (_L / "jellyfin" / "metadata").exists() and not (_L / "jellyfin" / "log").exists()
assert (_L / "bot" / "data" / "state.json").exists() and (TMP / "bk_dest" / "RESTORE.txt").exists()
from datetime import datetime as _bdt  # noqa: E402
for _i in range(10):   # only the last 7 days of the bot's memory are kept
    _bk.run(now=_bdt(2026, 11, 1 + _i, 3, 0))
assert len(list((TMP / "bk_dest" / "bot-daily").iterdir())) == 7
for _k in ("BACKUP_DIR", "SERVARR_DIR", "JELLYFIN_DIR", "ADGUARD_CONF"):
    os.environ.pop(_k, None)
assert _bk.dest() is None or str(_bk.dest()).startswith("/Volumes/")   # no Work SSD here: nothing to back up to
_un = mac.compare({"campaigns": {}, "listed_once": True, "drafts": {}}, [],
                  {"counts": {"needs changes": 1}, "needs_changes": [{"who": "the reviewer", "when": "recently", "note": "(open it)",
                                                                       "card": "unread-1", "unread": True}]}, [])
assert len(_un) == 1 and not getattr(_un[0], "rows", None)   # a note we couldn't read gets no Fix it button
# a pulled Work SSD: a leftover folder at /Volumes/<name> must not count as the drive
os.environ["CLIPPER_WORK"] = "/Volumes/NoSuchDriveXYZ/clipper"
assert not common.work_ready()
os.environ["CLIPPER_WORK"] = str(common.WORK)
assert common.work_ready()
# quiet by default: progress notes ("posted", "submitted") stay off Telegram unless notify.updates is on
_q_calls = []
_real_call9 = tg._call
tg._call = lambda m, data=None, files=None, timeout=30: (_q_calls.append((m, data)), {"message_id": 1})[1]
os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
_real_info("💰 Submitted your youtube post")
assert not _q_calls
tg.send("⚠️ something broke")
assert len(_q_calls) == 1
tg._call = _real_call9
for _k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_k, None)
# certain-GTA campaigns are joined by the Mac by itself; anything less certain isn't sent
from clipper import campaigns as _cmp9  # noqa: E402
assert _cmp9.sure_gta({"name": "NoPixel 4.0 Clips", "brief": ""})
assert _cmp9.sure_gta({"name": "Lysium/Benji Ramos Clips", "brief": "", "twitch_login": "lysium",
                       "why": "GTA in 9 of their last 10 streams"})
assert not _cmp9.sure_gta({"name": "Minecraft RP Server", "brief": "roleplay clips", "twitch_login": "x",
                           "why": "GTA in 2 of their last 10 streams"})
_mrc9 = os.environ.get("MAC_RUNS_CLIPPER")
os.environ["MAC_RUNS_CLIPPER"] = "1"
mac.JOINS.unlink(missing_ok=True)
_ref = _cmp9.notify({}, {"name": "GTA RP Highlights", "brief": "", "join_url": "https://clippingalpha.com/go/9",
                          "url": "u", "why": "mentions GTA"})
assert _ref.startswith("tg:") and mac.joins_load()[0]["status"] == "todo" and mac.joins_load()[0]["auto"]
assert _cmp9.notify({}, {"name": "Rust roleplay", "brief": "", "why": "mentions GTA"}).startswith("skipped")
assert len(mac.joins_load()) == 1
mac.JOINS.unlink(missing_ok=True)
if _mrc9 is None:
    os.environ.pop("MAC_RUNS_CLIPPER", None)
# posts a day step up: 5 now, 10 from launch day (and the week after), 12 from Dec 3
assert [crflow.daily_total(_ccfg, d) for d in ("2026-11-10", "2026-11-19", "2026-11-30", "2026-12-03")] == [5, 10, 10, 12]
# and the Claude budget steps up with them
_bs = {"limits": {"claude_usd_per_month": 5.0, "claude_usd_day_max": 0.5,
                  "budget_schedule": [{"from": "2000-01-01", "per_month": 30.0, "day_max": 1.5}]}}
assert abs(highlights.budget_left(_bs, {"spend": {}}) - 1.5) < 0.01
# end-of-day message: posts per platform, how many paid, drafts, best recent post, Claude spend
_today9 = mac.local_now().date().isoformat()
_nowi = common.now_utc().isoformat()
_main9 = {"clips": {"a": {"hook": "big one", "posts": {"youtube": {"posted_at": _nowi, "views": 4200},
                                                       "tiktok": {"posted_at": _nowi}}},
                    "b": {"hook": "paid one", "campaign": "Lysium", "posts": {"youtube": {"posted_at": _nowi, "views": 90}}}},
          "spend": {common.now_utc().date().isoformat(): 0.31}}
_ent9 = [{"yt_auto_at": _nowi, "submitted_at": _nowi}, {"approved_at": _nowi}]
_d9 = mac.day_stats(_main9, _ent9, [], _ccfg, _today9)
assert (_d9["youtube"], _d9["tiktok"], _d9["campaign"], _d9["drafts"], _d9["approved"]) == (3, 1, 2, 1, 1), _d9
_t9 = mac.day_text(_d9)
assert "3 on YouTube, 1 on TikTok (2 campaign)" in _t9 and "big one" in _t9 and "$0.31" in _t9, _t9
# YouTube posts are spread out: never closer than the posting day divided by the day's posts (45 min minimum)
_g = dict(_ccfg, limits=dict(_ccfg.get("limits") or {}, boost_dates=[]),
          posting=dict(_ccfg["posting"], daily_total_schedule=[]))
assert crflow.youtube_gap_minutes(_g) == 180
_half_hour_ago = (common.now_utc() - __import__("datetime").timedelta(minutes=30)).isoformat()
assert not crflow.youtube_ready(_g, {"a": {"posts": {"youtube": {"posted_at": _half_hour_ago}}}}, [])
assert crflow.youtube_ready(_g, {}, [{"yt_auto_at": "2026-01-01T00:00:00+00:00"}])
assert crflow.youtube_gap_minutes(dict(_g, posting=dict(_g["posting"], daily_total=12))) == 75
# strong swearing is masked in on-screen text and post text (YouTube age-restricts for it); ordinary words aren't
from clipper import clean as _cl  # noqa: E402
assert _cl.mask("YOU FUCKING MORON") == "YOU F***ING MORON"
assert _cl.mask("bro said shit") == "bro said s***"
assert _cl.mask("cockpit Dickens Scunthorpe classic") == "cockpit Dickens Scunthorpe classic"
assert _cl.mask("what a dick") == "what a d***"
_ass = TMP / "mask.ass"
render.build_ass({"start": 0, "end": 3}, [{"w": "fucking", "s": 0.1, "e": 0.5}], _ass)
assert "F***ING" in _ass.read_text() and "FUCKING" not in _ass.read_text()
assert "f***" in jobs._caption(_ccfg, {"caption": "fuck that guy", "streamer": "Lysium"}, "youtube")
print("BACKUP OK")
