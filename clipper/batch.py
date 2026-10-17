"""Half-price Claude judging for full streams, through Anthropic's Message Batches API.

A full-stream run gets everything ready (transcripts, candidate moments), hands the judging request to a batch, saves
its place in data/pending/, and exits, so it never holds up the posting check. A later run (hourly) picks up the
answer and makes the clips. If a batch hasn't finished after `limits.batch_wait_hours` (or failed), the judging is
done the normal way instead, so nothing gets stuck. Live clips never use this: they need to be fast.
"""
import json
import re
from datetime import datetime, timedelta

from .common import DATA, log, now_utc

PENDING = DATA / "pending"
DISCOUNT = 0.5   # batch requests cost half


def enabled(cfg):
    return bool((cfg.get("limits") or {}).get("batch", True))


def wait_hours(cfg):
    return float((cfg.get("limits") or {}).get("batch_wait_hours", 6))


def _path(vod_id):
    return PENDING / (re.sub(r"[^A-Za-z0-9_-]", "_", vod_id) + ".json")


def submit(client, vod, windows, words, max_picks, params):
    """Send the judge request as a batch and save everything needed to finish the job later."""
    custom_id = re.sub(r"[^A-Za-z0-9_-]", "_", vod["id"])[:60]
    b = client.messages.batches.create(requests=[{"custom_id": custom_id, "params": params}])
    PENDING.mkdir(parents=True, exist_ok=True)
    keep = [w for w in words if any(win["start"] - 1 <= w["s"] <= win["end"] + 1 for win in windows)]
    rec = {"batch_id": b.id, "custom_id": custom_id, "submitted": now_utc().isoformat(), "vod": vod,
           "windows": windows, "words": [[w["w"], round(w["s"], 2), round(w["e"], 2)] for w in keep],
           "max_picks": max_picks, "params": params}
    _path(vod["id"]).write_text(json.dumps(rec))
    print(f"::notice::{vod['streamer']['name']}: judging {len(windows)} moments at half price (batch {b.id}); "
          "clips will be made on a later run", flush=True)
    return rec


def pending():
    return [json.loads(p.read_text()) for p in sorted(PENDING.glob("*.json"))] if PENDING.exists() else []


def drop(rec):
    p = _path(rec["vod"]["id"])
    if p.exists():
        p.unlink()


def words_of(rec):
    return [{"w": w, "s": s, "e": e} for w, s, e in rec["words"]]


def result(client, cfg, rec):
    """('done', judge_data) | ('waiting', None) | ('fallback', reason). Records the batch's (half-price) cost."""
    from . import highlights
    b = client.messages.batches.retrieve(rec["batch_id"])
    age = now_utc() - datetime.fromisoformat(rec["submitted"])
    if getattr(b, "processing_status", None) != "ended":
        if age > timedelta(hours=wait_hours(cfg)):
            try:
                client.messages.batches.cancel(rec["batch_id"])
            except Exception:
                pass
            return "fallback", f"batch still not done after {age.total_seconds() / 3600:.1f}h"
        return "waiting", None
    for entry in client.messages.batches.results(rec["batch_id"]):
        if getattr(entry, "custom_id", None) != rec["custom_id"]:
            continue
        r = entry.result
        if getattr(r, "type", None) != "succeeded":
            return "fallback", f"batch request {getattr(r, 'type', '?')}"
        highlights.record_usage(r.message, discount=DISCOUNT)
        try:
            return "done", highlights.read_tool_reply(r.message)
        except ValueError as e:
            return "fallback", f"unreadable batch answer ({e})"
    return "fallback", "batch had no result for this stream"
