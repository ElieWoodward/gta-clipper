"""Content Rewards (contentrewards.com) through a real Chrome window on the Mac, logged in as Elie.

It reads what only the logged-in site shows: which campaigns he's in and whether he's approved, each campaign's
requirements (like a link that must be in his bio), its budget and whether it has ended, and his drafts (pending,
approved, needs changes with the reviewer's note, posted, rejected). submit_draft() uploads a clip for pre-approval.

How it behaves, so the account looks like a person using it: a normal Chrome window on Elie's home internet, a pause
of a few seconds between pages, no more than one full check every 30 minutes, and only during waking hours
(mac.py decides when). It never types a password: Elie logs in once himself with `python -m clipper cr_login`.

The page-reading functions (parse_*) are plain text parsing so they can be tested offline.
"""
import random
import re
import time
from pathlib import Path

from .common import log

SITE = "https://contentrewards.com"
PROFILE = Path.home() / ".clipper" / "chrome-profile"   # the bot's own Chrome profile (separate from Elie's)
STATUS_WORDS = ("Approved", "Pending", "Rejected", "Applied", "Under review", "Waitlisted", "Ended", "Paused")
DRAFT_TABS = ("Needs changes", "Pending", "Approved", "Posted", "Rejected")


class NotLoggedIn(RuntimeError):
    pass


# ---------------------------------------------------------------- parsing (offline-testable)
def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def parse_campaign_list(text):
    """The "Your Campaigns" page -> [{"name", "status", "creator"}]. Cards read: status, creator, ·, age, name, ..."""
    ls = _lines(text)
    out = []
    for i, ln in enumerate(ls):
        if ln in STATUS_WORDS and i + 4 < len(ls) and ls[i + 2] == "·":
            out.append({"status": ln.lower(), "creator": ls[i + 1], "name": ls[i + 4]})
    return out


def parse_campaign_page(text):
    """One campaign's page -> joined/approved state, budget, requirements, bio link, ended."""
    ls = _lines(text)
    full = "\n".join(ls)
    info = {"joined": "Joined" in ls, "approved": bool(re.search(r"Application approved", full)),
            "pending": bool(re.search(r"Application (pending|under review|submitted)", full, re.I)),
            "rejected": bool(re.search(r"Application (rejected|declined)", full, re.I)),
            "preapproval": bool(re.search(r"pre-?approval", full, re.I)), "bio": [], "requirements": [],
            "budget_used": None, "budget_total": None}
    for i, ln in enumerate(ls):
        if re.match(r"^Add (a )?link in (your )?bio$", ln, re.I) and i + 1 < len(ls):
            info["bio"].append(ls[i + 1])
    for ln in ls:   # the brief can ask for it in its own words ("put @imnyzzz in your bio")
        if (re.search(r"\b(in|to|on) (your |the )?(profile )?bio\b", ln, re.I) and len(ln) <= 300
                and not re.match(r"^Add (a )?link in (your )?bio$", ln, re.I) and not any(ln == b or b in ln for b in info["bio"])):
            info["bio"].append(ln)
    if "Content requirements" in ls:
        start = ls.index("Content requirements") + 1
        for ln in ls[start:start + 12]:
            if ln in ("Top clippers", "Reference materials", "Brief") or ln.startswith("Rank"):
                break
            info["requirements"].append(ln)
    m = re.search(r"Budget\s*\n\s*\$([\d,.]+K?)\s*/\s*\$([\d,.]+K?)", full)
    if m:
        info["budget_used"], info["budget_total"] = _money(m.group(1)), _money(m.group(2))
    head = ls[:25]   # status badges sit at the top; the brief further down can say anything
    badge = any(re.fullmatch(r"(Ended|Campaign ended|Completed|Closed|Paused|Budget (reached|exhausted|spent))", ln, re.I)
                for ln in head)
    spent = (info["budget_total"] and info["budget_used"] is not None and info["budget_used"] >= info["budget_total"])
    info["ended"] = bool(badge or spent)
    return info


def _money(s):
    s = s.replace(",", "")
    mult = 1000 if s.endswith("K") else 1
    try:
        return float(s.rstrip("K")) * mult
    except ValueError:
        return None


