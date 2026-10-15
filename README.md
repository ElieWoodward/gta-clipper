# gta-clipper

Turns GTA streamer VODs into 61-90 second vertical clips and posts them to TikTok and YouTube Shorts on a schedule, with no one at the keyboard. Runs entirely on GitHub Actions.

**What runs when** (all automatic, see `.github/workflows/clipper.yml`):

| Job | When | What it does |
|---|---|---|
| process | every 3 hours | Checks your streamers for new VODs, finds loud moments, transcribes them, has Claude pick the best and write a hook + context captions, renders 1080x1920 clips, opens a GitHub issue per clip |
| post | hourly | Posts the best ready clip to each platform whenever a daily slot is due, ramping from 1/day up to 4/day |
| analytics | daily | Pulls views for every post, flags/hides low performers, updates the pinned **Clip dashboard** issue |
| learn | Mondays | Claude studies what got views, rewrites `data/playbook.md` (which the picker follows) and shifts posting times toward what works |

**Change settings** by editing `config.yaml` on GitHub (streamers, clip length, posts per day, review mode).

**Run something now:** Actions tab > clipper > Run workflow > pick a job. Tick "force" to ignore the launch date when testing.

**Secrets needed** (Settings > Secrets and variables > Actions): `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `BLOTATO_API_KEY`. Optional for auto-hiding YouTube flops: `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.

Test locally without any keys: `python tests/test_pipeline.py`
