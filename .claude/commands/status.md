---
description: Quick health check of the running bot (recent runs, queue, last email, failures)
---
Give Elie a short status report. Do not change anything unless a run is failing; if one is, say what failed and fix it.

1. `git pull` and read `data/state.json` and `data/live.json` (merged the way `common.load_state()` does).
2. List the posting queue: each clip's hook, score, age, streamer, whether its email was sent, and whether it has
   been seen on TikTok or YouTube.
3. List the last 10 GitHub Actions runs of both workflows with their conclusion. For any failure, read its
   annotations and explain the cause in one line.
4. Show when the next posting email is due and roughly how much Claude spend the last 24 hours used (from the spend
   log in state if present).
5. Keep it under 15 lines.
