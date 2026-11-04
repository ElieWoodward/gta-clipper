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

    def campaign(self, url):
        return dict(parse_campaign_page(self.go(url)), url=url)

    def drafts(self):
        """Draft counts per tab, plus the newest reviewer note for each draft that needs changes."""
        counts = parse_draft_counts(self.go("/c/drafts"))
        notes = []
        if counts.get("needs changes"):
            self.page.get_by_role("tab", name=re.compile("^Needs changes")).first.click()
            self._pause(1.5, 3)
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
        return {"counts": counts, "needs_changes": notes}

    def submit_draft(self, campaign_url, video, platforms=("tiktok", "youtube"), dry_run=False):
        """Upload a clip as a draft for the brand's pre-approval. Returns True once the form is sent."""
        self.go(campaign_url)
        self.page.get_by_role("button", name="Submit clip").first.click()
        self.page.get_by_text("Submit a draft to").wait_for(timeout=30000)
        self._pause(1, 2)
        for p in platforms:
            row = self.page.get_by_text(re.compile(rf"^{p}$", re.I))
            if row.count():
                row.first.click()
                self._pause(0.5, 1.5)
        self.page.locator("input[type=file]").first.set_input_files(str(video))
        self.page.wait_for_timeout(5000)
        send = self.page.get_by_role("button", name="Submit draft for review")
        send.wait_for(timeout=120000)
        if dry_run:
            log.info("[dry run] draft form filled for %s; not sending", campaign_url)
            self.page.get_by_role("button", name="Cancel").click()
            return False
        for _ in range(120):   # the button stays disabled until the upload finishes
            if send.is_enabled():
                break
            time.sleep(2)
        send.click()
        self.page.get_by_text("Submit a draft to").wait_for(state="detached", timeout=120000)
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
