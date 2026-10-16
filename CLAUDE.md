# gta-clipper: notes for Claude Code

This repo is Elie's clip channel bot ("Wanted Level", @wantedlevelclips8 on TikTok and YouTube). It finds funny or
big moments in GTA RP Twitch streams, cuts them into vertical clips with the streamer's webcam on top, a hook and
captions, and gets them posted. Everything runs on GitHub Actions so Elie's computer can stay off.

Read `docs/BUILD_PROMPT.md` for the current build task and its definition of done.

## How it is laid out

- `clipper/` is the Python package. Entry point: `python -m clipper <job>`.
  - `jobs.py`: process (full VODs), post, analytics, learn, test runs, `make_clip`, `post_clip`.
  - `live.py`: live watcher (chat spikes + viewer clips on a stream that is still going).
  - `highlights.py`: candidate moments, transcripts, Claude judge and hook editor (tool-call answers, retried).
  - `render.py`: ffmpeg render (fill, wide, cam+fill, cam+wide layouts; hook box; captions; punchline).
  - `vision.py`: Claude looks at frames to find the webcam and the action.
  - `twitch.py`: Twitch GQL (VOD list, chapters, viewer clips, chat replay). `fetch.py`: yt-dlp downloads.
  - `preflight.py`: one-minute check of every service before any long run.
  - `common.py`: config, state. `data/state.json` is the main state; `data/live.json` is written only by the live
    watcher and merged in on load.
- `.github/workflows/clipper.yml`: scheduled process, post, analytics, learn, plus manual runs and test runs.
- `.github/workflows/live.yml`: live watcher every 20 minutes, own concurrency group.
- `config.yaml`: every setting Elie can change. `tests/test_pipeline.py`: offline end-to-end test, no keys needed.

## Rules

- Never post anything to TikTok or YouTube yourself, and never send email to anyone except through the code paths
  Elie asked for (the posting-package email to the address in the `NOTIFY_EMAIL` secret).
- Never write secrets, API keys or email addresses into the repo. The repo is public.
- Run `python tests/test_pipeline.py` before every push. Only push when it prints `ALL TESTS PASSED` and every
  section marker (`PREVIEWS OK`, `CHAPTERS OK`, `LISTING OK`, `LIVE OK`, plus any you add).
- Commit messages end with the attribution lines the session gives you.
- Keep Claude API spend low. Observed costs: about $0.15 to $0.25 per full 12-hour VOD run, about $0.02 per live
  moment judged. Do not add calls without a reason; prefer cheap filters before any Claude call.
- Elie wants plain, practical replies with numbered click-by-click steps when he has to do something.

## Things about this environment that will bite you

- This cloud sandbox usually cannot reach Twitch, TikTok, YouTube or Gmail directly (the proxy returns 403).
  Test anything that touches them by dispatching a GitHub Actions run, not from the sandbox.
- Dispatch a run: `POST https://api.github.com/repos/ElieWoodward/gta-clipper/actions/workflows/clipper.yml/dispatches`
  with `{"ref":"main","inputs":{"job":"<job>","test_source":"<source>"}}`. Poll
  `.../actions/runs?per_page=1` for status.
- Downloading run logs is blocked here. Read results from check-run annotations
  (`/check-runs/<job_id>/annotations`): print `::notice::` / `::warning::` / `::error::` lines from the code.
  GitHub shows at most 10 notices per step, so put detailed results in a file instead.
- Test runs (`test_source` set) post nothing (DRY_RUN) and publish `summary.json` plus contact-sheet previews to the
  `test-previews` branch. Fetch it with `git fetch origin test-previews` and look at the JPGs before calling a
  render good.
- `test_source` values: a VOD URL, `twitch:<channel>` (latest GTA VOD), or `live:<channel>` with job `live`
  (watches for 10 minutes, then clips the liveliest moment).
- The Twitch "failed integrity check" warning on one viewer-clips query shape is normal; the code falls back to the
  other shape.
- Big streamers start in Just Chatting and switch to GTA later; `twitch.chapters()` handles that. Streams run 12 to
  14 hours; `max_vod_hours` is 16.
