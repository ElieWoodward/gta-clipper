# gta-clipper

Finds the best moments in GTA RP Twitch streams, turns them into vertical clips (webcam on top, hook, captions), and
emails you a ready-to-post clip at each posting time. Everything runs on GitHub Actions, so your computer can be off.

## How it works

1. **It watches streamers.** Your streamers in `config.yaml` (xQc, Cinna and vanillamace to start), plus any streamer
   who has a paid clipping campaign and plays GTA. It only clips the GTA part of a stream.
   - **Live:** while someone is live on GTA it reads chat and the clips viewers make. When chat blows up or several
     people clip the same moment, it cuts that moment within a few minutes.
   - **After the stream:** every hour it checks for finished streams and finds the best moments. Claude judges these at
     half price through Anthropic's batch API, so those clips show up an hour or so after the stream ends.
2. **It keeps a small queue.** Clips scoring 7/10 or more wait in a queue of up to 8. When the queue is full it stops
   spending Claude money until there's room. Clips older than 48 hours drop out, because fresh beats a backlog.
3. **It emails you at each posting time** with the best clip in the queue attached, plus blocks to copy into TikTok and
   YouTube: TikTok caption, YouTube title, YouTube description, what's in it, the source link, and what's still waiting.
   - 1 email a day for the first 3 days, 2 a day from day 3, 3 from day 7, 4 from day 14.
   - The times rotate through an experiment (8am to 10pm Pacific) that scores each time by TikTok views 3 hours after
     posting, then favors the winners while one slot keeps testing.
   - A big live moment (8/10 or more) is emailed right away, at most one extra a day.
4. **It notices when you post.** Every 15 minutes it reads your TikTok and YouTube, takes posted clips out of the
   queue, and records their views and likes.
5. **It learns.** Every Monday Claude studies which clips did best and updates the rules the clip picker follows. You
   get an email with your best and worst clips and what changed.
6. **It looks for paid campaigns.** Every morning it scans the clipping-campaign boards (Content Rewards and others).
   If one pays for clips of a streamer who plays GTA, it starts clipping them right away and emails you the rate, the
   rules and how to join. Once you've joined, reply `join`: their clips jump the queue, and you get a "Claim" email
   with the exact link to submit after you post one.
7. **It watches its own health.** Claude spend is capped at $1 a day. If a job fails twice in a row, or clips are
   waiting but nothing has been emailed for 36 hours, you get one alert email saying what broke.

## Weekly long video (YouTube)

Every Sunday morning you get an email with a download link for "GTA RP MOST VIEWED Twitch Clips of The Week! #N":
the week's most-watched GTA clips on Twitch, counted down to #1 by their Twitch views, each with the live chat
replay beside it and a hype meter showing how hard chat went. About 10 minutes, 16:9, with a thumbnail, title,
description, chapters and streamer credits ready to paste, plus upload steps. From GTA 6's launch it becomes
"GTA 6 MOST VIEWED Twitch Clips of The Week!" starting at #1. It copies the numbered weekly series independent
channels run for other games (Rivals Guides started theirs the week Marvel Rivals came out).

## GTA 6 launch day

Nothing to edit on the day. A week before (Nov 12) you get an email with the steps to set up automatic posting
through Blotato. Once the `BLOTATO_API_KEY` secret is added, the bot emails you within 15 minutes to say whether
TikTok and YouTube are both connected. From launch day (Nov 19) it switches on its own:

- 3x the posts per day with automatic posting (2x if posting is still by email), a bigger queue, posting times
  spread over the whole day
- with automatic posting, each clip goes straight to TikTok and YouTube at its time; if Blotato fails, that clip is
  emailed to you instead
- the live watcher also watches the 4 biggest English GTA 6 streams, not only your saved streamers
- posting times switch to the hours that got the most views (then likes) in the time test, with no more testing
- the streamers are re-picked that morning and every morning after: streamers with a paid campaign first (best rate
  first), then the 6 biggest English GTA 6 streamers, on top of your own list. GTA 6 clipping campaigns are emailed
  best-paying first, plus one email listing the top campaigns and who's being clipped. The boards need you to sign
  in, so joining is up to you: reply `join` to a campaign's email once you have.
- the Claude budget goes to $4 a day (it's $1 a day now and $2 a day once posting reaches 3 a day). The week-before
  email includes turning on auto-reload so the credit balance never runs out

If the date moves, reply `launch YYYY-MM-DD` to any bot email. The dashboard shows the countdown. The manual
`launch_check` job shows what launch mode would do right now.

## Replying to the emails

| Reply | What it does |
|---|---|
| `posted` | Takes the clip out of the queue (it also notices on its own when it shows up on your TikTok) |
| `skip` | Drops the clip; the next check sends a replacement so you keep your daily count |
| `next` | Sends another clip right now, on top of the day's count |
| `pause` / `resume` | Stops / restarts the posting emails (clips keep collecting) |
| `launch 2026-12-03` | GTA 6's release date moved: launch mode and its reminder follow the new date |
| `join` (to a Campaign email) | You joined that campaign: its streamer's clips get priority and claim links |

## What runs when

| Workflow | When | What it does |
|---|---|---|
| clipper: post | every 15 minutes | Reads TikTok/YouTube/replies, expires old clips, emails a clip when a slot is due, updates the dashboard |
| clipper: process | hourly | Finishes half-price judging from earlier runs, checks for new finished streams while the queue has room |
| live | always on (restarts itself every ~6h) | Watches streamers who are live on GTA and clips big moments within minutes. It also starts the post and process runs on time, because GitHub often skips scheduled runs |
| clipper: analytics | daily ~7am Pacific | Scans paid clipping campaigns |
| clipper: learn | Mondays | Weekly report and new picker rules |

The pinned **Clip dashboard** issue shows the queue, today's posting times, the streamers, campaigns and spend.

## Settings

Edit `config.yaml` on GitHub. The main ones: `streamers`, `posting.ramp` and `posting.time_test` (when emails go out),
`queue.max_size` and `queue.max_age_hours`, `accounts` (your TikTok and YouTube handles), `limits` (daily Claude
budget, batch judging), and `live` (how sensitive the live watcher is). Set `posting.mode: blotato` to have it post
for you through Blotato instead of emailing you.

## Secrets (repo Settings > Secrets and variables > Actions)

- `ANTHROPIC_API_KEY`, `GROQ_API_KEY`: needed for everything.
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`: the Gmail that sends, its app password, and where emails go.
- `BLOTATO_API_KEY`: only for `posting.mode: blotato`.

## Testing

- Offline, no keys needed: `python tests/test_pipeline.py`
- On GitHub: Actions > clipper > Run workflow. `preflight` checks every service in about a minute. A `test_source`
  makes clips without posting or emailing: a VOD link, `twitch:<channel>`, or `live:<channel>` / `live:auto` with the
  `live` job. `email` with the `post` job sends you one real test email. `check_streamers` with a comma-separated list
  of channels reports how often each streams GTA and whether their VODs are public.
- Previews of every test clip are published to the `test-previews` branch.

Working on it with Claude Code: see `CLAUDE.md` and the `/verify`, `/status` and `/test-live` commands.
