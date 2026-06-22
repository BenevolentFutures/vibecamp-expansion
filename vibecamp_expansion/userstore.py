"""Durable, SQLite-backed store of bot users — for audience tracking + broadcast.

Unlike the in-memory ``analytics`` module (hashed keys, resets on redeploy),
this persists **real** Telegram chat ids so the operator can broadcast to people
who've messaged the bot. It is the consented, opt-out audience list for the
"microphone" feature, and it survives restarts when pointed at a durable path
(e.g. a mounted Railway volume).

Privacy: this deliberately holds real chat ids (and the public @username when
available) — the minimum needed to send a message — and nothing about message
content. Scope it to the broadcast/audience feature.

The store is process-local SQLite with a lock, which is correct for the bot's
single-instance deployment. With ``path=":memory:"`` (the default) it is a
fresh, hermetic, in-memory DB — used by tests and as a safe no-volume fallback.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Optional


def _now_iso(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now if now is not None else time.time()))


class UserStore:
    """Persistent users table: chat_id, username, subscribed, counts, timestamps."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.Lock()
        # check_same_thread=False: the async handlers run on one loop thread, but
        # the lock guards every access so cross-thread use is also safe.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                username   TEXT,
                subscribed INTEGER NOT NULL DEFAULT 1,
                messages   INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT,
                last_seen  TEXT
            )
            """
        )
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self._db.commit()

    # --- recording ---------------------------------------------------------- #

    def seen(self, chat_id: int, username: Optional[str] = None, *, now: Optional[float] = None) -> bool:
        """Record a message from a user (upsert). Returns True if newly seen.

        A brand-new user is auto-enrolled (subscribed=1, opt-out model). An
        existing user's ``subscribed`` flag is left untouched (we never silently
        re-subscribe someone who opted out).
        """
        ts = _now_iso(now)
        with self._lock:
            row = self._db.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            is_new = row is None
            if is_new:
                self._db.execute(
                    "INSERT INTO users (chat_id, username, subscribed, messages, first_seen, last_seen) "
                    "VALUES (?,?,1,1,?,?)",
                    (chat_id, username, ts, ts),
                )
            else:
                self._db.execute(
                    "UPDATE users SET messages=messages+1, last_seen=?, "
                    "username=COALESCE(?, username) WHERE chat_id=?",
                    (ts, username, chat_id),
                )
            self._db.commit()
        return is_new

    def add_cost(self, amount: float) -> None:
        """Accumulate estimated $ spend durably."""
        if not amount:
            return
        with self._lock:
            cur = float(self._meta_get("total_cost", "0") or "0")
            self._meta_set("total_cost", repr(cur + amount))
            self._db.commit()

    # --- subscription ------------------------------------------------------- #

    def set_subscribed(self, chat_id: int, subscribed: bool, *, now: Optional[float] = None) -> None:
        ts = _now_iso(now)
        with self._lock:
            # Ensure the row exists (e.g. /start before any message was recorded).
            self._db.execute(
                "INSERT INTO users (chat_id, subscribed, first_seen, last_seen) VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET subscribed=excluded.subscribed",
                (chat_id, 1 if subscribed else 0, ts, ts),
            )
            self._db.commit()

    def subscriber_ids(self) -> list[int]:
        """Chat ids currently opted in — the broadcast audience."""
        with self._lock:
            rows = self._db.execute("SELECT chat_id FROM users WHERE subscribed=1").fetchall()
        return [r["chat_id"] for r in rows]

    # --- reporting ---------------------------------------------------------- #

    def summary(self) -> dict[str, Any]:
        with self._lock:
            r = self._db.execute(
                "SELECT COUNT(*) AS users, "
                "COALESCE(SUM(subscribed),0) AS subscribed, "
                "COALESCE(SUM(messages),0) AS messages FROM users"
            ).fetchone()
            cost = float(self._meta_get("total_cost", "0") or "0")
        return {
            "users": r["users"],
            "subscribed": r["subscribed"],
            "opted_out": r["users"] - r["subscribed"],
            "messages": r["messages"],
            "total_cost": round(cost, 4),
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- internals ---------------------------------------------------------- #

    def _meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _meta_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
