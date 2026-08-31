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
PHOTO_TTL = 24 * 3600          # a known avatar is re-checked once a day
PHOTO_MISS_TTL = 2 * 3600      # "no avatar" is retried much sooner — privacy
                               # settings change, and people add photos

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
            CREATE TABLE IF NOT EXISTS votes (
                chat_id    INTEGER NOT NULL,
                ts         REAL NOT NULL,
                voter_id   INTEGER NOT NULL,
                target_key TEXT NOT NULL,
                label      TEXT
            );
            CREATE INDEX IF NOT EXISTS votes_window
                ON votes (chat_id, ts);
            CREATE TABLE IF NOT EXISTS players (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name    TEXT,
                place   TEXT,
                updated REAL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS travels (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name    TEXT,
                place   TEXT,
                origin  TEXT,
                roll    INTEGER,
                ts      REAL
            );
            CREATE INDEX IF NOT EXISTS travels_place
                ON travels (chat_id, place, ts);
            CREATE TABLE IF NOT EXISTS trips (
                token   TEXT PRIMARY KEY,
                payload TEXT,
                ts      REAL
            );
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER NOT NULL,
                key     TEXT NOT NULL,
                value   TEXT,
                PRIMARY KEY (chat_id, key)
            );
            CREATE TABLE IF NOT EXISTS handles (
                handle  TEXT PRIMARY KEY,
                user_id INTEGER
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
        row = _db().execute(
            "SELECT unique_id, fetched FROM photo_meta WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return False
    ttl = PHOTO_TTL if row[0] else PHOTO_MISS_TTL
    return (time.time() - (row[1] or 0)) < ttl


def drop_photo(user_id: int) -> None:
    """Forget what we know about a user's avatar, forcing a re-fetch."""
    with _lock:
        _db().execute("DELETE FROM photo_meta WHERE user_id=?", (user_id,))
        _db().commit()


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


# ------------------------------------------------------------------ vote log

VOTE_WINDOW = 24 * 3600


def record_vote(chat_id: int, voter_id: int, target_key: str,
                label: str) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO votes (chat_id, ts, voter_id, target_key, label)"
            " VALUES (?,?,?,?,?)",
            (chat_id, time.time(), voter_id, target_key, label),
        )
        _db().execute("DELETE FROM votes WHERE ts < ?",
                      (time.time() - 7 * 24 * 3600,))
        _db().commit()


def delete_votes_for(chat_id: int, target_key: str, label: str,
                     window: float = VOTE_WINDOW) -> int:
    """Remove every vote cast for one person inside the window."""
    since = time.time() - window
    with _lock:
        cur = _db().execute(
            """DELETE FROM votes
                WHERE chat_id=? AND ts>=? AND (target_key=? OR label=?)""",
            (chat_id, since, target_key, label),
        )
        _db().commit()
        return cur.rowcount


def delete_votes_by(chat_id: int, voter_id: int,
                    window: float = VOTE_WINDOW) -> int:
    """Remove a person's own vote, freeing them to vote again."""
    since = time.time() - window
    with _lock:
        cur = _db().execute(
            "DELETE FROM votes WHERE chat_id=? AND voter_id=? AND ts>=?",
            (chat_id, voter_id, since),
        )
        _db().commit()
        return cur.rowcount


def clear_votes(chat_id: int, window: float = VOTE_WINDOW) -> int:
    since = time.time() - window
    with _lock:
        cur = _db().execute("DELETE FROM votes WHERE chat_id=? AND ts>=?",
                            (chat_id, since))
        _db().commit()
        return cur.rowcount


def last_vote(chat_id: int, voter_id: int, window: float = VOTE_WINDOW):
    """(label, seconds_until_they_can_vote_again) or None if they are free."""
    since = time.time() - window
    with _lock:
        row = _db().execute(
            """SELECT label, ts FROM votes
                WHERE chat_id=? AND voter_id=? AND ts >= ?
             ORDER BY ts DESC LIMIT 1""",
            (chat_id, voter_id, since),
        ).fetchone()
    if not row:
        return None
    return row[0], max(0.0, row[1] + window - time.time())


def tally(chat_id: int, window: float = VOTE_WINDOW):
    """[(label, votes)] for the window, counting each voter once per target."""
    since = time.time() - window
    with _lock:
        rows = _db().execute(
            """SELECT target_key,
                      COUNT(DISTINCT voter_id) AS n,
                      (SELECT label FROM votes v2
                        WHERE v2.chat_id = v.chat_id
                          AND v2.target_key = v.target_key
                          AND v2.ts >= ?
                        ORDER BY v2.ts DESC LIMIT 1) AS label
                 FROM votes v
                WHERE chat_id = ? AND ts >= ?
             GROUP BY target_key
             ORDER BY n DESC, label ASC""",
            (since, chat_id, since),
        ).fetchall()
        voters = _db().execute(
            "SELECT COUNT(DISTINCT voter_id) FROM votes WHERE chat_id=? AND ts>=?",
            (chat_id, since),
        ).fetchone()[0]
    return [(row[2] or row[0], row[1]) for row in rows], voters


# --------------------------------------------------------- @handle -> user id

def known_handle(handle: str) -> int | None:
    with _lock:
        row = _db().execute("SELECT user_id FROM handles WHERE handle=?",
                            (handle.lower(),)).fetchone()
    return row[0] if row and row[0] else None


def store_handle(handle: str, user_id: int | None) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO handles (handle, user_id) VALUES (?,?)",
            (handle.lower(), user_id),
        )
        _db().commit()


