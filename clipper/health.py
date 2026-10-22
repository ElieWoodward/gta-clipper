"""Never fail silently: count failures per job and email one alert a day per problem."""
import os
from datetime import datetime, timedelta

from .common import log, now_utc


def _keys():
    live = os.environ.get("CLIPPER_LIVE") == "1"
    return ("health_live", "alerts_live") if live else ("health", "alerts")


def record(state, job, error=None):
    """Track consecutive failures of a job. Returns how many in a row (0 after a success)."""
    hkey, _ = _keys()
    h = state.setdefault(hkey, {}).setdefault(job, {"fails": 0})
    if error is None:
        h.update(fails=0, last_ok=now_utc().isoformat())
    else:
        h.update(fails=h.get("fails", 0) + 1, last_error=str(error)[:300], last_fail=now_utc().isoformat())
    return h["fails"]


def alert(state, key, subject, text):
    """Email one alert per problem per day (if email is set up)."""
    from . import mail
    today = now_utc().date().isoformat()
    _, akey = _keys()
    if state.get("alerts", {}).get(key) == today or state.get("alerts_live", {}).get(key) == today:
        return False
    print(f"::warning::{subject}", flush=True)
    if not mail.configured() or os.environ.get("DRY_RUN") == "1" or mail.down_until():
        return False
    try:
        mail.send(f"Clip bot problem: {subject}", text + "\n\nYou'll get at most one email a day about this.")
    except Exception as e:
        log.warning("couldn't send alert: %s", e)
        return False
    state.setdefault(akey, {})[key] = today
    return True


FIXES = {
    "GMAIL": "Check the GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets. If you changed the Gmail password, make a new app "
             "password (myaccount.google.com > Security > App passwords) and paste it into the secret.",
    "ANTHROPIC": "Check the ANTHROPIC_API_KEY secret and that the Anthropic account has credit "
                 "(console.anthropic.com > Settings > Billing).",
    "GROQ": "Check the GROQ_API_KEY secret (console.groq.com > API Keys).",
    "credit": "The Anthropic account may be out of credit: console.anthropic.com > Settings > Billing.",
}


def job_failed(state, job, error):
    n = record(state, job, error)
    if n >= 2:
        msg = str(error)
        fix = next((v for k, v in FIXES.items() if k.lower() in msg.lower()), "Nothing for you to do yet; Claude Code "
                   "can look at it (open the repo in claude.ai/code and run /status).")
        alert(state, f"fail:{job}", f"the {job} job failed {n} times in a row",
              f"What broke: {msg[:400]}\n\nWhat to do: {fix}")


def check_quiet(cfg, state, queue_len):
    """Alert if clips are waiting but nothing has been emailed for 36 hours."""
    from .clipqueue import last_emailed
    last = last_emailed(state)
    if not queue_len or not last:
        return
    if now_utc() - datetime.fromisoformat(last) > timedelta(hours=36):
        alert(state, "quiet", "no posting email for 36 hours",
              f"{queue_len} clip(s) are waiting but no email has gone out since {last[:16]} UTC. The post job may be "
              "failing; check the Actions tab, or open the repo in claude.ai/code and run /status.")
