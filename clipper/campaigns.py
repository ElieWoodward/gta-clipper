"""Daily scan for paid clipping campaigns that fit the channel (GTA or GTA-streamer content).

Source: ClippingAlpha's public directory, which lists the campaigns on Content Rewards (Whop), Vyro, Clipster and
other boards. For each new campaign that mentions GTA, or that pays for clips of a streamer who has streamed GTA
recently on Twitch, Elie gets one email with the rate, the rules and how to join. When he replies "join", that
streamer is added to the ones the bot clips, and every posting email for their clips says how to claim the money.
No Claude calls: this is plain web reading.
"""
import json
import os
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser

import requests

from .common import log, now_utc, DATA

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


def clean_name(name):
    """A campaign's name without the card text that sometimes runs into it ("... Post on TikTok, ... CPM $1.50")."""
    cut = re.split(r"\s+(?:Post on\b|Platforms\b|\$[\d.]+ per\b|LASTS\b)", name or "")[0].strip()
    return cut or (name or "").strip()


def parse_card(text, href):
    rate = re.search(r"\$([\d.]+) per 1K views", text)
    left = re.search(r"\$([\d.,]+K?) of \$([\d.,]+K?) left", text)
    plats = re.search(r"Platforms ([A-Za-z, ]+?)(?:CLIPPING|MUSIC|UGC|LOGO|CPM|$)", text)
    head = text.split("Platforms")[0]
    m = list(re.finditer(r"(?:LOW|FAIR|SOLID|ALPHA|SCORING)\s*\d*\s*", head))
    name = head[m[-1].end():].strip() if m else re.sub(r"^Board:\s*\S+(\s+Rewards)?\s*", "", head).strip()
    name = clean_name(name or head[-80:].strip())
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


# ------------------------------------------------------------------ Content Rewards' own GTA VI list
# ClippingAlpha doesn't list every campaign (grouped prize pools, brand-new ones). Content Rewards keeps a public
# GTA VI page; its cards are links to /c/campaigns/<id> whose text runs: agency, campaign name, then "$used/$total"
# with "$rate/1K" or "$12.5K pool".
CR_BASE = "https://contentrewards.com"
CR_MIN_FOLLOWERS = 20000
CR_GTA_URL = CR_BASE + "/c/discover/gtavi"
CR_AGENCY_RE = re.compile(r"^(?:[\w&.' -]{2,40}?\s+)?verified\s+", re.I)
CR_NAME_NOISE = re.compile(r"\$[\d,.]+k?|\b(gta|vi|6|grand theft auto|clipping|clips?|campaign|marathon|pool|stream|"
                           r"streamer|official|verified|reach|the)\b|[^\w\s]", re.I)


def parse_cr_card(text, href):
    """One Content Rewards card -> the same card dict ClippingAlpha cards use (source "cr")."""
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", text or "") if ln.strip()]
    flat = " ".join(lines)
    rate = re.search(r"\$([\d.]+)(?:\s*[–-]\s*\$([\d.]+))?\s*/\s*1K", flat, re.I)
    pool = re.search(r"\$([\d.,]+K?)\s*pool", flat, re.I)
    used = re.search(r"\$([\d.,]+K?)\s*/\s*\$([\d.,]+K?)(?!\s*/\s*1K)", flat)
    # the name: drop the money tokens at the end; an agency line or "<Agency> Verified" prefix goes too
    words = [ln for ln in lines if not re.fullmatch(r"[\d$.,K/–\- ]+(pool|1K)?|\d+|views?", ln, re.I)]
    if len(words) >= 2 and len(words[0]) <= 40 and not GTA_RE.search(words[0]):
        agency, name = words[0], " ".join(words[1:2])
    else:
        name = " ".join(words) or flat
        agency = ""
    name = re.sub(r"\s*(\$[\d.,]+K?\s*/\s*\$[\d.,]+K?.*|\$[\d.,]+K?\s*pool.*)$", "", name, flags=re.I).strip()
    name = CR_AGENCY_RE.sub("", name).strip() or name
    kind = ("LOGO" if re.search(r"\blogo\b", name, re.I) else
            "MUSIC" if re.search(r"\b(music|sound|song|tracks?|audio)\b", name, re.I) else "CLIPPING")
    cid = re.search(r"/c/campaigns/([0-9a-f-]{20,})", href or "")
    return {"id": "cr__" + (cid.group(1) if cid else re.sub(r"\W+", "", name)[:40]), "source": "cr",
            "url": CR_BASE + "/c/campaigns/" + (cid.group(1) if cid else ""), "name": name[:120], "agency": agency,
            "rate": float(rate.group(2) or rate.group(1)) if rate else None,
            "pool": pool.group(1) if pool else ("yes" if re.search(r"\bpool\b", name, re.I) else None),
            "left": None, "budget": (used.group(2) if used else pool.group(1) if pool else None),
            "used": used.group(1) if used else None, "platforms": "", "kind": kind, "text": flat[:400],
            "streamer_guess": cr_streamer_guesses(name)}


