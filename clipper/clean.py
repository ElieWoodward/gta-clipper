"""Mask strong swear words and slurs in on-screen text and post text (captions, hook, punchline, title, caption).
Heavy profanity in on-screen text early in a Short is one of the things YouTube age-restricts for, and an
age-restricted Short barely reaches the Shorts feed and can't earn. The audio is left as it is (most clip
channels do the same). Turned off with posting.mask_profanity: false."""
import re

# word stems; a match keeps the first letter and stars the rest of the stem: "fucking" -> "f***ing"
STEMS = ["motherfuck", "fuck", "shit", "bitch", "cunt", "nigga", "nigger", "faggot", "fag", "retard", "pussy",
         "whore", "slut", "dick", "cock", "asshole", "bastard"]
_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted(STEMS, key=len, reverse=True)) + r")", re.I)
# stems that are also parts of ordinary words: only masked as a whole word (plus common endings)
_WHOLE = {"fag", "dick", "cock", "pussy", "bastard", "retard"}
_WHOLE_RE = re.compile(r"^(s|es|ed|ing|y|ies|head|heads)?$", re.I)


def mask(text):
    if not text:
        return text

    def rep(m):
        stem = m.group(1)
        if stem.lower() in _WHOLE:
            rest = text[m.end():]
            tail = re.match(r"[a-z]*", rest, re.I).group(0)
            if not _WHOLE_RE.match(tail):
                return stem   # "dickens", "cockpit", "fagin": leave alone
        return stem[0] + "*" * (len(stem) - 1)
    return _RE.sub(rep, text)


def enabled(cfg):
    return (cfg.get("posting") or {}).get("mask_profanity", True) is not False
