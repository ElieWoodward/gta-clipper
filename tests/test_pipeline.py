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
os.environ.update(CLIPPER_ROOT=str(TMP), CLIPPER_WORK=str(TMP / "work"), FORCE="1")
sys.path.insert(0, str(REPO))

from clipper import fetch, highlights, jobs, services, common  # noqa: E402

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


def fake_transcribe(wav, model, start=0.0, end=None):
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
        self.prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply(kw))])


def judge_reply(kw):
    p = kw["messages"][0]["content"]
    import re
    picks = []
    for i, s, e in [(int(a), float(b), float(c)) for a, b, c in re.findall(r"\[id (\d+)\] (\d+)s-(\d+)s", p)][:2]:
        picks.append({"id": i, "start": s + 5, "end": s + 40,  # deliberately too short: validator must extend to >= 61s
                      "score": 8 if i == 0 else 6, "hook": "he really thought the *bridge* would hold",
                      "title": "Bridge jump escape", "caption": "He really thought this would work",
                      "tags": ["chaos", "police"], "why": "clear payoff",
                      "commentary": [{"at": 1, "text": "He has 4 stars and no plan"},
                                     {"at": 12, "text": "The bridge is closed for construction"},
                                     {"at": 30, "text": "Chat told him not to try it"}]})
    return json.dumps({"picks": picks})


highlights.transcribe = fake_transcribe
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
cfg["clip"]["per_vod"] = 2
vod = {"id": "twitch:123", "url": "https://twitch.tv/videos/123", "title": "GTA 6 day 1",
       "streamer": {"name": "teststreamer", "platform": "twitch"}}
claude = FakeClaude(judge_reply)
d = TMP / "work" / "vod"
d.mkdir(parents=True)
wav = fake_audio(vod, d)
picks, words = highlights.analyze(cfg, vod, wav, client=claude)
print("loudness picks ->", [(p["start"], p["end"], p["score"]) for p in picks])
assert len(picks) == 2, picks
for p in picks:
    assert cfg["clip"]["min_seconds"] <= p["end"] - p["start"] <= cfg["clip"]["max_seconds"], p
for p in picks:
    jobs.make_clip(cfg, state, vod, p, words, d)

# Same picks with a facecam layout
vod2 = dict(vod, id="twitch:456", streamer={"name": "camstreamer", "platform": "twitch",
                                          "facecam": {"x": 0.7, "y": 0.02, "w": 0.28, "h": 0.28}})
jobs.make_clip(cfg, state, vod2, picks[0], words, d)

state = common.load_state()
assert len(state["clips"]) == 3
for c in state["clips"].values():
    out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_type:format=duration",
                                     "-of", "json", c["local_file"]], capture_output=True, text=True).stdout)
    v = [s for s in out["streams"] if s["codec_type"] == "video"][0]
    dur = float(out["format"]["duration"])
    print(c["id"], c["status"], f"{v['width']}x{v['height']}", f"{dur:.1f}s", c["score"])
    assert (v["width"], v["height"]) == (1080, 1920)
    assert dur > 60.5, dur
statuses = sorted(c["status"] for c in state["clips"].values())
assert statuses == ["approved", "approved", "rejected"], statuses  # score 6 is below min_score 7

# Save preview frames for a visual check
for c in state["clips"].values():
    for t in (1, 20):
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
                                                 "best_slots": ["12:00", "19:00"]}))
os.environ["DRY_RUN"] = "1"
jobs.DRY_RUN = True
jobs.learn(client=learn_claude)
assert (TMP / "data" / "playbook.md").read_text().startswith("- Police")
assert jobs._slots(cfg, 2) == ["12:00", "19:00"]
# Playbook now feeds the judge prompt
highlights.judge(cfg, vod, [{"id": 0, "start": 0, "end": 120, "spike": 5, "text": "x"}], client=claude)
assert "Police chases win" in claude.prompts[-1]
print("learn loop ok")
print("ALL TESTS PASSED")
print("artifacts in", TMP)
