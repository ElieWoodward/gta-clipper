"""Daily scan for paid clipping campaigns that fit the channel (GTA or GTA-streamer content).

Source: ClippingAlpha's public directory, which lists the campaigns on Content Rewards (Whop), Vyro, Clipster and
other boards. For each new campaign that mentions GTA, or that pays for clips of a streamer who has streamed GTA
recently on Twitch, Elie gets one email with the rate, the rules and how to join. When he replies "join", that
streamer is added to the ones the bot clips, and every posting email for their clips says how to claim the money.
No Claude calls: this is plain web reading.
"""
import os
import re
from html.parser import HTMLParser

import requests

from .common import log, now_utc

BASE = "https://clippingalpha.com"
UA = {"User-Agent": "Mozilla/5.0 (compatible; wanted-level-clipper/1.0)"}
GTA_RE = re.compile(r"\bgta\b|grand theft|nopixel|no pixel|\brp\b|roleplay|role play|los santos|vice city|leonida|"
                    r"red dead|rdr2?\b|fivem", re.I)
STREAMER_RE = re.compile(r"stream|twitch|kick|\bvods?\b|\blive\b", re.I)
NOISE = re.compile(r"\$[\d,.]+k?|\b(clipping|clips?|campaign|streamer|stream|twitch|kick|official|viral|dedicated|"
                   r"highlights?|vods?|cuts?|daily|weekly|program|army|rewards?|content|the)\b|\[.*?\]|\(.*?\)|[^\w\s]",
                   re.I)
MAX_EMAILS_PER_SCAN = 3


class _Cards(HTMLParser):
    """Collects each <article> card's text and its first /campaigns/ link."""

    def __init__(self):
        super().__init__()
        self.cards, self._depth, self._text, self._href = [], 0, [], None

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self._depth += 1
            if self._depth == 1:
                self._text, self._href = [], None
        elif tag == "a" and self._depth and self._href is None:
            href = dict(attrs).get("href") or ""
            if href.startswith("/campaigns/") and href.count("/") >= 3:
                self._href = href

    def handle_endtag(self, tag):
        if tag == "article" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.cards.append((" ".join(" ".join(self._text).split()), self._href))

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)


