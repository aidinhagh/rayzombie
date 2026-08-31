"""
worldmap.py — the board.

Transcribed from the hand-drawn map. Every box and every blank circle on that
drawing is a place; every line between them is a road. The blank circles have
no name of their own, so they are the oases.

    kind      drives which landmark the ride renders, and the scenery around it
    x, y      position on the original drawing, used for direction and distance

If a road here is wrong, fix the pair in ROADS — that is the only place the
layout lives. `/map` in the group prints what the bot currently believes.
"""

from __future__ import annotations

from collections import deque

# ---------------------------------------------------------------- the places

# id, Persian name, kind, x, y
PLACES: list[tuple[str, str, str, int, int]] = [
    ("euphrates",     "رود فرات",              "river",      512,  28),
    ("camel_station", "ایستگاه شتر",           "caravan",    990, 125),
    ("baghdad_bazaar", "بازار بغداد",          "market",      65, 140),
    ("sham_palace",   "قصر شام",               "palace",     530, 137),
    ("kaaba",         "کعبه",                  "kaaba",      750, 182),
    ("kufa_prison",   "زندان کوفه",            "prison",     265, 240),
    ("kufa_mosque",   "مسجد کوفه",             "mosque",     512, 268),
    ("medina_clinic", "دارالشفا مدینه",        "clinic",     655, 258),
    ("caravanserai",  "کاروانسرا",             "inn",        378, 289),
    ("baqi",          "قبرستان بقیع",          "cemetery",   605, 340),
    ("treasury",      "خزانه بیت‌المال",       "treasury",   220, 372),
    ("police",        "ایستگاه پلیس",          "guardpost",  355, 447),
    ("tavern",        "میخانه",                "tavern",     497, 457),
    ("damascus_arena", "میدان اسب‌سواری دمشق", "arena",      735, 422),
    ("hira",          "غار حرا",               "cave",       193, 561),
    ("medina_square", "میدان شهر مدینه",       "square",     918, 561),

    # the blank circles
    ("oasis1",  "واحهٔ ۱",  "oasis", 272,  68),
    ("oasis2",  "واحهٔ ۲",  "oasis", 771,  68),
    ("oasis3",  "واحهٔ ۳",  "oasis", 148, 200),
    ("oasis4",  "واحهٔ ۴",  "oasis", 338, 175),
    ("oasis5",  "واحهٔ ۵",  "oasis", 405, 218),
    ("oasis6",  "واحهٔ ۶",  "oasis", 665, 120),
    ("oasis7",  "واحهٔ ۷",  "oasis", 575, 207),
    ("oasis8",  "واحهٔ ۸",  "oasis", 750, 306),
    ("oasis9",  "واحهٔ ۹",  "oasis", 932, 357),
    ("oasis10", "واحهٔ ۱۰", "oasis", 112, 363),
    ("oasis11", "واحهٔ ۱۱", "oasis", 432, 369),
    ("oasis12", "واحهٔ ۱۲", "oasis", 273, 503),
    ("oasis13", "واحهٔ ۱۳", "oasis", 518, 507),
    ("oasis14", "واحهٔ ۱۴", "oasis", 594, 434),
    ("oasis15", "واحهٔ ۱۵", "oasis", 838, 469),
]