def cr_streamer_guesses(name):
    """Twitch logins a Content Rewards campaign name points at: "Kai Cenat - $10,000 GTA 6 Pool Campaign" ->
    kaicenat; "Dasgasd3m x RUCKUS | GTA 6" -> dasgasd3m; "BOBBY VAN RIPER GTA VI Clipping" -> bobbyvanriper."""
    head = re.split(r"\s+(?:x|×)\s+|\s*[|–/]\s*|\s+-\s+|\s+GTA\b|\s+Grand Theft", name, maxsplit=1, flags=re.I)[0]
    words = [w for w in CR_NAME_NOISE.sub(" ", head).split() if not w.isdigit()][:6]
    out = []
    # the agency can run into the name on one line ("Clipped & Shipped Kai Cenat"): also try it with the first
    # words dropped; each guess is checked against Twitch before it's used
    for start in range(0, min(4, len(words))):
        w = words[start:start + 3]
        out += ["".join(w).lower(), "_".join(w).lower()] + (["".join(w[:2]).lower()] if len(w) >= 2 else [])
    if len(words) == 1:   # a one-word name ("Lacy"); otherwise single words match strangers who play GTA
        out.append(words[0].lower())
    out = [x for x in out if len(x) >= 6 or len(words) == 1]
    return [x for x in dict.fromkeys(out) if 3 <= len(x) <= 25][:10]


def cr_list_campaigns():
    """Cards on Content Rewards' public GTA VI page (no login). Empty if the page can't be read."""
    try:
        html = _get(CR_GTA_URL)
    except Exception as e:
        log.info("Content Rewards GTA VI page: %s", e)
        return []
    out, seen = [], set()
    for href, inner in re.findall(r'<a[^>]+href="((?:https://contentrewards\.com)?/c/campaigns/[0-9a-f-]{20,})[^"]*"[^>]*>'
                                  r'(.*?)</a>', html, flags=re.S | re.I):
        text = re.sub(r"<[^>]+>", "\n", inner)
        import html as _html
        text = _html.unescape(text)
        c = parse_cr_card(text, href)
        if c["id"] not in seen and c["name"]:
            seen.add(c["id"])
            out.append(c)
    print(f"::notice::Content Rewards GTA VI page: {len(out)} campaign(s)", flush=True)
    return out


def cr_detail(card):
    """A Content Rewards campaign page (public): brief text and any Twitch/Kick channel it names."""
    try:
        html = _get(card["url"])
    except Exception as e:
        log.info("couldn't read %s: %s", card["url"], e)
        html = ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    i = text.find("Brief")
    brief = (text[i:i + 1400] if i >= 0 else text[:1000]) or card["name"]
    bad = {"videos", "directory", "settings", "p", "login", "signup"}
    return {"brief": f"{card['name']}. {brief}", "join_url": card["url"],
            "twitch": [t.lower() for t in re.findall(r"twitch\.tv/([A-Za-z0-9_]{3,25})", html, re.I) if t.lower() not in bad],
            "kick": [k.lower() for k in re.findall(r"kick\.com/([A-Za-z0-9_]{3,25})", html, re.I)]}


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


