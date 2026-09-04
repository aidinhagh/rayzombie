"""Check that the things I claim are in bot.py actually are.

Silent string replacements are how a fix ends up announced but not shipped.
"""
import sys

MUST_HAVE = [
    ("setloc uses the placeholder resolver", "resolve_person(context.bot, GAME, who)"),
    ("setloc no longer refuses unseen people", None),   # checked as absent below
    ("pasted <brackets> are stripped", "def clean_arg("),
    ("nick argument is cleaned", "nick = clean_arg(nick)[:20]"),
    ("unknown @handle -> placeholder", 'return roster.placeholder_id(handle), f"@{handle}"'),
    ("known-but-idless -> placeholder", "uid = roster.placeholder_id(target.username)"),
    ("placeholders merge on first sight", "roster.merge_handle, GAME,"),
    ("board is private", "PRIVATE_BOARD = True"),
    ("one shared board", "GAME = 0"),
    ("draw runs without arguments", "async def chat_graph(chat_id: int = GAME)"),
    ("startup folds old data", "roster.fold_onto_board(GAME)"),
    ("tracking runs in private chats too", "async def bind_user("),
    ("travel binds the player", "await bind_user(message.chat_id, user)"),
    ("the ride uses nicknames", '"name": ride_name'),
    ("ride companions by nickname", 'nicks.get(uid) or "مسافر"'),
]
MUST_NOT_HAVE = [
    ("setloc's old refusal", "این نفر هنوز آی‌دی ندارد — یک پیام بدهد."),
    ("per-group data lookups", "roster.set_place, chat_id"),
    ("the group-only early return", "message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):\n        return"),
]

src = open("bot.py", encoding="utf-8").read()
bad = 0
for label, needle in MUST_HAVE:
    if needle is None:
        continue
    ok = needle in src
    print(f"  {'ok  ' if ok else 'MISS'}  {label}")
    bad += 0 if ok else 1
for label, needle in MUST_NOT_HAVE:
    ok = needle not in src
    print(f"  {'ok  ' if ok else 'STILL THERE'}  {label} removed")
    bad += 0 if ok else 1
print()
print("everything claimed is present" if not bad else f"{bad} claim(s) not actually in the file")
sys.exit(1 if bad else 0)