def parse_draft_counts(text):
    """The Drafts page header -> {"needs changes": 1, "pending": 1, ...}."""
    ls = _lines(text)
    out = {}
    for i, ln in enumerate(ls):
        if ln in DRAFT_TABS and i + 1 < len(ls) and ls[i + 1].isdigit():
            out[ln.lower()] = int(ls[i + 1])
    return out


def parse_timeline(text):
    """A draft's revisions panel -> the newest reviewer request ({"who", "when", "note"}) or None."""
    ls = _lines(text)
    found = None
    for i, ln in enumerate(ls):
        if ln.lower().startswith("requested changes") and i >= 2:
            who, when = ls[i - 2], ls[i - 1]
            note = []
            for nxt in ls[i + 1:]:
                if re.match(r"^(Brand reviewer|Upload revision|Leave a comment)", nxt) or re.match(
                        r"^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2}", nxt):
                    break
                note.append(nxt)
            found = {"who": who, "when": when, "note": " ".join(note).strip()}
    return found


def _dollars(s):
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_earnings(text):
    """The Earnings page (with Queued/Arriving/Received opened) -> totals plus one row per paid clip:
    {"lifetime", "today", "week", "month", "balance", "queued", "arriving", "received",
     "items": [{"section", "campaign", "amount", "pays", "state"}]}."""
    ls = _lines(text)
    out = {"items": []}
    for i, ln in enumerate(ls):
        if ln == "Lifetime earned" and i + 1 < len(ls):
            out["lifetime"] = _dollars(ls[i + 1])
        if ln in ("Today", "This week", "This month", "Balance") and i and _dollars(ls[i - 1]) is not None:
            out[{"Today": "today", "This week": "week", "This month": "month", "Balance": "balance"}[ln]] = \
                _dollars(ls[i - 1])
    section = None
    for i, ln in enumerate(ls):
        if ln in ("Queued", "Arriving", "Received"):
            section = ln.lower()
            out.setdefault(section, 0.0)
            continue
        if section and re.match(r"^(Still accumulating|Total):", ln):
            out[section] = _dollars(ln) or 0.0
            continue
        if section and re.fullmatch(r"\$[\d,]+(\.\d+)?", ln) and i >= 2:
            nxt = ls[i + 1] if i + 1 < len(ls) else ""
            state = ls[i + 2] if nxt.startswith("Pays") and i + 2 < len(ls) else ""
            out["items"].append({"section": section, "campaign": ls[i - 2], "amount": _dollars(ln),
                                 "pays": nxt[5:].strip() if nxt.startswith("Pays") else "", "state": state})
    return out


def _secs(mmss):
    m = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$", (mmss or "").strip())
    if not m:
        return None
    h, mi, se = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + se


def parse_approved_cards(text):
    """The Drafts page's Approved tab -> [{"campaign", "posted", "of", "duration"}], in page order. A card reads:
    campaign, CPM, "0/2 posted", 00:00, /, 00:29, Submit link."""
    ls = _lines(text)
    out = []
    for i, ln in enumerate(ls):
        m = re.match(r"^(\d+)/(\d+) posted$", ln)
        if not m or i < 2:
            continue
        dur = None
        for j in range(i + 1, min(i + 6, len(ls))):
            if ls[j] == "/" and j + 1 < len(ls):
                dur = _secs(ls[j + 1])
                break
        out.append({"campaign": ls[i - 2], "posted": int(m.group(1)), "of": int(m.group(2)), "duration": dur})
    return out


def pick_card(cards, campaign, duration):
    """Index of the card that best matches a draft (same campaign, nearest length), or None."""
    best, best_i = None, None
    for i, c in enumerate(cards):
        if campaign and c.get("campaign") and campaign.lower() not in c["campaign"].lower() \
                and c["campaign"].lower() not in campaign.lower():
            continue
        gap = abs((c.get("duration") or 0) - (duration or 0)) if duration and c.get("duration") else 99
        if best is None or gap < best:
            best, best_i = gap, i
    if best_i is None or (duration and best is not None and best > 4):
        return None
    return best_i


