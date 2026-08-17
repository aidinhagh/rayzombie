"""
matching.py — match a written name against group members, across scripts.

The hard case: someone types "ممد" or "محمدرضا" and the member's Telegram
profile says "Mohammadreza N." or "@mrezaa". Persian script drops short
vowels, and Latin transliterations of Persian are wildly inconsistent
(Mohammad / Mohamad / Muhammed / Mohammed).

Trick: reduce BOTH scripts to the same consonant skeleton, dropping vowels
entirely and collapsing the sounds that transliterate inconsistently.

    محمد      -> mhmd
    Mohammad  -> mhmd
    Muhammed  -> mhmd
    شهرام     -> 1hrm        (1 = sh)
    Shahram   -> 1hrm

Digraphs map to digits so they stay single symbols: 1=sh 2=ch 3=kh 4=gh/q 5=zh
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# --------------------------------------------------------------- normalisation

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_CHAR_FIX = {
    "ك": "ک", "ي": "ی", "ى": "ی", "ﻯ": "ی", "ة": "ه", "ۀ": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ؤ": "و", "ئ": "ی",
}

_STRIP = re.compile(r"[\u064B-\u065F\u0670\u0640\u200c\u200d\u200e\u200f]")
_NONWORD = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)


def normalize(text: str | None) -> str:
    """Lowercased, punctuation-free, with Arabic letter shapes folded to Persian."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_DIGITS)
    t = "".join(_CHAR_FIX.get(c, c) for c in t)
    t = _STRIP.sub("", t)
    t = t.lower().replace("_", " ")
    t = _NONWORD.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------- skeletons

_FA = {
    "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j", "چ": "2", "ح": "h",
    "خ": "3", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "ژ": "5", "س": "s",
    "ش": "1", "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "", "غ": "4",
    "ف": "f", "ق": "4", "ک": "k", "گ": "g", "ل": "l", "م": "m", "ن": "n",
    "و": "v", "ه": "h", "ی": "", "ا": "", "ء": "",
}

_DIGRAPHS = [("kh", "3"), ("gh", "4"), ("sh", "1"), ("ch", "2"), ("zh", "5"),
             ("ph", "f"), ("th", "t"), ("ck", "k"), ("qu", "kv")]

_VOWELS = re.compile(r"[aeiouy']")
_KEEP = re.compile(r"[^a-z12345]")


def _latin_skeleton(chunk: str) -> str:
    s = re.sub(r"\d", "", chunk)
    for a, b in _DIGRAPHS:
        s = s.replace(a, b)
    s = s.replace("q", "4").replace("x", "3").replace("c", "k").replace("w", "v")
    s = _VOWELS.sub("", s)
    return _KEEP.sub("", s)


def skeleton(text: str) -> str:
    """Consonant skeleton of a name, whichever script it is written in."""
    out: list[str] = []
    latin: list[str] = []

    def flush() -> None:
        if latin:
            out.append(_latin_skeleton("".join(latin)))
            latin.clear()

    for ch in normalize(text):
        if "\u0600" <= ch <= "\u06ff":
            flush()
            out.append(_FA.get(ch, ""))
        elif ch.isalnum():
            latin.append(ch)
        else:
            flush()
    flush()
    # Mohammad/Mohamad, Hossein/Hosein — doubled consonants are a coin flip in
    # transliteration and never distinguish two real names.
    return re.sub(r"(.)\1+", r"\1", "".join(out))


def skeleton_variants(text: str) -> set[str]:
    """و and ه transliterate as consonant or vowel depending on the word, so
    treat v and h as optional rather than guessing."""
    base = skeleton(text)
    if not base:
        return set()
    # v: و is consonant or vowel depending on the word, so make it optional.
    # h: only a TRAILING one (silent final ه). Dropping every h would erase the
    # difference between محمد and احمدی.
    no_v = base.replace("v", "")
    raw = {base, no_v, re.sub(r"h$", "", base), re.sub(r"h$", "", no_v)}
    forms = {re.sub(r"(.)\1+", r"\1", f) for f in raw}
    return {f for f in forms if f}


# ---------------------------------------------------------------------- scoring

