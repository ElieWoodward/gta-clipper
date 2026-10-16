# Build task: posting queue, email packages, TikTok check

Paste everything below the line into a Claude Code session on claude.ai/code with the repo
`ElieWoodward/gta-clipper` selected.

---

You are finishing my clip channel bot in this repo. Read `CLAUDE.md` first; it explains the code, the rules, and the
quirks of this environment (the sandbox can't reach Twitch/TikTok/Gmail, so real tests run on GitHub Actions).

## What I want

I'm going to post clips myself on TikTok and YouTube instead of paying for an auto-poster. The bot should:

1. Keep a small queue of ready-to-post clips (max 5) by watching my streamers live and checking their finished
   streams. Once the queue has 5 clips, it stops looking for new ones so it doesn't spend Claude tokens on clips I
   won't use. As soon as there's room again, it starts looking again.
2. Email me every time it's time to post, with the single best clip in the queue, attached, plus everything I need
   to paste into TikTok and YouTube.
3. Notice on its own when I've posted a clip (by checking my TikTok and YouTube pages) and take it out of the queue.
4. Keep the queue fresh. Being relevant matters more than having a backlog, so old clips expire.
5. Run entirely in the cloud on GitHub Actions so my computer can be off, and never fail silently.

## Detailed spec

### Queue

- The queue is every clip with status `approved` that I haven't posted yet and hasn't expired. Store it in the
  existing state (`data/state.json`, plus `data/live.json` for live-watcher clips; keep using `common.load_state()`
  which merges them). Each queued clip remembers: id, hook, score, streamer, source link + timestamp, created time,
  when its email was sent (if it was), the video file location, and the captions/tags you generated for it.
- Size limit: `queue.max_size` in `config.yaml`, default 5.
- Expiry: `queue.max_age_hours`, default 48. Expired clips leave the queue (mark them `expired`, keep the record).
- Only clips scoring at least `clip.min_score` (currently 7) enter the queue.
- When the queue is full:
  - the live watcher (`clipper/live.py`) keeps running cheap checks only (is anyone live, chat rate), makes no Claude
    calls and cuts nothing;
  - the VOD `process` job skips without downloading anything.
- When there's room, the VOD job asks the judge for at most `max_size - queue_size` clips, and the live watcher works
  as it does now. Prefer live clips (fresher) over VOD clips when both are available.
- The video file has to survive between runs. Upload each queued clip as an asset on a GitHub Release named `queue`
  in this repo (create it if missing), and delete the asset when the clip leaves the queue, so it doesn't grow
  forever. Store the asset URL on the clip.

### Posting emails

- Send at the existing posting slots (`posting.slots` and the `posting.ramp` per-day count; `start_date` stays
  2026-10-16). The hourly `post` job becomes "send the email if a slot is due" instead of calling Blotato. Keep the
  Blotato code path, but turn it off behind `posting.mode: email` (new setting; `blotato` keeps the old behavior).
- `jobs._posting_ready()` currently waits for `BLOTATO_API_KEY`. In email mode it should wait for the three email
  secrets instead. Pass the new secrets to both workflows (`clipper.yml` and `live.yml`).
- Which clip: the highest-scoring clip in the queue made since the last email. If there's none, the best clip still
  in the queue. Tie-breaks: webcam visible, then newest. Never email the same clip twice. If the queue is empty,
  send nothing.
- Send with Gmail SMTP from the channel Gmail using an app password (secrets `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`),
  to the address in the secret `NOTIFY_EMAIL`. Never put any email address in the repo. If Google no longer allows
  app passwords for SMTP, tell me and use the simplest working alternative.
- Subject: `Post now: <hook> (<score>/10)`.
- Attach the mp4. Gmail's limit is 25 MB, so if the file is bigger, re-encode a copy for the email to fit under 24 MB
  (keep 1080x1920, lower the bitrate), and also put the Release asset link in the body as a backup download.
- Body, in this order, each piece in its own clearly separated block so I can long-press and copy it on my phone:
  1. TikTok caption: the casual caption line, then `Streamer: <name>`, then the hashtags (existing `_caption()`
     logic for tiktok).
  2. YouTube title: under 100 characters, includes `#shorts`.
  3. YouTube description: the caption, the streamer credit with a link to their Twitch, then the hashtags.
  4. What's in the clip: one line, plus score and length.
  5. Source: the Twitch VOD link with the timestamp, so I can check it.
  6. What's still in the queue: one line per clip (hook, score, age).
- Also send a plain-text version of the same email.

### Detecting that I posted

- My accounts: TikTok `@wantedlevelclips8`, YouTube `@wantedlevelclips8`. Put both handles in `config.yaml` under
  `accounts`.
- Every hour (as part of the `post` job, before deciding what to email), read my latest uploads on both:
  - TikTok: try `yt-dlp --flat-playlist -J https://www.tiktok.com/@wantedlevelclips8` (description, duration,
    timestamp). If TikTok blocks it from GitHub's servers, try once more with a short delay, then fall back to the
    email check below. Don't add paid scraping services.
  - YouTube: the channel's public RSS feed (`https://www.youtube.com/feeds/videos.xml?channel_id=<id>`; look up the
    channel id once from the handle and save it in state).