# ------------------------------------------------------------ chat settings

def get_setting(chat_id: int, key: str) -> str | None:
    with _lock:
        row = _db().execute(
            "SELECT value FROM settings WHERE chat_id=? AND key=?",
            (chat_id, key),
        ).fetchone()
    return row[0] if row else None


def set_setting(chat_id: int, key: str, value: str | None) -> None:
    with _lock:
        if value is None:
            _db().execute("DELETE FROM settings WHERE chat_id=? AND key=?",
                          (chat_id, key))
        else:
            _db().execute(
                "INSERT INTO settings (chat_id, key, value) VALUES (?,?,?)"
                " ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value",
                (chat_id, key, value),
            )
        _db().commit()


# ------------------------------------------------------------ board position

def get_place(chat_id: int, user_id: int) -> str | None:
    with _lock:
        row = _db().execute(
            "SELECT place FROM players WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    return row[0] if row else None


def set_place(chat_id: int, user_id: int, name: str, place: str) -> None:
    with _lock:
        _db().execute(
            """INSERT INTO players (chat_id, user_id, name, place, updated)
               VALUES (?,?,?,?,?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET
                 name=excluded.name, place=excluded.place,
                 updated=excluded.updated""",
            (chat_id, user_id, name, place, time.time()),
        )
        _db().commit()


def log_travel(chat_id: int, user_id: int, name: str, place: str,
               origin: str | None, roll: int | None) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO travels (chat_id, user_id, name, place, origin, roll, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (chat_id, user_id, name, place, origin, roll, time.time()),
        )
        _db().commit()


def all_players(chat_id: int) -> list[tuple[int, str, str, float]]:
    with _lock:
        return _db().execute(
            """SELECT user_id, name, place, updated FROM players
                WHERE chat_id=? ORDER BY updated DESC""",
            (chat_id,),
        ).fetchall()


def others_at(chat_id: int, place: str, exclude: int, limit: int = 3
              ) -> list[str]:
    """Names of other players who have gone to the same place."""
    with _lock:
        rows = _db().execute(
            """SELECT name FROM players
                WHERE chat_id=? AND place=? AND user_id<>? AND name IS NOT NULL
             ORDER BY updated DESC LIMIT ?""",
            (chat_id, place, exclude, limit),
        ).fetchall()
    return [r[0] for r in rows]


def clear_place(chat_id: int, user_id: int) -> int:
    with _lock:
        cur = _db().execute("DELETE FROM players WHERE chat_id=? AND user_id=?",
                            (chat_id, user_id))
        _db().commit()
        return cur.rowcount


def clear_all_places(chat_id: int) -> int:
    with _lock:
        cur = _db().execute("DELETE FROM players WHERE chat_id=?", (chat_id,))
        _db().execute("DELETE FROM travels WHERE chat_id=?", (chat_id,))
        _db().commit()
        return cur.rowcount


# --------------------------------------------------- trips for the mini app

TRIP_TTL = 6 * 3600


def save_trip(token: str, payload: str) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO trips (token, payload, ts) VALUES (?,?,?)",
            (token, payload, time.time()),
        )
        _db().execute("DELETE FROM trips WHERE ts < ?", (time.time() - TRIP_TTL,))
        _db().commit()


def load_trip(token: str) -> str | None:
    with _lock:
        row = _db().execute("SELECT payload FROM trips WHERE token=?",
                            (token,)).fetchone()
    return row[0] if row else None


# ------------------------------------------- which board a DM should act on

def chats_for_user(user_id: int) -> list[int]:
    """Group chats where this person is known — as a player or just as a member,
    most recent first."""
    with _lock:
        rows = _db().execute(
            """SELECT chat_id, MAX(seen) FROM (
                   SELECT chat_id, updated AS seen FROM players WHERE user_id=?
                   UNION ALL
                   SELECT chat_id, last_seen AS seen FROM members WHERE user_id=?
               ) WHERE chat_id < 0
             GROUP BY chat_id ORDER BY 2 DESC""",
            (user_id, user_id),
        ).fetchall()
    return [r[0] for r in rows]