def _skeleton_score(q_forms: set[str], c_forms: set[str]) -> float:
    best = 0.0
    for q in q_forms:
        for c in c_forms:
            if q == c:
                return 0.92
            if len(q) >= 3 and (c.startswith(q) or q.startswith(c)):
                best = max(best, 0.80)
            best = max(best, SequenceMatcher(None, q, c).ratio() * 0.88)
    return best


def score_field(query: str, q_forms: set[str], field: str) -> float:
    """0..1 — how well `query` names the thing written in `field`."""
    q = normalize(query)
    c = normalize(field)
    if not q or not c:
        return 0.0

    if q == c:
        return 1.0

    tokens = c.split()
    if q in tokens:
        return 0.96
    # "ali" for "alireza", "mohammad" for "mohammadreza"
    if len(q) >= 3 and any(t.startswith(q) or q.startswith(t) for t in tokens):
        return 0.86
    if len(q) >= 4 and q in c:
        return 0.82

    return _skeleton_score(q_forms, skeleton_variants(field))


class Candidate:
    __slots__ = ("user_id", "first", "last", "username", "last_seen")

    def __init__(self, user_id, first, last, username, last_seen=0.0):
        self.user_id = user_id
        self.first = first or ""
        self.last = last or ""
        self.username = username or ""
        self.last_seen = last_seen or 0.0

    @property
    def display(self) -> str:
        name = " ".join(p for p in (self.first, self.last) if p).strip()
        return name or (f"@{self.username}" if self.username else str(self.user_id))

    def fields(self) -> list[tuple[str, str]]:
        full = " ".join(p for p in (self.first, self.last) if p)
        pairs = [("first", self.first), ("last", self.last),
                 ("full", full), ("username", self.username)]

        handle = normalize(self.username).replace(" ", "")
        for key in (handle, str(self.user_id)):
            for extra in EXTRA_NAMES.get(key, ()):
                pairs.append(("alias", extra))

        return [(kind, value) for kind, value in pairs if value]


# Colloquial short forms that no phonetic rule will ever recover.
# STRICTLY one nickname -> one canonical name. For "this person also goes by
# these four spellings", use EXTRA_NAMES below instead.
ALIASES = {
    "ممد": "محمد", "ممدرضا": "محمدرضا", "مموتی": "محمد",
    "ابی": "ابراهیم", "اکی": "اکبر", "اصی": "اصغر", "عبی": "عباس",
    "حسی": "حسین", "رضی": "رضا", "مسی": "مسعود", "مجی": "مجید",
    "نری": "نرگس", "زری": "زهرا", "فری": "فرشته", "سمی": "سمیرا",
    "moh": "mohammad", "mamad": "mohammad", "memad": "mohammad",
}

# Extra spellings for ONE specific person, when their Telegram name gives the
# matcher nothing to work with (a handle like "BellaCia0o7" for صادق).
# Key: their @username (no @, case-insensitive) OR their numeric user id.
# Value: a list of every way people write them. Get the id from /whois.
EXTRA_NAMES: dict[str, list[str]] = {
     "bellacia0o7": ["صادق", "صادخ", "sadegh", "sadekh"],
     "Ezrail":   ["عزی", "ezi","عزرائیل"],
    "ّ ":   ["داریوش", "darius","Darius","Dariush"]
}

# a first name is a stronger signal than a surname when scores are close
_FIELD_WEIGHT = {"first": 1.0, "alias": 1.0, "full": 0.99,
                 "username": 0.98, "last": 0.96}

THRESHOLD = 0.74


def best_match(query: str, candidates: list[Candidate]):
    """Return (Candidate, score) or (None, best_score) if nothing is confident."""
    q = normalize(query)
    if not q or not candidates:
        return None, 0.0

    handle = q.lstrip("@").replace(" ", "")

    queries = [query]
    alias = ALIASES.get(q)
    if alias:
        queries.append(alias)
    forms = [(text, skeleton_variants(text)) for text in queries]

    ranked = []
    for cand in candidates:
        score = 0.0
        if cand.username and normalize(cand.username).replace(" ", "") == handle:
            score = 1.0
        else:
            for kind, value in cand.fields():
                weight = _FIELD_WEIGHT[kind]
                for text, q_forms in forms:
                    score = max(score, score_field(text, q_forms, value) * weight)
        ranked.append((score, cand.last_seen, cand))

    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    top_score, _, top = ranked[0]
    if top_score < THRESHOLD:
        return None, top_score
    return top, top_score
