# gta-clipper

Finds the best moments in GTA RP Twitch streams, turns them into vertical clips (webcam on top, hook, captions), and
emails you one ready-to-post clip at each posting time. Everything runs on GitHub Actions, so your computer can be off.

## How it works (email mode, the default)

1. **It keeps a small queue.** It watches your streamers live (chat blowing up, viewers clipping the same moment) and
   checks their finished streams every 3 hours. Clips scoring 7/10 or more go into the queue. Once 5 are waiting it
   stops looking, so it isn't spending Claude money on clips you won't use. Clips older than 48 hours drop out.
2. **It emails you at each posting time** (11:00 Pacific in week 1, more slots later) with the best clip made since
   the last email, attached, plus blocks you can copy straight into TikTok and YouTube: TikTok caption, YouTube title and
   description, what's in the clip, the source link, and what's still waiting. A really big live moment
   (8/10 or more) is emailed right away, once a day at most on top of the normal emails.
3. **It notices when you post.** Every hour it reads your TikTok and YouTube pages and takes posted clips out of the
   queue. You can also reply to the email with `posted` or `skip`.
4. **It watches its own health.** Claude spend is capped per day. If a job fails twice in a row, or clips are waiting
   but nothing has been emailed for 36 hours, you get one alert email saying what broke.

## What runs when

| Workflow | When | What it does |
|---|---|---|
| clipper: process | every 3 hours | Checks finished streams for clips while the queue has room |
| clipper: post | hourly | Checks TikTok/YouTube/replies, expires old clips, emails a clip when a slot is due |
| clipper: analytics | daily | Dashboard issue |
| clipper: learn | Mondays | Claude studies what worked and updates `data/playbook.md` |
| live | every 20 minutes | Watches streamers who are live on GTA and clips big moments within minutes |

## Settings

Edit `config.yaml` on GitHub. The main ones: `streamers`, `posting.slots` and `posting.ramp` (when emails go out),
`queue.max_size` and `queue.max_age_hours`, `accounts` (your TikTok and YouTube handles), `limits.claude_usd_per_day`,
and `live` (how sensitive the live watcher is). Set `posting.mode: blotato` to have it post for you through Blotato
instead.

## Secrets (repo Settings > Secrets and variables > Actions)

- `ANTHROPIC_API_KEY`, `GROQ_API_KEY`: needed for everything.
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`: the Gmail that sends, its app password, and where emails go.
  Until these exist, the scheduled jobs leave a note on the run page and do nothing.
- `BLOTATO_API_KEY`: only for `posting.mode: blotato`.

## Testing

- Offline, no keys needed: `python tests/test_pipeline.py`
- On GitHub: Actions > clipper > Run workflow. `preflight` checks every service in about a minute. A `test_source`
  makes clips without posting or emailing: a VOD link, `twitch:<channel>`, or `live:<channel>` / `live:auto` with the
  `live` job. `email` with the `post` job sends you one real test email.
- Previews of every test clip are published to the `test-previews` branch.

Working on it with Claude Code: see `CLAUDE.md` and the `/verify`, `/status` and `/test-live` commands.