def parse_pick_label(label):
    """One post in the "Submit clip" form's list (Nov 2026). Its label reads
    "<caption>, @handle, Posted 1 min ago, 28 min left" -> {"text", "handle", "posted", "left"}."""
    m = re.match(r"^(.*), @([\w.]+), Posted (.+?), (\d+) min left\s*$", label or "", re.S)
    if not m:
        return {"text": (label or "").strip(), "handle": "", "posted": "", "left": None}
    return {"text": m.group(1).strip(), "handle": m.group(2), "posted": m.group(3), "left": int(m.group(4))}


def best_pick(labels, texts, threshold=0.55):
    """Index of the listed post that matches our caption/title/hook best, or None if none is close enough."""
    from .clipqueue import similarity
    best, best_i = 0.0, None
    for i, lab in enumerate(labels):
        t = parse_pick_label(lab)["text"]
        score = max([similarity(x, t) for x in texts if x] or [0.0])
        if score > best:
            best, best_i = score, i
    return best_i if best >= threshold else None


# ---------------------------------------------------------------- the browser
class Site:
    """with Site() as s: s.campaigns() ... Opens the bot's own Chrome profile (logged in once by Elie)."""

    def __init__(self, headless=False):
        self.headless = headless

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        PROFILE.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=self.headless, viewport={"width": 1280, "height": 860})
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def _pause(self, lo=2.5, hi=6.0):
        time.sleep(random.uniform(lo, hi))

    def go(self, path):
        self.page.goto(path if path.startswith("http") else SITE + path, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_load_state("networkidle", timeout=30000)
        self._pause()
        if "/login" in self.page.url or self.page.get_by_role("link", name=re.compile("^log ?in$", re.I)).count():
            raise NotLoggedIn("Content Rewards isn't logged in on the bot's Chrome window")
        self.dismiss_popups()
        return self.page.inner_text("main") if self.page.locator("main").count() else self.page.inner_text("body")

    def campaigns(self):
        """Joined campaigns with their page details: [{"name", "status", "url", **parse_campaign_page}]."""
        out = []
        listed = parse_campaign_list(self.go("/c/campaigns"))
        for c in listed:
            self.go("/c/campaigns")
            btn = self.page.get_by_role("button", name=f"Preview {c['name']}")
            if not btn.count():
                out.append(c)
                continue
            btn.first.click()
            self.page.wait_for_url(re.compile(r"/c/campaigns/[0-9a-f-]{20,}"), timeout=30000)
            self._pause()
            c["url"] = self.page.url
            c.update(parse_campaign_page(self.page.inner_text("main")))
            out.append(c)
        return out

    def join(self, url):
        """Open a campaign (a ClippingAlpha /go/ link redirects to it) and click Join Campaign.
        Content Rewards joins on that one click (checked Nov 2026: no form). Returns the page details plus
        url, name, joined, and either elsewhere=True (the link isn't a Content Rewards campaign) or form=<text>
        (a popup asked for more than the click; it's closed and nothing in it is sent)."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            self.page.wait_for_url(re.compile(r"contentrewards\.com/c/campaigns/"), timeout=20000)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        self._pause()
        if "contentrewards.com" not in self.page.url:
            return {"elsewhere": True, "url": self.page.url, "joined": False, "bio": []}
        if "/login" in self.page.url:
            raise NotLoggedIn("Content Rewards isn't logged in on the bot's Chrome window")
        name = re.sub(r"\s*\|\s*Content Rewards\s*$", "", self.page.title() or "").strip()
        info = parse_campaign_page(self.page.inner_text("main"))
        if not info["joined"]:
            btn = self.page.locator("button:visible", has_text=re.compile(r"^\s*Join Campaign\s*$"))
            if not btn.count():
                raise RuntimeError("the campaign page has no Join Campaign button")
            btn.first.click()
            try:
                self.page.get_by_text("Joined", exact=True).first.wait_for(timeout=20000)
            except Exception:
                dlg = self.page.locator('[role="dialog"]')
                form = " ".join(dlg.last.inner_text().split())[:600] if dlg.count() else ""
                try:
                    self.page.screenshot(path=str(Path.home() / "Desktop" / "join_popup.png"))
                except Exception:
                    pass
                self.page.keyboard.press("Escape")
                return dict(info, url=self.page.url, name=name, joined=False,
                            form=form or "the Join button didn't finish joining")
            self._pause()
            info = parse_campaign_page(self.page.inner_text("main"))
        return dict(info, url=self.page.url, name=name, joined=True)

    def campaign(self, url):
        return dict(parse_campaign_page(self.go(url)), url=url)

    POPUP_CLOSE = re.compile(r"^(Not now|Maybe later|No thanks|Got it|Dismiss|Skip|Close)$", re.I)

    def dismiss_popups(self):
        """Close announcement popups ("Support is here", what's new...) that cover the page and block every click.
        Only buttons that close something are pressed; nothing is opened or sent."""
        for _ in range(3):
            dlg = self.page.locator('[role="dialog"]:visible, [role="alertdialog"]:visible')
            if not dlg.count():
                return
            btn = dlg.last.get_by_role("button", name=self.POPUP_CLOSE)
            try:
                if btn.count():
                    log.info("closing a Content Rewards popup: %s", " ".join(dlg.last.inner_text().split())[:80])
                    btn.first.click(timeout=5000)
                else:
                    self.page.keyboard.press("Escape")
            except Exception:
                self.page.keyboard.press("Escape")
            self._pause(0.8, 1.5)

    def _open_tab(self, name):
        """Click a Drafts tab. Its accessible name can carry an icon or the count ("Needs changes 1"), so match
        loosely, then fall back to the visible text."""
        pat = re.compile(rf"\b{re.escape(name)}\b", re.I)
        for loc in (self.page.get_by_role("tab", name=pat), self.page.get_by_role("button", name=pat),
                    self.page.get_by_text(name, exact=True)):
            try:
                if loc.count():
                    loc.first.click(timeout=10000)
                    self._pause(1.5, 3)
                    return True
            except Exception:
                continue
        raise RuntimeError(f"couldn't find the {name!r} tab on the Drafts page")

    def drafts(self):
        """Draft counts per tab, plus the newest reviewer note for each draft that needs changes."""
        counts = parse_draft_counts(self.go("/c/drafts"))
        notes = []
        if counts.get("needs changes"):
            try:
                notes = self._needs_changes_notes()
            except Exception as e:   # the counts still go out, with a plain "go look" note
                log.warning("couldn't read the reviewer notes: %s", e)
                notes = [{"who": "the reviewer", "when": "recently", "campaign": "",
                          "note": "(I couldn't read the note itself. Open Drafts > Needs changes on Content Rewards.)",
                          "card": f"unread-{counts['needs changes']}", "unread": True}]
        return {"counts": counts, "needs_changes": notes}

    def _needs_changes_notes(self):
        notes = []
        self._open_tab("Needs changes")
        cards = self.page.get_by_role("button", name=re.compile("^See revisions$"))
        for i in range(min(cards.count(), 10)):
            card_text = cards.nth(i).locator("xpath=ancestor::div[.//video][1]").inner_text(timeout=5000) \
                if cards.nth(i).locator("xpath=ancestor::div[.//video][1]").count() else ""
            cards.nth(i).click()
            self._pause(1.5, 3)
            t = parse_timeline(self.page.inner_text("body"))
            if t:
                t["campaign"] = _lines(card_text)[0] if card_text else ""
                t["card"] = " | ".join(_lines(card_text)[:3])
                notes.append(t)
            self.page.keyboard.press("Escape")
            self._pause(1, 2)
        return notes

    def earnings(self):
        """parse_earnings() of the Earnings page, with each section opened so every paid clip is listed."""
        self.go("/c/earnings")
        try:
            self.page.get_by_text(re.compile(r"^\$[\d,.]+$")).first.wait_for(timeout=20000)
        except Exception:
            pass
        self.page.wait_for_timeout(3000)
        for name in ("Queued", "Arriving", "Received"):   # open a section only if it has clips not shown yet
            text = self.page.inner_text("main")
            m = re.search(rf"^{name}\s*\n\s*\((\d+)\)", text, re.M)
            shown = sum(1 for it in parse_earnings(text)["items"] if it["section"] == name.lower())
            if m and int(m.group(1)) > shown:
                try:
                    self.page.get_by_text(name, exact=True).first.click()
                    self._pause(1, 2)
                except Exception:
                    pass
        return parse_earnings(self.page.inner_text("main"))

    def upload_revision(self, campaign, video):
        """Drafts > Needs changes: open the draft for this campaign, Upload revision, attach the new video, send.
        Raises (and sends nothing) if any step doesn't look as expected; the caller then hands it to Elie."""
        self.go("/c/drafts")
        self._open_tab("Needs changes")
        cards = self.page.get_by_role("button", name=re.compile("^See revisions$"))
        pick = None
        for i in range(min(cards.count(), 10)):
            box = cards.nth(i).locator("xpath=ancestor::div[.//video][1]")
            text = box.inner_text(timeout=5000) if box.count() else ""
            if not campaign or campaign.lower()[:20] in text.lower():
                pick = i
                break
        if pick is None:
            raise RuntimeError(f"no draft for {campaign} under Needs changes")
        cards.nth(pick).click()
        self._pause(1.5, 3)
        def newest():
            return max([int(n) for n in re.findall(r"Revision (\d+)", self.page.inner_text("body"))] or [0])
        before = newest()
        btn = self.page.get_by_role("button", name=re.compile(r"^Upload revision$", re.I)).filter(visible=True)
        if not btn.count():
            raise RuntimeError("the draft's timeline has no Upload revision button")
        # the button opens a file picker (checked Nov 2026); catch it so no Mac dialog opens, and hand it the video
        with self.page.expect_file_chooser(timeout=15000) as fc:
            btn.first.click()
        fc.value.set_files(str(video))
        self.page.wait_for_timeout(5000)
        send = self.page.locator("button:visible", has_text=re.compile(r"^\s*(Submit|Send)( revision| for review)?\s*$", re.I))
        if send.count():   # some versions ask to confirm; wait for the upload to finish first
            for _ in range(120):
                if send.last.is_enabled():
                    break
                time.sleep(2)
            send.last.click()
        for _ in range(90):   # done when the timeline shows a new revision or the draft leaves Needs changes
            body = self.page.inner_text("body")
            if newest() > before or re.search(r"Revision submitted|Pending review", body, re.I):
                self._pause()
                return True
            time.sleep(2)
        raise RuntimeError("the revision didn't show up on the draft after 3 minutes")

    def _approved_tab(self):
        self.go("/c/drafts")
        self._open_tab("Approved")
        return self.page.inner_text("main")

    def approved(self):
        """The approved drafts waiting for their live links."""
        return parse_approved_cards(self._approved_tab())

    def _open_submit(self, campaign, duration):
        """Approved tab -> the matching draft's "Submit clip" form."""
        cards = parse_approved_cards(self._approved_tab())
        i = pick_card(cards, campaign, duration)
        if i is None:
            raise RuntimeError(f"no approved draft for {campaign} ({duration}s) on the Drafts page")
        self.page.get_by_role("button", name="Submit link").nth(i).click()
        dialog = self.page.get_by_role("dialog", name="Submit clip")
        dialog.wait_for(timeout=30000)
        self._pause(1, 2)
        return dialog

    def _choose(self, dialog, keep):
        """The form lists your recent posts as toggle buttons and pre-selects one. Leave only `keep` selected
        (None: none, so the pasted link is what gets sent)."""
        cards = dialog.locator("button[aria-pressed]")
        for i in range(cards.count()):
            on = cards.nth(i).get_attribute("aria-pressed") == "true"
            if on != (i == keep):
                cards.nth(i).click()
                self._pause(0.4, 0.9)

    def _send(self, dialog, dry_run=False):
        """Tick "I've read the requirements" and press Submit clip. The real checkbox is hidden behind a styled one,
        so its label is clicked."""
        cb = dialog.locator("input[type=checkbox]")
        if cb.count() and not cb.first.is_checked():
            try:
                dialog.get_by_text(re.compile(r"read the requirements", re.I)).first.click(timeout=5000)
            except Exception:
                pass
            if not cb.first.is_checked():
                cb.first.check(force=True)
        self._pause(0.8, 1.6)
        if dry_run:
            self.page.keyboard.press("Escape")
            return False
        dialog.get_by_role("button", name="Submit clip").click()
        try:
            dialog.wait_for(state="detached", timeout=30000)
        except Exception:
            msg = " ".join(dialog.inner_text().split())[:300] if dialog.count() else ""
            self.page.keyboard.press("Escape")
            raise RuntimeError(f"Content Rewards didn't accept it: {msg}")
        self._pause()
        return True

    def pick_labels(self, campaign, duration):
        """The posts the form offers right now (only ones still inside the 30-minute window)."""
        dialog = self._open_submit(campaign, duration)
        cards = dialog.locator("button[aria-pressed]")
        labels = [cards.nth(i).get_attribute("aria-label") or "" for i in range(cards.count())]
        self.page.keyboard.press("Escape")
        return labels

    def submit_pick(self, campaign, duration, texts, dry_run=False):
        """Submit the listed post that matches our caption/title. Returns its label, or None if none matches yet."""
        dialog = self._open_submit(campaign, duration)
        cards = dialog.locator("button[aria-pressed]")
        labels = [cards.nth(i).get_attribute("aria-label") or "" for i in range(cards.count())]
        i = best_pick(labels, texts)
        if i is None:
            self.page.keyboard.press("Escape")
            return None
        self._choose(dialog, i)
        if dry_run:
            log.info("[dry run] would submit %r", labels[i][:80])
        self._send(dialog, dry_run)
        return labels[i]

    def submit_link(self, campaign, duration, url, dry_run=False):
        """Paste a live post link into the matching approved draft's form and send it.
        Returns True when Content Rewards accepts it; raises with the page's message when it doesn't."""
        dialog = self._open_submit(campaign, duration)
        self._choose(dialog, None)
        box = dialog.get_by_role("textbox", name="Clip link")
        box.wait_for(timeout=30000)
        box.fill(url)
        self._pause(0.8, 1.6)
        if dry_run:
            log.info("[dry run] link form filled for %s; not sending", campaign)
        return self._send(dialog, dry_run)

    def submit_draft(self, campaign_url, video, platforms=("tiktok", "youtube"), dry_run=False):
        """Upload a clip as a draft for the brand's pre-approval. Returns True once the form is sent.
        Every click stays inside the "Submit a draft" popup: the campaign page behind it has its own TikTok and
        YouTube labels, and clicking those hits the popup's dark backdrop instead."""
        self.go(campaign_url)
        self.page.get_by_role("button", name="Submit clip").first.click()
        form = self.page.locator('[role="dialog"]').filter(has_text="Submit a draft to").last
        form.wait_for(timeout=30000)
        self._pause(1, 2)
        for p in platforms:
            row = form.get_by_text(re.compile(rf"^{p}$", re.I))
            if not row.count():
                raise RuntimeError(f"the draft form has no {p} account to pick")
            row.first.click()
            self._pause(0.5, 1.5)
        form.locator("input[type=file]").first.set_input_files(str(video))
        self.page.wait_for_timeout(5000)
        send = form.get_by_role("button", name="Submit draft for review")
        send.wait_for(timeout=120000)
        if dry_run:
            shot = Path.home() / "Desktop" / "practice_draft_form.png"
            try:
                self.page.screenshot(path=str(shot))
                print(f"Screenshot of the filled form: {shot}", flush=True)
            except Exception:
                pass
            log.info("[dry run] draft form filled for %s; not sending", campaign_url)
            form.get_by_role("button", name="Cancel").click()
            return False
        for _ in range(120):   # the button stays disabled until the upload finishes
            if send.is_enabled():
                break
            time.sleep(2)
        send.click()
        form.wait_for(state="detached", timeout=120000)
        self._pause()
        return True


def login_window():
    """Open the bot's Chrome window on the login page and wait until Elie has logged in (or closes it)."""
    with Site() as s:
        s.page.goto(SITE + "/login")
        print("Log in to Content Rewards in the Chrome window that just opened. "
              "This waits until you're in (up to 10 minutes).", flush=True)
        for _ in range(300):
            time.sleep(2)
            try:   # a Google login can finish in a popup or another tab: look at every tab
                if any("contentrewards.com/c/" in p.url for p in s.ctx.pages):
                    print("Logged in. You can close this; the bot remembers the login.", flush=True)
                    time.sleep(3)
                    return True
            except Exception:
                return False
    return False
