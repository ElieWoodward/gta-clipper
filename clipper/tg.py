"""Telegram: messages to Elie's phone, with tap buttons for decisions (Join / Skip, Fix / Skip).

Secrets (in the Mac's .env, never in the repo): TELEGRAM_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (Elie's own
id from @userinfobot). The bot only ever talks to that one chat and ignores anyone else who finds it.
No Claude calls here: buttons carry short codes like "camp:join:<id>" that mac.py acts on.
"""
import json
import os

import requests

from .common import DATA, log, now_utc

API = "https://api.telegram.org/bot{token}/{method}"
STATE = DATA / "telegram.json"   # last update id read, so a restart never replays old taps
MAX_TEXT = 4000                  # Telegram's limit is 4096


def configured():
    return bool(os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _call(method, data=None, files=None, timeout=30):
    url = API.format(token=os.environ["TELEGRAM_TOKEN"], method=method)
    r = requests.post(url, data=data, files=files, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed ({r.status_code}): {body.get('description') or r.text[:200]}")
    return body["result"]


def buttons(rows):
    """[[("Join", "camp:join:x"), ("Skip", "camp:skip:x")]] -> Telegram inline keyboard JSON."""
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in rows]})


def send(text, rows=None, silent=False):
    """Send a message to Elie. Returns the Telegram message id, or None when Telegram isn't set up / dry run."""
    if not configured() or os.environ.get("DRY_RUN") == "1":
        log.info("[telegram off] %s", text[:200])
        return None
    data = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:MAX_TEXT], "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false"}
    if rows:
        data["reply_markup"] = buttons(rows)
    return _call("sendMessage", data)["message_id"]


def send_video(path, caption, rows=None):
    """Send a clip (Telegram bots can send up to 50 MB). Falls back to text if the file is too big."""
    if not configured() or os.environ.get("DRY_RUN") == "1":
        log.info("[telegram off] video %s: %s", path, caption[:120])
        return None
    if os.path.getsize(path) > 49 * 1024 * 1024:
        return send(caption + "\n\n(The video is over Telegram's 50 MB limit; it's on the Mac.)", rows)
    data = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": caption[:1000], "supports_streaming": "true"}
    if rows:
        data["reply_markup"] = buttons(rows)
    with open(path, "rb") as f:
        return _call("sendVideo", data, files={"video": f}, timeout=300)["message_id"]


def edit(message_id, text):
    """Replace a message's text and remove its buttons (after a tap, so it can't be tapped twice)."""
    if not configured() or not message_id:
        return
    try:
        _call("editMessageText", {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "message_id": message_id,
                                  "text": text[:MAX_TEXT], "disable_web_page_preview": "true"})
    except Exception as e:
        log.info("couldn't edit telegram message: %s", e)


def _load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"offset": 0}


def poll(wait=0):
    """New taps and texts from Elie: [("tap", data, message_id, message_text) | ("text", text, message_id, "")].
    Anything from another chat is ignored."""
    if not configured():
        return []
    st = _load()
    res = _call("getUpdates", {"offset": st.get("offset", 0), "timeout": wait,
                               "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=wait + 30)
    me = str(os.environ["TELEGRAM_CHAT_ID"])
    out = []
    for u in res:
        st["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            q = u["callback_query"]
            try:
                _call("answerCallbackQuery", {"callback_query_id": q["id"]})
            except Exception:
                pass
            if str((q.get("from") or {}).get("id")) != me:
                continue
            msg = q.get("message") or {}
            out.append(("tap", q.get("data") or "", msg.get("message_id"), msg.get("text") or msg.get("caption") or ""))
        elif "message" in u:
            m = u["message"]
            if str((m.get("chat") or {}).get("id")) != me:
                continue
            out.append(("text", (m.get("text") or "").strip(), m.get("message_id"), ""))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    st["polled"] = now_utc().isoformat()
    STATE.write_text(json.dumps(st))
    return out


def problem(state, key, what, fix):
    """One message per problem per day: what happened and exactly how to fix it."""
    today = now_utc().date().isoformat()
    seen = state.setdefault("tg_alerts", {})
    if seen.get(key) == today:
        return False
    try:
        send(f"⚠️ Clip bot problem\n\nWhat happened: {what}\n\nHow to fix it:\n{fix}\n\n"
             "(You'll get at most one message a day about this.)")
    except Exception as e:
        log.warning("couldn't send the problem message: %s", e)
        return False
    seen[key] = today
    return True
