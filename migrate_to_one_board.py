"""Fold any per-group rows onto the shared board.

You should not normally need this: the bot does the same thing at startup, and
it is idempotent. Keep it for running against a copy of the database, or for a
host where you would rather migrate before letting the bot start.

    DB_PATH=/data/roster.db python migrate_to_one_board.py
"""
import os
import sys

import roster


def main() -> int:
    if not os.path.exists(roster.DB_PATH):
        print(f"no database at {roster.DB_PATH}")
        return 1
    moved = roster.fold_onto_board(0)
    for table, n in moved.items():
        print(f"  {table:<12} {n} row(s) moved")
    print("done" if any(moved.values()) else "already on one board — nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())