ROADS: list[tuple[str, str]] = [
    ("oasis1", "euphrates"),
    ("oasis1", "baghdad_bazaar"),
    ("oasis1", "oasis4"),
    ("euphrates", "oasis2"),
    ("oasis2", "camel_station"),
    ("oasis2", "oasis9"),
    ("camel_station", "oasis9"),

    ("baghdad_bazaar", "oasis3"),
    ("baghdad_bazaar", "oasis10"),
    ("oasis3", "kufa_prison"),
    ("oasis3", "oasis10"),
    ("oasis4", "sham_palace"),
    ("oasis4", "kufa_prison"),
    ("kufa_prison", "police"),

    ("sham_palace", "oasis6"),
    ("oasis6", "oasis7"),
    ("oasis6", "kaaba"),
    ("oasis7", "medina_clinic"),
    ("oasis7", "kufa_mosque"),
    ("oasis7", "oasis5"),
    ("oasis5", "caravanserai"),
    ("oasis5", "kufa_mosque"),
    ("oasis5", "oasis11"),
    ("kaaba", "oasis8"),
    ("medina_clinic", "baqi"),

    ("oasis10", "treasury"),
    ("oasis10", "hira"),
    ("treasury", "police"),
    ("treasury", "hira"),
    ("caravanserai", "oasis11"),
    ("oasis11", "police"),
    ("oasis11", "tavern"),
    ("oasis11", "oasis14"),
    ("police", "oasis12"),
    ("oasis12", "hira"),
    ("oasis12", "oasis13"),
    ("hira", "oasis13"),

    ("baqi", "oasis14"),
    ("oasis14", "oasis8"),
    ("oasis14", "damascus_arena"),
    ("oasis14", "tavern"),
    ("oasis8", "damascus_arena"),
    ("damascus_arena", "oasis15"),
    ("oasis9", "oasis15"),
    ("oasis9", "medina_square"),
    ("oasis15", "medina_square"),
    ("oasis13", "oasis15"),
    ("oasis13", "medina_square"),
]

NAME = {pid: name for pid, name, _, _, _ in PLACES}
KIND = {pid: kind for pid, _, kind, _, _ in PLACES}
POS = {pid: (x, y) for pid, _, _, x, y in PLACES}
IDS = [pid for pid, *_ in PLACES]

NEIGHBOURS: dict[str, set[str]] = {pid: set() for pid in IDS}
for _a, _b in ROADS:
    NEIGHBOURS[_a].add(_b)
    NEIGHBOURS[_b].add(_a)


def name_of(pid: str) -> str:
    return NAME.get(pid, pid)


def distances_from(start: str) -> dict[str, int]:
    """Road-hops from `start` to everywhere reachable."""
    seen = {start: 0}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        for nxt in NEIGHBOURS.get(here, ()):
            if nxt not in seen:
                seen[nxt] = seen[here] + 1
                queue.append(nxt)
    return seen


def reachable(start: str, steps: int, exact: bool = False) -> list[str]:
    """Where a roll of `steps` can take you.

    Default is "up to" — a 5 lets you stop anywhere within five roads. Set
    EXACT_STEPS in bot.py if you would rather a roll mean exactly that far.
    """
    dist = distances_from(start)
    out = [pid for pid, d in dist.items()
           if (d == steps if exact else 0 < d <= steps)]
    if not out and not exact:                     # isolated: allow neighbours
        out = sorted(NEIGHBOURS.get(start, ()))
    return sorted(out, key=lambda p: (dist[p], NAME[p]))


def find(text: str) -> str | None:
    """Match a place by id or Persian name, loosely."""
    from matching import normalize

    query = normalize(text)
    if not query:
        return None
    if text in NAME:
        return text
    for pid, name in NAME.items():
        if normalize(name) == query or normalize(pid) == query:
            return pid
    for pid, name in NAME.items():
        if query in normalize(name):
            return pid
    return None


def summary() -> str:
    lines = [f"{len(PLACES)} مکان، {len(ROADS)} جاده", ""]
    for pid, name, kind, _, _ in PLACES:
        links = "، ".join(sorted(NAME[n] for n in NEIGHBOURS[pid]))
        lines.append(f"• {name} → {links}")
    return "\n".join(lines)


def sanity() -> list[str]:
    """Problems worth knowing about before anyone plays."""
    problems = []
    known = set(IDS)
    for a, b in ROADS:
        if a not in known or b not in known:
            problems.append(f"road to nowhere: {a} — {b}")
    reach = distances_from(IDS[0])
    for pid in IDS:
        if pid not in reach:
            problems.append(f"unreachable: {pid}")
        if not NEIGHBOURS[pid]:
            problems.append(f"no roads at all: {pid}")
    return problems