def _get(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def parse_card(text, href):
    rate = re.search(r"\$([\d.]+) per 1K views", text)
    left = re.search(r"\$([\d.,]+K?) of \$([\d.,]+K?) left", text)
    plats = re.search(r"Platforms ([A-Za-z, ]+?)(?:CLIPPING|MUSIC|UGC|LOGO|CPM|$)", text)
    name = re.sub(r"^.*?(?:Board: )?\S+?(?:LOW|FAIR|SOLID|ALPHA|SCORING)\d*", "", text.split("Platforms")[0]).strip()
    name = re.sub(r"^(Footage after you join|Brief after you join|Application required|After joining)\.?\s*", "",
                  name, flags=re.I).strip() or text.split("Platforms")[0][-80:].strip()
    return {"id": href.strip("/").replace("campaigns/", "").replace("/", "__"), "url": BASE + href, "name": name[:120],
            "rate": float(rate.group(1)) if rate else None, "left": left.group(1) if left else None,
            "budget": left.group(2) if left else None, "platforms": plats.group(1).strip() if plats else "",
            "kind": next((k for k in ("MUSIC", "UGC", "LOGO", "CLIPPING") if k in text), ""), "text": text[:400]}


def list_campaigns(max_pages=25):
    """Every live campaign in the directory (about 800)."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        p = _Cards()
        p.feed(_get(f"{BASE}/campaigns" if page == 1 else f"{BASE}/campaigns/page/{page}"))
        new = [(t, h) for t, h in p.cards if h and h not in seen]
        if not new:
            break
        for t, h in new:
            seen.add(h)
            out.append(parse_card(t, h))
    return out


def detail(card):
    """Brief excerpt, the join link, and any Twitch/Kick channel the brief names."""
    html = _get(card["url"])
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    i = text.find("THRESHOLDS AND CAPS")
    brief = text[i:i + 1400] if i >= 0 else text[:800]
    join = re.search(r'href="(https?://(?:www\.)?(?:whop\.com|vyro\.com|clipster\.[a-z]+|promote\.fun|clipstake\.[a-z]+|'
                     r'clipfarm\.[a-z]+)[^"]*)"', html)
    twitch = re.findall(r"twitch\.tv/([A-Za-z0-9_]{3,25})", html, re.I)
    kick = re.findall(r"kick\.com/([A-Za-z0-9_]{3,25})", html, re.I)
    bad = {"videos", "directory", "settings", "p", "login", "signup"}
    return {"brief": brief, "join_url": join.group(1) if join else card["url"],
            "twitch": [t.lower() for t in twitch if t.lower() not in bad], "kick": [k.lower() for k in kick]}


def login_guesses(name):
    words = NOISE.sub(" ", name).split()
    words = [w for w in words if not w.isdigit()][:3]
    if not words:
        return []
    base = "".join(words).lower()
    return list(dict.fromkeys([base, "_".join(words).lower(), words[0].lower()]))


def streams_gta(login, game_re=r"grand theft auto|gta"):
    """(True, details) if the Twitch channel had GTA in any of its last 10 broadcasts."""
    from . import twitch
    try:
        vods = twitch.list_archives(login, 10)
    except Exception:
        return False, "no Twitch channel"
    if not vods:
        return False, "no Twitch broadcasts"
    hits = 0
    for v in vods[:10]:
        if re.search(r"gta|grand theft|nopixel|\brp\b", v.get("title") or "", re.I):
            hits += 1
            continue
        try:
            if any(re.search(game_re, g or "", re.I) for _, _, g in twitch.chapters(v["id"].lstrip("v"))):
                hits += 1
        except Exception:
            pass
    return hits > 0, f"GTA in {hits} of their last {len(vods)} streams"


def evaluate(card, game_re):
    """Decide whether a campaign fits. Returns a match dict or None."""
    gta_text = bool(GTA_RE.search(card["name"]))
    streamer = bool(STREAMER_RE.search(card["name"])) and card["kind"] in ("CLIPPING", "")
    if not (gta_text or streamer):
        return None
    d = detail(card)
    gta_text = gta_text or bool(GTA_RE.search(d["brief"]))
    login, why = None, ""
    for cand in d["twitch"] + login_guesses(card["name"]):
        ok, why = streams_gta(cand, game_re)
        if ok:
            login = cand
            break
    if not (gta_text or login):
        return None
    m = dict(card, **d, twitch_login=login, why=why if login else "mentions GTA")
    m["auto_clip"] = bool(login)   # the bot can clip a Twitch streamer's VODs; edits/music campaigns need you
    return m


def _steps(m):
    steps = [f"Open the campaign: {m['url']}",
             f"Click Join (it opens the board: {m['join_url']}) and sign in with the channel Gmail.",
             "Read the brief there. The key rules are copied below, but the board's page is the one that counts."]
    if m["auto_clip"]:
        steps.append(f'Reply "join" to this email and the bot starts clipping {m["twitch_login"]} on Twitch. Every '
                     "posting email for their clips will include the link to submit.")
    else:
        steps.append("This one needs edits the bot doesn't make (for example trailer edits set to a song). "
                     'Reply "skip" to stop hearing about it.')
    steps.append("After you post a campaign clip, submit its TikTok/YouTube link on the board to get paid. "
                 "The bot emails you the exact link to submit once it sees the post.")
    return steps


def notify(state, m):
    from . import mail
    rate = f"${m['rate']:.2f} per 1,000 views" if m.get("rate") else "rate on the page"
    budget = f"{m['left']} of {m['budget']} left" if m.get("left") else "budget on the page"
    lines = [f"New paid campaign that fits the channel: {m['name']}", "",
             f"Pays: {rate}  |  Budget: {budget}  |  Platforms: {m.get('platforms') or 'see page'}",
             f"Why it fits: {m['why']}", "", "What to do:"]
    lines += [f"{i}. {s}" for i, s in enumerate(_steps(m), 1)]
    lines += ["", "Rules from the brief (excerpt):", m["brief"][:1200]]
    mid = mail.send(f"Campaign: {m['name']} ({rate})", "\n".join(lines))
    return mid


def scan(cfg, state):
    """Find new matching campaigns and email up to MAX_EMAILS_PER_SCAN of them. Returns the new matches."""
    from . import mail
    book = state.setdefault("campaigns", {})
    seen = state.setdefault("campaigns_seen", {})
    game_re = (cfg.get("fetch") or {}).get("game_match") or "grand theft auto|gta"
    cards = list_campaigns()
    log.info("campaign scan: %d live campaigns", len(cards))
    print(f"::notice::campaign scan: {len(cards)} live campaigns listed", flush=True)
    found, sent = [], 0
    for c in cards:
        if c["id"] in seen or c["id"] in book:
            continue
        seen[c["id"]] = now_utc().date().isoformat()
        try:
            m = evaluate(c, game_re)
        except Exception as e:
            log.info("couldn't check campaign %s: %s", c["id"], e)
            continue
        if not m:
            continue
        rec = {k: m.get(k) for k in ("id", "name", "url", "join_url", "rate", "left", "budget", "platforms", "kind",
                                     "twitch_login", "auto_clip", "why")}
        rec.update(brief=m["brief"][:1500], found=now_utc().isoformat(), status="found")
        dry = os.environ.get("DRY_RUN") == "1"
        if dry:
            print(f"::notice::[dry run] would email: {rec['name']} | {rec.get('rate')}/1K | twitch={rec.get('twitch_login')} "
                  f"| join={rec.get('join_url')}", flush=True)
        elif mail.configured() and sent < MAX_EMAILS_PER_SCAN:
            rec["email_message_id"] = notify(state, m)
            rec["status"] = "notified"
            sent += 1
        book[c["id"]] = rec
        found.append(rec)
        print(f"::notice::campaign match: {rec['name']} ({rec.get('rate')} per 1K) | {rec['why']}", flush=True)
    for k in sorted(seen, key=seen.get)[:-3000]:   # keep the seen list bounded
        seen.pop(k)
    return found


def apply_reply(state, ref, verdict):
    """A reply to a campaign email: 'join' starts clipping that streamer, 'skip' drops it. True if it matched."""
    for rec in (state.get("campaigns") or {}).values():
        if rec.get("email_message_id") == ref:
            if verdict == "join":
                rec["status"] = "joined" if rec.get("auto_clip") else "joined_manual"
                rec["joined"] = now_utc().isoformat()
            elif verdict == "skip":
                rec["status"] = "skipped"
            return True
    return False


def joined_streamers(state):
    """Streamer entries (config format) for campaigns Elie joined."""
    out = []
    for rec in (state.get("campaigns") or {}).values():
        if rec.get("status") == "joined" and rec.get("twitch_login"):
            out.append({"name": rec["twitch_login"], "platform": "twitch", "channel": rec["twitch_login"],
                        "enabled": True, "campaign": rec["name"], "campaign_id": rec["id"]})
    return out


def campaign_for(state, streamer_name):
    for rec in (state.get("campaigns") or {}).values():
        if rec.get("status") == "joined" and (rec.get("twitch_login") or "").lower() == (streamer_name or "").lower():
            return rec
    return None
