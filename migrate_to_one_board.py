"""One-off: fold any existing per-group data onto the single board (chat_id 0).

Run once after deploying the shared-board version. Safe to run twice — rows
already on the board are left alone, and the newest record wins on conflict.
"""
import os, sqlite3, sys

DB = os.environ.get("DB_PATH", "roster.db")
GAME = 0

MOVE = {                      # table: how to pick a winner per user
    "players":   "updated",
    "nicknames": None,
    "dead":      "ts",
}
COPY = ["votes", "travels", "hunts", "members", "roadwork"]

def main():
    if not os.path.exists(DB):
        print(f"no database at {DB}"); return 1
    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys=OFF")
    moved = {}

    for table, order in MOVE.items():
        try:
            rows = db.execute(
                f"SELECT * FROM {table} WHERE chat_id <> ?", (GAME,)).fetchall()
        except sqlite3.OperationalError:
            continue
        cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
        n = 0
        for row in rows:
            data = dict(zip(cols, row))
            data["chat_id"] = GAME
            placeholders = ",".join("?" for _ in cols)
            db.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)})"
                f" VALUES ({placeholders})", [data[c] for c in cols])
            n += 1
        db.execute(f"DELETE FROM {table} WHERE chat_id <> ?", (GAME,))
        moved[table] = n

    for table in COPY:
        try:
            n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE chat_id <> ?",
                           (GAME,)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if table == "members":
            db.execute("INSERT OR REPLACE INTO settings (chat_id, key, value)"
                       " SELECT 0, 'group:' || chat_id, '' FROM members"
                       " WHERE chat_id < 0 GROUP BY chat_id")
        db.execute(f"UPDATE OR REPLACE {table} SET chat_id=? WHERE chat_id <> ?",
                   (GAME, GAME))
        moved[table] = n

    db.commit()
    for table, n in moved.items():
        print(f"  {table:<12} {n} row(s) folded onto the shared board")
    print("done")
    return 0

if __name__ == "__main__":
    sys.exit(main())