def streams_gta(login, game_re=r"grand theft auto|gta", max_chapter_checks=10):
    """(True, details) if the Twitch channel had GTA in any of its last 10 broadcasts."""
    from . import twitch
    try:
        vods = twitch.list_archives(login, 10)
    except Exception:
        return False, "no Twitch channel"
    if not vods:
        # empty can also mean Twitch answered "service error": ask again before deciding there's nothing
        q = 'query { user(login: "%s") { id videos(first: 1, type: ARCHIVE) { edges { node { id } } } } }' % login.lower()
        data = twitch._gql({"query": q}) or {}
        if data.get("errors") or data.get("data") is None:
            raise Deferred(f"Twitch didn't answer for {login}")
        if (((data.get("data") or {}).get("user") or {}).get("videos") or {}).get("edges"):
            vods = twitch.list_archives(login, 10)
        if not vods:
            return False, "no Twitch broadcasts"
    hits, checked = 0, 0
    for v in vods[:10]:
        if re.search(r"gta|grand theft|nopixel|\brp\b", v.get("title") or "", re.I):
            hits += 1
            continue
        checked += 1
        if checked > max_chapter_checks:
            continue
        try:
            if any(re.search(game_re, g or "", re.I) for _, _, g in twitch.chapters(v["id"].lstrip("v"))):
                hits += 1
        except Exception:
            pass
    return hits > 0, f"GTA in {hits} of their last {len(vods)} streams"


class Deferred(Exception):
    """This scan's budget of Twitch lookups is used up; check the campaign on a later scan."""


QUICK = {"left": 0}   # Twitch lookups left this scan for campaigns named only after a person ("Benji Ramos")


def _slug_guess(card):
    """Logins from the campaign's URL. ClippingAlpha URLs are /campaigns/<creator>/<campaign>, and the creator part
    is usually the streamer's own name ("lysium" in lysium/benji-ramos, where Benji Ramos is his GTA character)."""
    parts = card["id"].split("__")
    head = [re.sub(r"[^a-z0-9_]", "", parts[0].lower())] if len(parts) > 1 else []
    tail = parts[-1]
    tail = re.sub(r"-?(clipping|clips?|campaign|official|stream(er)?|rewards?|\d+)(-|$)", "-", tail)
    out = head + [tail.replace("-", "").lower(), tail.replace("-", "_").strip("_").lower()]
    return [w for w in dict.fromkeys(out) if len(w) >= 3]


