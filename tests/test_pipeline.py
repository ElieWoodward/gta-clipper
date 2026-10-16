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
picks, words = highlights.analyze(cfg, vod, wav, client=claude)
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
(TMP / "config.yaml").write_text(_cfg_text.replace("  mode: blotato ", "  mode: email ", 1))
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
for label in ("TIKTOK CAPTION", "YOUTUBE TITLE", "YOUTUBE DESCRIPTION", "YOUTUBE TAGS", "WHAT'S IN IT", "SOURCE",
              "STILL IN THE QUEUE"):
    assert f"=== {label} ===" in sent[0]["text"], label
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
