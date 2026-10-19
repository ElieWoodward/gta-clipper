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
GTA6_RE = re.compile(r"\bgta ?(6|vi)\b|grand theft auto:? ?(6|vi)\b|leonida", re.I)


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
    head = text.split("Platforms")[0]
    m = list(re.finditer(r"(?:LOW|FAIR|SOLID|ALPHA|SCORING)\s*\d*\s*", head))
    name = head[m[-1].end():].strip() if m else re.sub(r"^Board:\s*\S+(\s+Rewards)?\s*", "", head).strip()
    name = name or head[-80:].strip()
    return {"id": href.strip("/").replace("campaigns/", "").replace("/", "__"), "url": BASE + href, "name": name[:120],
            "rate": float(rate.group(1)) if rate else None, "left": left.group(1) if left else None,
            "budget": left.group(2) if left else None, "platforms": plats.group(1).strip() if plats else "",
            "kind": next((k for k in ("MUSIC", "UGC", "LOGO", "CLIPPING") if k in text), ""), "text": text[:400]}


def list_campaigns(max_pages=25):
    """Every live campaign in the directory (about 800)."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        p = _Cards()
        try:
            p.feed(_get(f"{BASE}/campaigns" if page == 1 else f"{BASE}/campaigns/page/{page}"))
        except requests.HTTPError as e:
            if page > 1 and e.response is not None and e.response.status_code == 404:
                break   # past the last page
            raise
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
    join = re.search(r'href="((?:https://clippingalpha\.com)?/go/\d+[^"]*)"', html) or \
        re.search(r'href="(https?://(?:www\.)?(?:whop\.com|vyro\.com|clipster\.[a-z]+|promote\.fun|clipstake\.[a-z]+|'
                  r'clipfarm\.[a-z]+)[^"]*)"', html)
    twitch = re.findall(r"twitch\.tv/([A-Za-z0-9_]{3,25})", html, re.I)
    kick = re.findall(r"kick\.com/([A-Za-z0-9_]{3,25})", html, re.I)
    bad = {"videos", "directory", "settings", "p", "login", "signup"}
    join_url = join.group(1).replace("&amp;", "&") if join else card["url"]
    if join_url.startswith("/"):
        join_url = BASE + join_url
    return {"brief": brief, "join_url": join_url,
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
    # GTA 6 gameplay clipping (not music/trailer edits, which the bot can't make)
    m["gta6"] = (card["kind"] in ("CLIPPING", "") and bool(GTA6_RE.search(card["name"]) or GTA6_RE.search(d["brief"]))
                 and not re.search(r"\b(trailer|music|song|edits?)\b", f"{card['name']} {d['brief'][:300]}", re.I))
    return m


def _steps(m):
    steps = [f"Open the campaign: {m['url']}",
             f"Click Join (it opens the board: {m['join_url']}) and sign in with the channel Gmail.",
             "Read the brief there. The key rules are copied below, but the board's page is the one that counts."]
    if m["auto_clip"]:
        steps.append(f'The bot has already started clipping {m["twitch_login"]} (GTA only). Once you\'ve joined, reply '
                     '"join" to this email: their clips then go to the front of the queue and every posting email for '
                     'them includes the link to submit. Reply "skip" if you don\'t want this one.')
    elif m.get("gta6"):
        steps.append("It pays for GTA 6 clips, and the bot is clipping GTA 6 from the biggest streams. Check the brief "
                     "allows clips of any streamer; if it does, join and submit your GTA 6 posts there. "
                     'Reply "join" so the dashboard tracks it, or "skip" to drop it.')
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
    launch_on = bool((cfg.get("_launch") or {}).get("on"))
    if launch_on:
        from .launch import settings as _ls
        cap = int(_ls(cfg)["campaign_emails"])
    else:
        cap = MAX_EMAILS_PER_SCAN
    live_ids = {c["id"] for c in cards}
    dry = os.environ.get("DRY_RUN") == "1"
    for rec in book.values():   # campaigns that disappeared (budget spent, ended): stop clipping for them
        if rec.get("status") in ACTIVE and rec["id"] not in live_ids and cards:
            rec["status"] = "ended"
            rec["ended"] = now_utc().isoformat()
            print(f"::notice::campaign ended: {rec['name']}", flush=True)
            if rec.get("joined") and mail.configured() and not dry:
                try:
                    mail.send(f"Clip bot: campaign ended: {rec['name'][:60]}",
                              f"{rec['name']} is no longer listed (budget used up or ended). The bot stopped clipping "
                              f"{rec.get('twitch_login')} for it. Submit any clips you haven't yet at "
                              f"{rec.get('join_url') or rec['url']} while the page is still up.")
                except Exception as e:
                    log.info("couldn't send the campaign-ended email: %s", e)
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
                                     "twitch_login", "auto_clip", "why", "gta6")}
        rec.update(brief=m["brief"][:1500], found=now_utc().isoformat(), status="found")
        if dry:
            print(f"::notice::[dry run] would email: {rec['name']} | {rec.get('rate')}/1K | twitch={rec.get('twitch_login')} "
                  f"| join={rec.get('join_url')}", flush=True)
        elif not rec["auto_clip"] and not rec["gta6"]:
            rec["status"] = "noted"   # GTA edit/music campaigns: recorded, not emailed (the bot can't make those)
        book[c["id"]] = rec
        found.append(rec)
        print(f"::notice::campaign match: {rec['name']} ({rec.get('rate')} per 1K) | {rec['why']}", flush=True)
    if launch_on:   # GTA 6 campaigns noted before launch get their email now
        for r in book.values():
            text = f"{r.get('name')} {r.get('brief')}"
            if (r.get("status") == "noted" and r.get("kind") in ("CLIPPING", "") and GTA6_RE.search(text)
                    and not re.search(r"\b(trailer|music|song|edits?)\b", text[:500], re.I)):
                r.update(gta6=True, status="found")
    # Email what fits, best paying first: streamer campaigns the bot clips, and from launch day GTA 6 campaigns too.
    def emailable(r):
        return r.get("status") == "found" and (r.get("auto_clip") or (launch_on and r.get("gta6")))
    if mail.configured() and not dry:
        for rec in sorted((r for r in book.values() if emailable(r)), key=lambda r: -(r.get("rate") or 0)):
            if sent >= cap:
                break
            rec["email_message_id"] = notify(state, dict(rec, brief=rec.get("brief") or ""))
            rec["status"] = "notified"
            sent += 1
    for k in sorted(seen, key=seen.get)[:-3000]:   # keep the seen list bounded
        seen.pop(k)
    return found


def apply_reply(state, ref, verdict):
    """A reply to a campaign email: 'join' starts clipping that streamer, 'skip' drops it. True if it matched."""
    for rec in (state.get("campaigns") or {}).values():
        if rec.get("email_message_id") == ref:
            if verdict == "join":
                rec["status"] = "joined" if rec.get("auto_clip") or rec.get("gta6") else "joined_manual"
                rec["joined"] = now_utc().isoformat()
            elif verdict == "skip":
                rec["status"] = "skipped"   # also stops clipping them (unless they're in config.yaml)
            return True
    return False


ACTIVE = ("found", "notified", "joined")   # campaigns whose streamer is being clipped


def joined_streamers(state):
    """Streamer entries (config format) for campaigns that pay for a GTA streamer's clips. They're clipped as soon as
    they're found, so clips are ready by the time you join; joined ones go first."""
    out = []
    recs = sorted((state.get("campaigns") or {}).values(), key=lambda r: r.get("status") != "joined")
    for rec in recs:
        if rec.get("status") in ACTIVE and rec.get("auto_clip") and rec.get("twitch_login"):
            out.append({"name": rec["twitch_login"], "platform": "twitch", "channel": rec["twitch_login"],
                        "enabled": True, "campaign": rec["name"], "campaign_id": rec["id"],
                        "campaign_joined": rec.get("status") == "joined"})
    return out


def campaign_for(state, streamer_name, joined_only=False):
    """The active campaign that pays for this streamer's clips (None if there isn't one)."""
    for rec in (state.get("campaigns") or {}).values():
        if (rec.get("twitch_login") or "").lower() != (streamer_name or "").lower():
            continue
        if rec.get("status") == "joined" or (not joined_only and rec.get("status") in ACTIVE):
            return rec
    return None
