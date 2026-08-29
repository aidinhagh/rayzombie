"""
seed.py — the people this group actually votes about.

Each entry: a Telegram @username (no @, case-insensitive) or a numeric user id,
plus every way people write that person's name. The matcher still does the
fuzzy/cross-script work on top of these, so you only need the spellings that a
phonetic rule would never reach — nicknames, in-jokes, second names.

`immune` marks someone whose votes get a lightning bolt and are never counted.
"""

from __future__ import annotations

import matching

# key, [names...], immune
PEOPLE: list[tuple[str, list[str], bool]] = [
    ("informer_mohammad",     ["اینفرمر", "ممد", "محمد پیروز", "درخراب",
                               "informer", "mohammad"], False),
    ("theforgottendreamer74", ["محو", "شتر", "mahv", "shotor"], False),
    ("astromasoud",           ["مسعود", "masoud"], False),
    ("meysam_khaan",          ["میثم", "meysam", "میثم خان"], False),
    ("ezi110",                ["عزی", "عزرائیل", "ezi", "ezrail"], False),
    ("bellacia0o7",           ["صادق", "صادخ", "sadegh", "sadekh"], False),
    ("amircrowley",           ["امیرپیمان", "امیر پیمان", "امیر", "peyman"], False),
    ("mttaherpour",           ["طاهر", "taher", "طاهرپور"], False),
    ("mrbayati",              ["مرتضی", "مری", "موری", "morteza", "bayati"], False),
    ("miladshah1990",         ["میلاد", "milad"], False),
    ("a12m_s",                ["امیرحسین", "امیر حسین", "amirhossein"], False),
    ("moeeneon",              ["معین", "moein"], False),
    ("kbrabb",                ["دلارام", "delaram"], False),
    ("farazfe",               ["فراز", "faraz"], False),
    ("527341236",             ["داریوش", "dariush"], False),
    ("109009789",             ["سیاوش", "siavash"], False),
    ("aidinhagh",             ["آیدین", "ایدین", "aidin", "aydin"], True),
]


def _key(value: str) -> str:
    return matching.normalize(value).replace(" ", "")


NAMES: dict[str, list[str]] = {_key(k): names for k, names, _ in PEOPLE}
IMMUNE: set[str] = {_key(k) for k, _, immune in PEOPLE if immune}
DISPLAY: dict[str, str] = {_key(k): names[0] for k, names, _ in PEOPLE}


def install() -> None:
    """Feed the spellings into the matcher so roster rows pick them up."""
    for key, names in NAMES.items():
        matching.EXTRA_NAMES.setdefault(key, []).extend(names)


def candidates() -> list[matching.Candidate]:
    """Seed people as match candidates, for those who have not spoken yet.

    user_id 0 means "not resolved" — the bot will try to look the handle up
    when it needs an avatar.
    """
    out = []
    for key, names, _ in PEOPLE:
        normalized = _key(key)
        if normalized.isdigit():
            out.append(matching.Candidate(int(normalized), names[0], "", "", 0.0))
        else:
            out.append(matching.Candidate(0, names[0], "", normalized, 0.0))
    return out


def key_for(candidate: matching.Candidate) -> str | None:
    """Which seed entry (if any) this candidate is."""
    handle = _key(candidate.username or "")
    if handle and handle in NAMES:
        return handle
    uid = str(candidate.user_id)
    return uid if uid in NAMES else None


def is_immune(candidate: matching.Candidate) -> bool:
    return key_for(candidate) in IMMUNE


def display_for(candidate: matching.Candidate) -> str | None:
    key = key_for(candidate)
    return DISPLAY.get(key) if key else None