def quick_gta_login(card, game_re):
    """For a clipping campaign whose name has no GTA or streaming word: is it named after a Twitch streamer who
    plays GTA? Cheap (a few Twitch lookups, no page load), capped per scan."""
    name = re.split(r"\bPost on\b|\bCLIPPING\b", card["name"])[0]
    words = [w for w in re.split(r"[/|,]", name) if w.strip()]
    guesses = _slug_guess(card) + [g for w in words for g in login_guesses(w)] + login_guesses(name)
    # a first name alone ("harry" from "Harry Jowsey") matches some random streamer: only full names
    firsts = {w.split()[0].lower() for w in words if len(w.split()) > 1}
    guesses = [g for g in dict.fromkeys(guesses) if g not in firsts][:6]
    if QUICK["left"] < len(guesses):
        raise Deferred(card["id"])
    for g in guesses:
        QUICK["left"] -= 1
        ok, why = streams_gta(g, game_re, max_chapter_checks=5)
        # a mostly-GTA streamer only: a name guess can hit an unrelated channel that played GTA once or twice
        m = re.search(r"GTA in (\d+) of their last (\d+)", why) if ok else None
        if m and int(m.group(1)) >= max(3, (int(m.group(2)) + 1) // 2):   # at least half their recent streams
            return g
    return None


def evaluate(card, game_re):
    """Decide whether a campaign fits. Returns a match dict or None."""
    gta_text = bool(GTA_RE.search(card["name"])) or card.get("source") == "cr"   # Content Rewards' GTA VI page
    streamer = bool(STREAMER_RE.search(card["name"])) and card["kind"] in ("CLIPPING", "")
    named_after = None
    if not (gta_text or streamer):
        # Campaigns are often just the streamer's name ("Benji Ramos"): check that name against Twitch.
        if card["kind"] not in ("CLIPPING", ""):
            return None
        named_after = None if card.get("source") == "cr" else quick_gta_login(card, game_re)
        if not named_after:
            return None
    d = cr_detail(card) if card.get("source") == "cr" else detail(card)
    if named_after:
        # the campaign page itself has to point at that channel or at GTA, or it's a name clash
        if named_after not in [t.lower() for t in d["twitch"]] and not GTA_RE.search(d["brief"]):
            return None
        d["twitch"] = [named_after] + [t for t in dict.fromkeys(d["twitch"]) if t != named_after]
    gta_text = gta_text or bool(GTA_RE.search(d["brief"]))
    login, why = None, ""
    guesses = card.get("streamer_guess") or login_guesses(card["name"])
    for cand in ([] if is_logo(card) else list(dict.fromkeys(d["twitch"] + guesses))):
        ok, why = streams_gta(cand, game_re)
        if ok and card.get("source") == "cr" and cand not in d["twitch"]:
            from . import twitch   # a guessed channel (the page doesn't link it): only a real streamer counts
            ok = (twitch.followers(cand) or 0) >= CR_MIN_FOLLOWERS
        if ok:
            login = cand
            break
    if not login and card.get("source") == "cr" and d["twitch"]:
        # the campaign page itself links this channel and the campaign is on the GTA VI page: that's the
        # streamer, even if their last few streams weren't GTA (only the GTA parts of a stream get clipped)
        login, why = d["twitch"][0], "linked on the campaign page"
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
    if m.get("auto_clip"):
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


# For joining by itself the bot wants more than a loose match: GTA named outright (not just "RP"/"roleplay", which
# also fits Minecraft or Rust roleplay), or a streamer whose recent streams are mostly GTA.
SURE_GTA_RE = re.compile(r"\bgta ?(v|5|6|vi|rp|online)?\b|grand theft auto|nopixel|no pixel|los santos|leonida|"
                         r"vice city|\bfivem\b", re.I)


def sure_gta(m):
    """True only when a campaign is certainly GTA content (used to join by itself)."""
    if SURE_GTA_RE.search(m.get("name") or "") or SURE_GTA_RE.search(m.get("brief") or ""):
        return True
    if m.get("source") == "cr" and m.get("twitch_login") and m.get("why") == "linked on the campaign page":
        return True
    hit = re.search(r"GTA in (\d+) of their last (\d+)", m.get("why") or "")
    return bool(m.get("twitch_login") and hit and int(hit.group(1)) >= max(3, (int(hit.group(2)) + 1) // 2))


def paused(cfg, day=None):
    """campaigns.pause_until: before this date only really popular streamers' campaigns are clipped and joined
    (posting what gets views builds the account first)."""
    until = str((cfg.get("campaigns") or {}).get("pause_until") or "")
    if not until:
        return False
    if day is None:
        from zoneinfo import ZoneInfo
        day = now_utc().astimezone(ZoneInfo(cfg.get("timezone", "UTC"))).date().isoformat()
    return day < until


def popular(cfg, followers):
    """Big enough to be worth a campaign during the pause (campaigns.popular_followers on Twitch)."""
    need = int((cfg.get("campaigns") or {}).get("popular_followers") or 1_000_000)
    return followers is not None and int(followers) >= need


def avoided(cfg, login):
    """Streamers Elie never wants clipped (campaigns.avoid), campaign or not."""
    return (login or "").lower() in {str(a).lower() for a in (cfg.get("campaigns") or {}).get("avoid") or []}


def apply_rules(cfg, day=None):
    """In place on a loaded config: avoided streamers off; during the pause, small campaign streamers off (or plain
    regular streams if they're clipped anyway)."""
    for s in cfg.get("streamers") or []:
        login = s.get("channel") or s.get("name")
        if avoided(cfg, login):
            s.update(enabled=False, campaign="", campaign_off="avoided", campaign_was=s.get("campaign") or "")
        elif s.get("campaign") and paused(cfg, day) and not popular(cfg, s.get("followers")):
            s.update(campaign="", preapproval=False, campaign_off="paused", campaign_was=s["campaign"])
            if s.get("from_campaign"):
                s["enabled"] = False
    return cfg


def auto_join_on(cfg=None):
    from .common import load_config
    cfg = cfg or load_config()
    return os.environ.get("MAC_RUNS_CLIPPER") == "1" and bool((cfg.get("campaigns") or {}).get("auto_join", True))


def queue_join(m):
    """Hand a campaign to the Mac's join step (it opens Content Rewards and clicks Join). Returns the reference the
    join is tracked by, the same shape a Telegram Join tap gives."""
    import uuid
    from . import mac
    token = uuid.uuid4().hex[:10]
    items = [j for j in mac.joins_load() if j.get("name") != m.get("name")]
    items.append({"token": token, "name": m.get("name"), "link": m.get("join_url") or m.get("url"), "status": "todo",
                  "tries": 0, "auto": True, "at": now_utc().isoformat()})
    mac.joins_save(items)
    log.info("auto-joining campaign %s", m.get("name"))
    return f"tg:{token}"


BRAND_RE = re.compile(r"\b(1win|puffco|fanvue|casino|bet(ting)?|gambl\w*|vape|nicotine|crypto|stake|"
                      r"luxdrop|18\+|regulated)\b|\s(x|×)\s+[A-Z0-9]", re.I)


def brand_deal(m):
    """A brand collab ("BenDaDonnn x Puffco", "GATTOUZ × GTA VI × 1WIN", gambling, vapes): Elie decides."""
    return bool(BRAND_RE.search(m.get("name") or "") or
                re.search(r"\b(1win|casino|gambl\w*|vape|nicotine|18\+|regulated)\b", (m.get("brief") or "")[:1500], re.I))


def notify(state, m):
    from . import mail
    if auto_join_on():   # the Mac joins certain-GTA campaigns by itself; anything less certain isn't sent at all
        from .common import load_config
        cfg = load_config()
        if avoided(cfg, m.get("twitch_login")):
            return "skipped:avoided"
        if sure_gta(m) and paused(cfg) and not popular(cfg, m.get("followers")):
            return "skipped:paused"   # looked at again once the pause ends (see scan)
        if sure_gta(m) and m.get("twitch_login") and not m.get("pool") and not brand_deal(m) and \
                m.get("kind") in ("CLIPPING", "", None):
            if m.get("id") and m["id"] not in (state.get("campaigns_active") or {m["id"]: 1}):
                return "waiting:rank"   # joined once it ranks in the top campaigns.max_active (see scan)
            return queue_join(m)
        if not (m.get("gta6") or sure_gta(m)):
            return "skipped:not-sure-gta"
        # a GTA campaign it won't join by itself (a prize pool, a brand deal, no Twitch channel it can clip):
        # Elie gets the join link and decides
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
    if (cfg.get("campaigns") or {}).get("contentrewards_source", True):
        from .mac import same_campaign
        known = [c["name"] for c in cards] + [r.get("name") for r in book.values()]
        cr_all = cr_list_campaigns()
        cr_extra = [c for c in cr_all if not any(same_campaign(c["name"], n) for n in known if n)]
    else:
        cr_all, cr_extra = [], []
    log.info("campaign scan: %d live campaigns", len(cards))
    print(f"::notice::campaign scan: {len(cards)} live campaigns listed", flush=True)
    found, sent = [], 0
    launch_on = bool((cfg.get("_launch") or {}).get("on"))
    if launch_on:
        from .launch import settings as _ls
        cap = int(_ls(cfg)["campaign_emails"])
    else:
        cap = MAX_EMAILS_PER_SCAN
    live_ids = {c["id"] for c in cards} | {c["id"] for c in cr_all}
    dry = os.environ.get("DRY_RUN") == "1"
    prev = state.get("campaign_scan_count") or 0
    partial = not cards or (prev and len(cards) < 0.7 * prev)   # a short listing (a page that didn't load): no verdicts
    state["campaign_scan_count"] = len(cards) if not partial else prev
    for rec in book.values():   # wrongly ended (one listing it was missing from): back on if it's still running
        if rec.get("status") == "ended" and rec.get("joined") and \
                (rec["id"] in live_ids or cr_still_running(rec)) and \
                (now_utc() - datetime.fromisoformat(rec.get("ended") or "2000-01-01T00:00:00+00:00")).days < 7:
            rec.update(status="joined", missing_scans=0)
            rec.pop("missing_since", None)
            print(f"::notice::campaign still running, back on: {rec['name']}", flush=True)
    for rec in book.values():   # campaigns that disappeared (budget spent, ended): stop clipping for them
        if rec.get("status") not in ACTIVE:
            continue
        if rec["id"] in live_ids:
            rec.pop("missing_since", None)
            rec["missing_scans"] = 0
            continue
        if partial or (str(rec["id"]).startswith("cr__") and not cr_all):
            continue   # that list didn't load this time: no verdicts from it
        # one missing listing isn't an ending: it takes 3 scans in a row over at least 6 hours, and never while the
        # Mac's Content Rewards check still sees the campaign running
        rec["missing_scans"] = rec.get("missing_scans", 0) + 1
        rec.setdefault("missing_since", now_utc().isoformat())
        if rec["missing_scans"] < 3 or \
                (now_utc() - datetime.fromisoformat(rec["missing_since"])).total_seconds() < 6 * 3600 or \
                cr_still_running(rec):
            continue
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
    QUICK["left"] = int((cfg.get("campaigns") or {}).get("lookups_per_scan", 400))
    if not state.get("campaigns_recheck_v3"):   # earlier scans skipped campaigns named only after a streamer
        for k in [k for k in seen if k not in book]:
            seen.pop(k)
        state["campaigns_recheck_v3"] = now_utc().isoformat()
    deferred = 0
    # A campaign that didn't fit is looked at again every 2 days while it's still listed: before GTA 6 came out,
    # streamers weren't playing GTA, so their campaigns failed the "streams GTA" check and were never seen again.
    recheck = (now_utc() - timedelta(days=int((cfg.get("campaigns") or {}).get("recheck_days", 2)))).date().isoformat()
    for c in sorted(cards + cr_extra, key=lambda c: -(c.get("rate") or (1.0 if c.get("pool") else 0))):   # best paying first
        if c["id"] in book or (c["id"] in seen and str(seen[c["id"]]) > recheck):
            continue
        try:
            m = evaluate(c, game_re)
        except Deferred:
            deferred += 1
            continue   # not marked seen: checked on a later scan
        except Exception as e:
            log.info("couldn't check campaign %s: %s", c["id"], e)
            seen[c["id"]] = now_utc().date().isoformat()
            continue
        seen[c["id"]] = now_utc().date().isoformat()
        if not m:
            continue
        if m.get("twitch_login"):
            from . import twitch
            m["followers"] = twitch.followers(m["twitch_login"])
        rec = {k: m.get(k) for k in ("id", "name", "url", "join_url", "rate", "left", "budget", "platforms", "kind",
                                     "twitch_login", "auto_clip", "why", "gta6", "followers", "source", "pool")}
        rec.update(brief=m["brief"][:1500], found=now_utc().isoformat(), status="found")
        if dry:
            print(f"::notice::[dry run] would email: {rec['name']} | {rec.get('rate')}/1K | twitch={rec.get('twitch_login')} "
                  f"| join={rec.get('join_url')}", flush=True)
        elif not rec["auto_clip"] and not rec["gta6"]:
            rec["status"] = "noted"   # GTA edit/music campaigns: recorded, not emailed (the bot can't make those)
        book[c["id"]] = rec
        found.append(rec)
        print(f"::notice::campaign match: {rec['name']} ({rec.get('rate')} per 1K) | {rec['why']}", flush=True)
    for r in book.values():   # campaign streamers found before follower counts were kept
        if r.get("status") in ACTIVE and r.get("twitch_login") and r.get("followers") is None and not dry:
            from . import twitch
            r["followers"] = twitch.followers(r["twitch_login"])
    if not paused(cfg):   # the pause is over: campaigns skipped during it get their turn
        for r in book.values():
            if r.get("email_message_id") == "skipped:paused" and r.get("status") == "notified":
                r["status"] = "found"
                r.pop("email_message_id", None)
    if launch_on:   # GTA 6 campaigns noted before launch get their email now
        for r in book.values():
            text = f"{r.get('name')} {r.get('brief')}"
            if (r.get("status") == "noted" and r.get("kind") in ("CLIPPING", "") and GTA6_RE.search(text)
                    and not re.search(r"\b(trailer|music|song|edits?)\b", text[:500], re.I)):
                r.update(gta6=True, status="found")
    # Pick the top campaigns (joined or not) before anything is joined; a waiting one that made it is joined now.
    top = select_active(cfg, state)
    if auto_join_on(cfg) and not dry:
        for i in top:
            r = book.get(i) or {}
            if r.get("status") == "waiting":
                r["email_message_id"] = queue_join(r)
                r["status"] = "notified"
    # Email what fits, best paying first: streamer campaigns the bot clips, and from launch day GTA 6 campaigns too.
    def emailable(r):
        return r.get("status") == "found" and (r.get("auto_clip") or (launch_on and r.get("gta6")))
    if mail.configured() and not dry:
        for rec in sorted((r for r in book.values() if emailable(r)), key=lambda r: -(r.get("rate") or 0)):
            if sent >= cap:
                break
            try:
                rec["email_message_id"] = notify(state, dict(rec, brief=rec.get("brief") or ""))
            except Exception as e:   # Gmail down: it stays "found" and goes out on a later scan
                log.warning("couldn't email campaign %s: %s", rec.get("name"), e)
                break
            rec["status"] = "waiting" if rec["email_message_id"] == "waiting:rank" else "notified"
            sent += rec["status"] == "notified"
    if deferred:
        print(f"::notice::campaign scan: {deferred} campaign(s) named after people left to check on later scans",
              flush=True)
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


def is_logo(rec):
    """A logo/brand campaign (a brand's logo on GTA videos): it pays for the logo, not for a streamer's clips, so a
    name like "Jack [GTA LOGO]" must never turn into the Twitch channel "jack"."""
    if rec.get("kind") == "LOGO" or re.search(r"\blogo\b", rec.get("name") or "", re.I):
        return True
    # the saved brief starts with ClippingAlpha's own menu ("... UGC 143 Logo 38 Music ..."): only the campaign's
    # own words count, e.g. "Jack logo campaign"
    return bool(re.search(r"\blogo (campaign|clipping|promo)", rec.get("brief") or "", re.I))


def cr_still_running(rec):
    """The Mac's last Content Rewards check (data/mac.json) saw this campaign, not ended, in the last 12 hours."""
    try:
        from .mac import same_campaign
        book = (json.loads((DATA / "mac.json").read_text()).get("campaigns") or {})
    except Exception:
        return False
    cutoff = (now_utc() - timedelta(hours=12)).isoformat()
    return any(same_campaign(n, rec.get("name")) and not r.get("ended") and (r.get("seen") or "") >= cutoff
               for n, r in book.items())


# ------------------------------------------------------------------ which campaigns get clipped (top N by money)
def _money(x):
    """"$6,825" / "2.5K" / "6825" -> 6825.0 (None if unknown)."""
    m = re.search(r"([\d.,]+)\s*(K)?", str(x or "").replace("$", ""), re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
    except ValueError:
        return None


def campaign_score(cfg, state, rec):
    """Expected money: pay per 1K views x budget left x how our clips of this streamer do x how often they
    stream GTA. Neutral (1.0) where there's no data yet."""
    rate = rec.get("rate") or 0.5
    total, used, left = _money(rec.get("budget")), _money(rec.get("used")), _money(rec.get("left"))
    left = left if left is not None else (total - used if total is not None and used is not None else None)
    budget = 0.8 if left is None else max(0.15, min(1.0, left / 1000))
    login = (rec.get("twitch_login") or "").lower()
    since = (now_utc() - timedelta(days=21)).isoformat()
    def med(vals):
        vals = sorted(vals)
        return vals[len(vals) // 2] if vals else None
    yt = [(c, (c.get("posts") or {}).get("youtube") or {}) for c in (state.get("clips") or {}).values()]
    yt = [(c, p) for c, p in yt if p.get("views") is not None and (p.get("posted_at") or "") >= since]
    mine = med([p["views"] for c, p in yt if (c.get("streamer") or "").lower() == login])
    allv = med([p["views"] for c, p in yt])
    n_mine = sum(1 for c, p in yt if (c.get("streamer") or "").lower() == login)
    views = max(0.3, min(3.0, mine / allv)) if mine is not None and allv and n_mine >= 3 else 1.0
    hit = re.search(r"GTA in (\d+) of their last (\d+)", rec.get("why") or "")
    active = max(0.3, int(hit.group(1)) / int(hit.group(2))) if hit else 0.6
    return round(rate * budget * views * active, 4)


def eligible(cfg, rec):
    """A campaign the bot can work for: a Twitch streamer it clips, paid per view, not a brand deal or a pool."""
    return (rec.get("status") in ACTIVE + ("waiting",) and rec.get("auto_clip") and rec.get("twitch_login")
            and not is_logo(rec) and not avoided(cfg, rec.get("twitch_login"))
            # a prize pool or brand deal is Elie's call: clipped once he has joined it
            and (rec.get("status") == "joined" or not (rec.get("pool") or brand_deal(rec))))


def select_active(cfg, state):
    """The campaigns that get clipped: the best campaigns.max_active (4) by expected money, joined or not.
    One already in keeps its place for campaigns.grace_days (3) after it started, and after that is only replaced by
    one that scores campaigns.swap_margin (1.3x) better, so they don't flip back and forth. Saved in
    state["campaigns_active"] {id: since}; returns it."""
    c = cfg.get("campaigns") or {}
    n, margin, grace = int(c.get("max_active", 4)), float(c.get("swap_margin", 1.3)), int(c.get("grace_days", 3))
    book = state.get("campaigns") or {}
    cur = dict(state.get("campaigns_active") or {})
    pool = {i: r for i, r in book.items() if eligible(cfg, r)}
    score = {i: campaign_score(cfg, state, r) for i, r in pool.items()}
    keep = sorted((i for i in cur if i in pool), key=lambda i: -score[i])[:n]
    out = {i: cur[i] for i in keep}
    now = now_utc()
    for i in sorted((i for i in pool if i not in out), key=lambda i: -score[i]):
        if len(out) < n:
            out[i] = now.isoformat()
            continue
        settled = [k for k in out if (now - datetime.fromisoformat(out[k])).days >= grace]
        weakest = min(settled, key=lambda k: score[k], default=None)
        if weakest and score[i] > margin * score[weakest]:
            print(f"::notice::campaign swap: {pool[i]['name']} ({score[i]}) replaces {pool[weakest]['name']} "
                  f"({score[weakest]})", flush=True)
            out.pop(weakest)
            out[i] = now.isoformat()
    state["campaigns_active"] = out
    for i, r in pool.items():
        r["score"] = score[i]
    return out


def joined_streamers(state):
    """Streamer entries (config format) for campaigns that pay for a GTA streamer's clips. They're clipped as soon as
    they're found, so clips are ready by the time you join; joined ones go first."""
    out = []
    recs = sorted((state.get("campaigns") or {}).values(), key=lambda r: r.get("status") != "joined")
    top = state.get("campaigns_active")   # only the best few get clipped (select_active); None: not ranked yet
    for rec in recs:
        if top is not None and rec.get("id") not in top:
            continue
        if rec.get("status") in ACTIVE and rec.get("auto_clip") and rec.get("twitch_login") and not is_logo(rec):
            out.append({"name": rec["twitch_login"], "platform": "twitch", "channel": rec["twitch_login"],
                        "enabled": True, "campaign": clean_name(rec["name"]), "campaign_id": rec["id"],
                        "campaign_joined": rec.get("status") == "joined", "followers": rec.get("followers"),
                        "from_campaign": True})
    return out


def campaign_for(state, streamer_name, joined_only=False):
    """The active campaign that pays for this streamer's clips (None if there isn't one)."""
    for rec in (state.get("campaigns") or {}).values():
        if (rec.get("twitch_login") or "").lower() != (streamer_name or "").lower():
            continue
        if rec.get("status") == "joined" or (not joined_only and rec.get("status") in ACTIVE):
            return rec
    return None
