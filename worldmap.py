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

    # Not on the drawn map, not listed by /map, and not offered to anyone who
    # has not earned it. South of the cemetery.
    ("missile_silo", "سیلوی موشک", "silo", 605, 470),
]

SECRET = {"missile_silo"}

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

    ("baqi", "missile_silo"),          # the way in, and the way back out
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


NAMED = [pid for pid, _, kind, _, _ in PLACES
         if kind != "oasis" and pid not in SECRET]


def landmarks_near(pid: str, want: int = 2) -> list[str]:
    """The nearest named places, for describing an oasis by its surroundings."""
    seen = {pid}
    ring = [pid]
    found: list[str] = []
    while ring and len(found) < want:
        nxt = []
        for here in ring:
            for other in sorted(NEIGHBOURS.get(here, ())):
                if other in seen:
                    continue
                seen.add(other)
                if KIND.get(other) != "oasis":
                    found.append(other)
                else:
                    nxt.append(other)
        ring = nxt
    return found[:want]


def describe(pid: str) -> str:
    """'واحهٔ ۸ (بین کعبه و میدان اسب‌سواری دمشق)' — an oasis is only meaningful
    by what it sits between."""
    name = name_of(pid)
    if KIND.get(pid) != "oasis":
        return name
    near = landmarks_near(pid, 2)
    if not near:
        return name
    if len(near) == 1:
        return f"{name} (نزدیک {name_of(near[0])})"
    return f"{name} (بین {name_of(near[0])} و {name_of(near[1])})"


def short_describe(pid: str, limit: int = 60) -> str:
    text = describe(pid)
    return text if len(text) <= limit else name_of(pid)


def build_graph(closed=(), extra=()) -> dict[str, set[str]]:
    """The road network with a group's own changes applied.

    Roads can be destroyed and rebuilt during play, so nothing may assume the
    static ROADS list — every lookup takes the graph it should use.
    """
    adj = {pid: set(nbrs) for pid, nbrs in NEIGHBOURS.items()}
    for a, b in extra:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)
    for a, b in closed:
        if a in adj and b in adj:
            adj[a].discard(b)
            adj[b].discard(a)
    return adj


def distances_from(start: str, adj=None) -> dict[str, int]:
    """Road-hops from `start` to everywhere reachable."""
    adj = adj or NEIGHBOURS
    seen = {start: 0}
    queue = deque([start])
    while queue:
        here = queue.popleft()
        for nxt in adj.get(here, ()):
            if nxt not in seen:
                seen[nxt] = seen[here] + 1
                queue.append(nxt)
    return seen


def reachable(start: str, steps: int, exact: bool = False, adj=None) -> list[str]:
    """Where a roll of `steps` can take you.

    Default is "up to" — a 5 lets you stop anywhere within five roads. Set
    EXACT_STEPS in bot.py if you would rather a roll mean exactly that far.
    """
    adj = adj or NEIGHBOURS
    dist = distances_from(start, adj)
    out = [pid for pid, d in dist.items()
           if (d == steps if exact else 0 < d <= steps)]
    if not out and not exact:                     # cut off: allow neighbours
        out = sorted(adj.get(start, ()))
    return sorted(out, key=lambda p: (dist.get(p, 99), NAME[p]))


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


def summary(adj=None) -> str:
    """The map as it stands. Secret places are left out entirely."""
    adj = adj or NEIGHBOURS
    shown = [p for p in PLACES if p[0] not in SECRET]
    edges = sum(len([n for n in adj[p[0]] if n not in SECRET]) for p in shown) // 2
    lines = [f"{len(shown)} مکان، {edges} جاده", ""]
    for pid, name, kind, _, _ in shown:
        links = "، ".join(sorted(NAME[n] for n in adj[pid] if n not in SECRET))
        label = describe(pid) if kind == "oasis" else name
        lines.append(f"• {label} → {links or '—'}")
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