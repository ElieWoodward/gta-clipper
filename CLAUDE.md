# gta-clipper: notes for Claude Code

This repo is Elie's clip channel bot ("Wanted Level", @wantedlevelclips8 on TikTok and YouTube). It finds funny or
big moments in GTA RP Twitch streams, cuts them into vertical clips with the streamer's webcam on top, a hook and
captions, and gets them posted. Everything runs on GitHub Actions so Elie's computer can stay off.

The email posting mode described in `docs/BUILD_PROMPT.md` is built (`clipper/clipqueue.py`, `clipper/mail.py`,
`clipper/health.py`). Use that file's definition of done when checking or extending it.

## How it is laid out

- `clipper/` is the Python package. Entry point: `python -m clipper <job>`.
  - `jobs.py`: process (full VODs), post, analytics, learn, test runs, `make_clip`, `post_clip`.
  - `live.py`: live watcher (chat spikes + viewer clips on a stream that is still going).
  - `highlights.py`: candidate moments, transcripts, Claude judge and hook editor (tool-call answers, retried).
  - `render.py`: ffmpeg render (fill, wide, cam+fill, cam+wide layouts; hook box; captions; punchline).
  - `vision.py`: Claude looks at frames to find the webcam and the action.
  - `twitch.py`: Twitch GQL (VOD list, chapters, viewer clips, chat replay). `fetch.py`: yt-dlp downloads.
  - `preflight.py`: one-minute check of every service before any long run.
  - `clipqueue.py`: the queue (max size, expiry), the posting-email package, TikTok/YouTube posted detection.
  - `mail.py`: Gmail SMTP/IMAP with an app password. `health.py`: failure counting and alert emails.
  - `campaigns.py`: daily scan of ClippingAlpha's campaign directory (no Claude calls). Emails campaigns that pay for
    a Twitch streamer with recent GTA streams; reply "join" adds them to the clipped streamers (via state, merged in
    `load_config`); posting emails for their clips carry the claim link, and a "Claim" email goes out once posted.
  - `batch.py`: full-stream judging through the half-price Message Batches API. `process` hands off and exits;
    a later hourly `process` run finishes it (`jobs.finish_batches`); falls back to a normal call after 6h.
  - `timing.py`: posting-time experiment (candidate times, views 3h after posting, explore/exploit plan per day).
  - `launch.py`: GTA 6 launch day (`launch.date`, Nov 19 2026). `apply()` runs inside `load_config` and, from the
    launch date, multiplies the ramp/queue (x3 with Blotato checked ok in `state["launch"]["blotato_ok"]`, else x2),
    spreads posting times hourly, and adds the biggest English GTA 6 streams to the live watcher. `daily()` (from
    the post job) sends the week-before setup email, the Blotato confirmation and the "launch mode is on" email.
    `jobs.deliver` posts through Blotato in launch mode and falls back to the email. Job `launch_check` reports it.
    Launch days use `timing.best_times` (exploit only). `refresh_streamers` (analytics job) re-picks extra
    streamers daily into `state["launch"]["streamers"]`. Claude cap: `launch.daily_cap` ($1, $2 from 3 posts/day,
    `launch.claude_usd_per_day` from launch).
  - `common.py`: config, state. `data/state.json` is the main state; `data/live.json` is written only by the live
    watcher and merged in on load.
- `.github/workflows/clipper.yml`: post every 15 min, process hourly, analytics daily, learn Mondays, plus manual
  runs and test runs. All share one concurrency group, so a long run delays the others.
- `.github/workflows/live.yml`: live watcher, own concurrency group, writes only `data/live.json`. It runs for
  ~6h, then dispatches itself again (`live.Heartbeat.hand_over`); its cron at :07/:27/:47 is only a backup.
  GitHub skips many scheduled runs, so the watcher is also the heartbeat: it dispatches clipper.yml `post` every
  15 min and `process` hourly, and catches up `analytics`/`learn` from `state["last_run"]`.
- `config.yaml`: every setting Elie can change. `tests/test_pipeline.py`: offline end-to-end test, no keys needed.

## Rules

- Never post anything to TikTok or YouTube yourself, and never send email to anyone except through the code paths
  Elie asked for (the posting-package email to the address in the `NOTIFY_EMAIL` secret).
- Never write secrets, API keys or email addresses into the repo. The repo is public.
- Run `python tests/test_pipeline.py` before every push. Only push when it prints `ALL TESTS PASSED` and every
  section marker (`PREVIEWS OK`, `CHAPTERS OK`, `LISTING OK`, `LIVE OK`, plus any you add).
- Commit messages end with the attribution lines the session gives you.
- Keep Claude API spend low. Observed costs: about $0.05 to $0.10 per full 12-hour VOD with batch judging, about
  $0.02 per live moment judged. Do not add calls without a reason; prefer cheap filters before any Claude call.
  Picks below `clip.min_score` never get the hook-polish call (`highlights.keepers`).
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
- `test_source` values: a VOD URL, `twitch:<channel>` (latest GTA VOD), `live:<channel>` or `live:auto` with job
  `live` (watches for 10 minutes, then clips the liveliest moment), or `email` with job `post` (sends ONE real test
  email to NOTIFY_EMAIL; ask Elie before running it).
- TikTok's page needs yt-dlp's impersonation add-on (`yt-dlp[curl-cffi]` in requirements). An account with no
  videos makes yt-dlp error; that's treated as an empty list.
- The Twitch "failed integrity check" warning on one viewer-clips query shape is normal; the code falls back to the
  other shape.
- GitHub shows at most 10 notices per step. Scheduled runs print a single "preflight ok" line; the manual
  `preflight` job prints every check.
- Twitch GQL sometimes answers "service error" for `user.videos`; the code falls back to yt-dlp.
- Big streamers start in Just Chatting and switch to GTA later; `twitch.chapters()` handles that. Streams run 12 to
  14 hours; `max_vod_hours` is 16.
