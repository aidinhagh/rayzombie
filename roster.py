"""
roster.py — who is in this group, and what do they look like.

The Bot API has no "list all members" call, so the roster is built by watching
traffic: everyone who speaks, joins, or is replied to gets recorded. Admins are
pulled in once via getChatAdministrators to give it a head start.

Everything here is plain blocking sqlite3; the bot calls it via
asyncio.to_thread so the event loop never waits on disk.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from matching import Candidate

DB_PATH = os.environ.get("DB_PATH", "roster.db")
PHOTO_TTL = 24 * 3600          # re-check a user's avatar once a day

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        directory = os.path.dirname(os.path.abspath(DB_PATH))
        os.makedirs(directory, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS members (
                chat_id  INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                first    TEXT,
                last     TEXT,
                username TEXT,
                last_seen REAL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS photo_meta (
                user_id   INTEGER PRIMARY KEY,
                unique_id TEXT,
                fetched   REAL
            );
            CREATE TABLE IF NOT EXISTS photo_blob (
                unique_id TEXT PRIMARY KEY,
                data      BLOB
            );
            """
        )
        _conn.commit()
    return _conn


# ------------------------------------------------------------------- roster

def remember(chat_id: int, user) -> None:
    """Record (or refresh) one member. `user` is a telegram.User."""
    if user is None or getattr(user, "is_bot", False):
        return
    with _lock:
        _db().execute(
            """INSERT INTO members (chat_id, user_id, first, last, username, last_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET
                 first=excluded.first, last=excluded.last,
                 username=excluded.username, last_seen=excluded.last_seen""",
            (chat_id, user.id, user.first_name, user.last_name,
             user.username, time.time()),
        )
        _db().commit()


def members(chat_id: int) -> list[Candidate]:
    with _lock:
        rows = _db().execute(
            """SELECT user_id, first, last, username, last_seen
               FROM members WHERE chat_id=? ORDER BY last_seen DESC""",
            (chat_id,),
        ).fetchall()
    return [Candidate(*row) for row in rows]


def count(chat_id: int) -> int:
    with _lock:
        row = _db().execute(
            "SELECT COUNT(*) FROM members WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row[0] if row else 0


def forget(chat_id: int, user_id: int) -> None:
    with _lock:
        _db().execute("DELETE FROM members WHERE chat_id=? AND user_id=?",
                      (chat_id, user_id))
        _db().commit()


# -------------------------------------------------------------- photo cache

def photo_is_fresh(user_id: int) -> bool:
    with _lock:
        row = _db().execute("SELECT fetched FROM photo_meta WHERE user_id=?",
                            (user_id,)).fetchone()
    return bool(row) and (time.time() - (row[0] or 0)) < PHOTO_TTL


def cached_photo(user_id: int) -> tuple[str | None, bytes | None]:
    """Return (unique_id, jpeg_bytes) for a user, or (None, None)."""
    with _lock:
        row = _db().execute(
            """SELECT m.unique_id, b.data FROM photo_meta m
               LEFT JOIN photo_blob b ON b.unique_id = m.unique_id
               WHERE m.user_id=?""",
            (user_id,),
        ).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def store_photo(user_id: int, unique_id: str | None, data: bytes | None) -> None:
    """unique_id None means 'checked, this user has no visible avatar'."""
    with _lock:
        db = _db()
        db.execute(
            """INSERT INTO photo_meta (user_id, unique_id, fetched) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 unique_id=excluded.unique_id, fetched=excluded.fetched""",
            (user_id, unique_id, time.time()),
        )
        if unique_id and data:
            db.execute(
                "INSERT OR REPLACE INTO photo_blob (unique_id, data) VALUES (?,?)",
                (unique_id, data),
            )
        db.commit()
