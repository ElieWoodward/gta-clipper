---
description: Test live clipping on a channel that is live on GTA right now
---
Test the live watcher on the channel `$ARGUMENTS` (use `xqc` if that is empty).

1. Dispatch the `live` job on `clipper.yml` with `test_source` `live:<channel>`. If that channel isn't live on
   GTA, pick the most-watched channel that is (Twitch GQL `game(name: "Grand Theft Auto V") { streams(first: 15) }`,
   run from a GitHub Actions job or a browser on twitch.tv, since this sandbox can't reach Twitch) and say which.
2. Poll until it finishes (about 15 minutes). Read the annotations: it must show "watching", a "big moment" line,
   and a "live moment ... score" line.
3. Fetch `test-previews` and look at the clip's preview. Report how many minutes passed between the moment and the
   finished clip, the score, and whether the render is clean.