- A queued clip counts as posted when an upload's description or title closely matches the caption or title we sent
  for it (normalize case, emoji and hashtags; use a similarity score, threshold 0.6) and the upload is newer than the
  email. Duration within 1.5 seconds is extra evidence. Record which platform(s) and the post URL.
- Fallback that always works: I can reply to the posting email with `posted` or `skip`. Read replies over IMAP with
  the same app password and apply them.
- A clip leaves the queue once it's posted on TikTok (YouTube is recorded but not required), skipped, or expired.

### Spending limits

- Add a daily Claude spend cap, `limits.claude_usd_per_day`, default 1.00. Record the token usage from every
  Anthropic response (input and output tokens times the model's current price; look the price up, don't guess) in
  state. When the cap is hit, the live watcher and process job stop making Claude calls until the next day and say
  so in the run notices.
- Keep the existing cheap filters (chat spike, viewer clips, GTA category, cooldowns) in front of every Claude call.

### Never failing silently

- If a scheduled job fails twice in a row, or nothing has been emailed for 36 hours while the queue isn't empty,
  send me one alert email that says what broke in one sentence, and how to fix it if I have to do something. Don't
  send the same alert more than once a day.
- `preflight` checks the email login (SMTP and IMAP) and that the `queue` release can be written, when posting mode
  is email.

## How to work

1. Read `CLAUDE.md`, `config.yaml`, `clipper/jobs.py`, `clipper/live.py`, `clipper/common.py` and
   `tests/test_pipeline.py` before changing anything.
2. Make a task list. Build in small steps, and after each one extend `tests/test_pipeline.py` with offline tests that
   fake the network (SMTP, IMAP, yt-dlp output, RSS, GitHub Releases) and print a new section marker. Cover at least:
   the queue filling to 5 and stopping, expiry, picking the best clip since the last email, never emailing the same
   clip twice, matching a TikTok upload to a queued clip (and not matching an unrelated one), the `posted`/`skip`
   email replies, the spend cap, the email staying under 24 MB, and the alert rules.
3. Never push unless the full offline test passes.
4. Then prove it on GitHub Actions, following `.claude/commands/verify.md`. Add a test mode for the email path:
   `test_source: email` sends one real posting email to `NOTIFY_EMAIL` built from a fresh test clip. That's the only
   email you may send while testing. Ask me before sending it, and only after the three secrets exist.
5. Check things yourself. Don't trust that a run "succeeded": read the annotations, and look at the preview images
   from the `test-previews` branch.
6. If something fails, fix it and rerun the whole check, not just the part that failed.

## Things only I can do (ask me, give numbered click-by-click steps)

- Turn on 2-Step Verification for the channel Gmail and create an app password.
- Add the secrets `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and `NOTIFY_EMAIL` in the repo (Settings, Secrets and
  variables, Actions).
- Approve the one test email.

Keep working on everything else while you wait for me.

## Definition of done

All of these are true, and you've shown me the evidence:

- The offline test passes with the new section markers.
- `preflight` passes on Actions, including the email and release checks.
- A real test email arrived with the clip attached, all six blocks filled in, and under 25 MB. Ask me to confirm.
- A `process` test run on `twitch:xqc` and a `live:` test run on a channel that's live on GTA both produce clean clips
  (you looked at the previews).
- The scheduled workflows have run on their own for at least 3 hours with no failures, and the queue state in
  `data/` looks right.
- `README.md` describes the new email flow in plain words, and `config.yaml` comments explain every new setting.

## Staying on it

If I started you with `/loop`, use each pass to check the scheduled runs since the last pass (`/status`), fix
anything that failed, and rerun `/verify` after any fix. When everything in the definition of done is true and three
passes in a row find nothing to fix, tell me it's finished.

When you report back, be short and plain: what's working, what you need from me as numbered steps, and anything you
weren't able to verify.
