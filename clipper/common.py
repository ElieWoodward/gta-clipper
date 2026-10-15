"""Shared helpers: config, state file, paths, logging, time."""
import json
import logging
import os
import subprocess
from datetime import datetime, date, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(os.environ.get("CLIPPER_ROOT", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"
WORK = Path(os.environ.get("CLIPPER_WORK", ROOT / "work"))
STATE_FILE = DATA / "state.json"
PLAYBOOK_FILE = DATA / "playbook.md"
INSIGHTS_FILE = DATA / "insights.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clipper")


def load_config():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_vods": [], "clips": {}, "posts": [], "pruned": []}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def load_json(path, default):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def now_utc():
    return datetime.now(timezone.utc)


def now_local(cfg):
    return datetime.now(ZoneInfo(cfg.get("timezone", "UTC")))


def days_since_start(cfg):
    start = date.fromisoformat(str(cfg["start_date"]))
    return (now_local(cfg).date() - start).days


def run(cmd, **kw):
    """Run a command, raise with stderr on failure."""
    log.debug("run: %s", " ".join(map(str, cmd)))
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {p.stderr[-2000:]}")
    return p.stdout


def require_env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing secret {name}. Add it under repo Settings > Secrets > Actions.")
    return v
