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
REPO = Path(__file__).resolve().parent.parent
shutil.copy(REPO / "config.yaml", TMP / "config.yaml")
# Sections 1-8 test the Blotato auto-posting path; section 9 switches to email mode.
_cfg_text = (TMP / "config.yaml").read_text().replace("  mode: email ", "  mode: blotato ", 1)
assert "mode: blotato" in _cfg_text
_cfg_text = _cfg_text.replace("launch:                           # GTA 6", "launch:  # GTA 6", 1).replace(
    "  enabled: true\n  date: \"2026-11-19\"", "  enabled: false\n  date: \"2026-11-19\"", 1)
assert "enabled: false\n  date:" in _cfg_text   # launch mode is tested on its own in section 21
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
assert "status" in issues and "12,000" in issues["status"]
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


mail.send = fake_send
replies = []
mail.read_replies = lambda since_days=7: list(replies)
tt_uploads = []
clipqueue.tiktok_uploads = lambda handle, n=15: list(tt_uploads)
clipqueue.youtube_channel_id = lambda handle: "UC" + "x" * 22
clipqueue.youtube_uploads = lambda cid: []

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
# full queue: no Claude work, and the process job doesn't even list streams
assert not jobs.claude_allowed(cfg, st)
common.save_state(st)
_orig_list = fetch.list_new_vods
fetch.list_new_vods = lambda *a, **k: (_ for _ in ()).throw(AssertionError("process should not look for streams"))
os.environ.pop("FORCE")
jobs.FORCE = False
jobs.process()
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
common.add_spend(1.25)
assert highlights.budget_left(cfg, st) <= 0 and not jobs.claude_allowed(cfg, st)
common.save_state(st)
st = common.load_state()
assert abs(common.spend_today(st) - 1.25) < 0.01 and not jobs.claude_allowed(cfg, st)
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
    return {"posts": {"tiktok": {"posted_at": t0.isoformat(), "history": hist, "views": views, "likes": views // 20}}}
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
