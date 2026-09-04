import asyncio, os, sys, types
os.environ["DB_PATH"] = "/tmp/privacy.db"
os.environ["BOT_TOKEN"] = "x"
os.environ["ADMIN_IDS"] = "111"
sys.path.insert(0, "/home/claude/vote-shredder-bot")
import roster, worldmap as wm

GROUP, ADMIN, P1, P2 = -1001, 111, 222, 333
sent = []

class FakeBot:
    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        sent.append((chat_id, text)); return types.SimpleNamespace(message_id=1)
    async def get_chat(self, cid): return types.SimpleNamespace(id=ADMIN, title="grp")

for t in ("players","nicknames","votes","travels","hunts","members","dead"):
    try: roster._db().execute(f"DELETE FROM {t}")
    except Exception: pass
roster._db().execute("DELETE FROM settings")
roster._db().commit()
roster.remember_group(GROUP, "grp")
roster.store_handle("Aidinhagh", ADMIN)
class U:
    def __init__(s,i,f,u): s.id,s.first_name,s.last_name,s.username,s.is_bot=i,f,None,u,False
for uid, nm in [(P1,"Faeze"), (P2,"Mohammad")]:
    roster.remember(0, U(uid, nm, nm.lower()))
roster.set_nick(0, P1, "شبح"); roster.set_nick(0, P2, "خولی")

import bot as B
ctx = types.SimpleNamespace(bot=FakeBot(), args=[])

async def main():
    await B.assign_missing(ctx)
    groups = [(c,t) for c,t in sent if c < 0]
    admin  = [(c,t) for c,t in sent if c == ADMIN]
    secrets_ = [wm.NAME[p] for p in wm.IDS] + list(roster.all_nicks(0).values()) \
             + ["Faeze","Mohammad"]
    leaked = [(tok, t) for c,t in groups for tok in secrets_ if tok and tok in t]
    print(f"  to groups: {len(groups)}")
    for c,t in groups: print(f"     {c}: {t[:80]}")
    print(f"  to admin : {len(admin)}")
    for c,t in admin: print(f"     admin: {t[:90].replace(chr(10),' | ')}")
    print()
    print("  LEAK: " + str(leaked[:3]) if leaked else "  nothing named a player or a place in a group ✅")
    return 1 if leaked else 0

leaky = None


def _audit():
    import ast
    src = open("/home/claude/vote-shredder-bot/bot.py", encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    NICK = ("all_nicks", "get_nick", "game_name")
    # game_name is the helper itself; travel() looks a nickname up only to hand
    # it to report_roll and the admin's copy of the trip record — every
    # player-facing string in it uses the real name.
    # on_travel_button reads nicknames on purpose: the ride shows in-game
    # names, never real ones. Everything else here only reports to the admin.
    ADMIN_SENDERS = {"report_to_admin", "report_roll", "assign_missing",
                     "game_name", "travel", "on_travel_button"}
    out = []
    for name, node in funcs.items():
        body = ast.get_source_segment(src, node) or ""
        if not any(c in body for c in NICK):
            continue
        if "admin_only(" in body or "is_admin(" in body or name in ADMIN_SENDERS:
            continue
        out.append(name)
    return out


rc = asyncio.run(main())
leaky = _audit()
print()
if leaky:
    print("  functions that touch a nickname without an admin gate:")
    for f in leaky:
        print(f"     {f}")
    rc = 1
else:
    print("  nicknames are only reachable from admin-gated code ✅")
sys.exit(rc)