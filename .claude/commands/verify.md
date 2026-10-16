---
description: Run the offline tests, push, then prove it works on GitHub Actions (preflight plus a real test run)
---
Verify the clipper end to end. Do every step; stop and fix on the first failure, then start over from step 1.

1. Run `python tests/test_pipeline.py`. It must print `ALL TESTS PASSED` and every section marker. Run
   `python -m pyflakes clipper/*.py` and fix anything that isn't an unused import.
2. Commit and push to main if there are changes.
3. Dispatch the `preflight` job with `test_source` `twitch:xqc`. Poll until it finishes. Every preflight line in the
   annotations must say `ok`.
4. Dispatch the `process` job with `test_source` set to `$ARGUMENTS` (use `twitch:xqc` if that is empty). Poll every 60 seconds until it
   finishes (12-hour streams take about 20 minutes). Read the annotations, then `git fetch origin test-previews` and
   look at every preview JPG yourself: webcam fully visible with heads not cut off, hook box readable, captions
   matching speech, no black bars or doubled webcam, punchline only where it is said.
5. Report in plain words: what passed, what each clip is, its score, and anything that looks wrong.
