"""SQLite-backed cache with full event lifecycle tracking.

Design goals:
  * Never lose data. Events that vanish upstream are *soft-deleted*
    (``deleted_at`` set), not removed, and can be resurrected.
  * Every content change is recorded in an append-only ``event_history`` log
    with a field-level diff.
  * Bookmark counts update silently (they churn every crawl) and do not spam
    the history.
  * Full-text search over name/description/location/creator via FTS5.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import config

# Columns persisted on the events table (normalized fields + lifecycle).
_EVENT_COLUMNS = (
    "event_id",
    "name",
    "description",
    "event_type",
    "start_datetime",
    "end_datetime",
    "start_date",
    "duration_minutes",
    "location",
    "event_site_location",
    "event_site_location_name",
    "plaintext_location",
    "creator_name",
    "created_by_account_id",
    "will_be_filmed",
    "av_needs",
    "bookmarks",
    "is_placeholder",
    "content_hash",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id                  TEXT PRIMARY KEY,
    name                      TEXT NOT NULL,
    description               TEXT NOT NULL DEFAULT '',
    event_type                TEXT,
    start_datetime            TEXT,
    end_datetime              TEXT,
    start_date                TEXT,
    duration_minutes          INTEGER,
    location                  TEXT,
    event_site_location       TEXT,
    event_site_location_name  TEXT,
    plaintext_location        TEXT,
    creator_name              TEXT,
    created_by_account_id     TEXT,
    will_be_filmed            INTEGER NOT NULL DEFAULT 0,
    av_needs                  TEXT,
    bookmarks                 INTEGER NOT NULL DEFAULT 0,
    is_placeholder            INTEGER NOT NULL DEFAULT 0,
    content_hash              TEXT NOT NULL,
    -- lifecycle
    first_seen                TEXT NOT NULL,
    last_seen                 TEXT NOT NULL,
    deleted_at                TEXT,
    is_deleted                INTEGER NOT NULL DEFAULT 0,
    revision                  INTEGER NOT NULL DEFAULT 0,
    raw_json                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_site ON events(event_site_location_name);
CREATE INDEX IF NOT EXISTS idx_events_deleted ON events(is_deleted);
CREATE INDEX IF NOT EXISTS idx_events_placeholder ON events(is_placeholder);

CREATE TABLE IF NOT EXISTS event_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    change_type  TEXT NOT NULL,   -- created | updated | deleted | resurrected
    changed_at   TEXT NOT NULL,
    changes_json TEXT,            -- list of {field, old, new}
    snapshot_json TEXT            -- full normalized event at this point
);
CREATE INDEX IF NOT EXISTS idx_history_event ON event_history(event_id);
CREATE INDEX IF NOT EXISTS idx_history_time ON event_history(changed_at);
CREATE INDEX IF NOT EXISTS idx_history_type ON event_history(change_type);

CREATE TABLE IF NOT EXISTS crawl_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,   -- ok | error
    http_status    INTEGER,
    event_count    INTEGER,
    num_created    INTEGER DEFAULT 0,
    num_updated    INTEGER DEFAULT 0,
    num_deleted    INTEGER DEFAULT 0,
    num_resurrected INTEGER DEFAULT 0,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl_started ON crawl_log(started_at);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    event_id UNINDEXED,
    name,
    description,
    location,
    creator_name,
    tokenize = 'porter unicode61'
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Fields whose diffs we record in history (content fields, plus we note
# bookmarks separately but don't diff them).
_DIFF_FIELDS = (
    "name",
    "description",
    "event_type",
    "start_datetime",
    "end_datetime",
    "location",
    "event_site_location",
    "event_site_location_name",
    "plaintext_location",
    "creator_name",
    "will_be_filmed",
    "av_needs",
)


class Store:
    """Thread-safe SQLite store. Safe to share one instance across threads."""

    def __init__(self, db_path: Optional[Path] = None):
        config.ensure_data_dir()
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self._local = threading.local()
        # Initialize schema once.
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ----------------------------------------------------------------- writes

    def _fts_upsert(self, conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
        conn.execute("DELETE FROM events_fts WHERE event_id = ?", (ev["event_id"],))
        conn.execute(
            "INSERT INTO events_fts (event_id, name, description, location, creator_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ev["event_id"],
                ev.get("name") or "",
                ev.get("description") or "",
                ev.get("location") or "",
                ev.get("creator_name") or "",
            ),
        )

    def _fts_delete(self, conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute("DELETE FROM events_fts WHERE event_id = ?", (event_id,))

    def _record_history(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        change_type: str,
        changed_at: str,
        changes: Optional[list[dict[str, Any]]],
        snapshot: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO event_history (event_id, change_type, changed_at, changes_json, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                change_type,
                changed_at,
                json.dumps(changes, ensure_ascii=False) if changes else None,
                json.dumps(snapshot, ensure_ascii=False, default=str),
            ),
        )

    def reconcile(
        self, normalized: list[dict[str, Any]], raw_by_id: dict[str, Any], now: Optional[str] = None
    ) -> dict[str, int]:
        """Apply one crawl's worth of events, computing the full diff.

        Returns counts of created/updated/deleted/resurrected/unchanged.
        """
        now = now or _utcnow()
        counts = {"created": 0, "updated": 0, "deleted": 0, "resurrected": 0, "unchanged": 0}

        with self.transaction() as conn:
            existing = {
                row["event_id"]: row
                for row in conn.execute("SELECT * FROM events").fetchall()
            }
            seen_ids: set[str] = set()

            for ev in normalized:
                eid = ev["event_id"]
                if not eid:
                    continue
                seen_ids.add(eid)
                raw = raw_by_id.get(eid)
                prior = existing.get(eid)

                if prior is None:
                    self._insert_event(conn, ev, raw, now)
                    self._record_history(conn, eid, "created", now, None, ev)
                    counts["created"] += 1
                    continue

                changes = self._diff(prior, ev)
                was_deleted = bool(prior["is_deleted"])
                content_changed = prior["content_hash"] != ev["content_hash"]

                if was_deleted:
                    # Event reappeared upstream.
                    revision = prior["revision"] + (1 if content_changed else 0)
                    self._update_event(
                        conn, ev, raw, prior["first_seen"], now,
                        deleted_at=None, is_deleted=0, revision=revision,
                    )
                    self._record_history(conn, eid, "resurrected", now, changes or None, ev)
                    counts["resurrected"] += 1
                elif content_changed:
                    revision = prior["revision"] + 1
                    self._update_event(
                        conn, ev, raw, prior["first_seen"], now,
                        deleted_at=None, is_deleted=0, revision=revision,
                    )
                    self._record_history(conn, eid, "updated", now, changes, ev)
                    counts["updated"] += 1
                else:
                    # No content change — refresh bookmarks + last_seen only.
                    conn.execute(
                        "UPDATE events SET bookmarks = ?, last_seen = ? WHERE event_id = ?",
                        (ev["bookmarks"], now, eid),
                    )
                    counts["unchanged"] += 1

            # Anything previously active but absent this crawl → soft delete.
            for eid, prior in existing.items():
                if eid in seen_ids or prior["is_deleted"]:
                    continue
                conn.execute(
                    "UPDATE events SET is_deleted = 1, deleted_at = ?, last_seen = ? WHERE event_id = ?",
                    (now, prior["last_seen"], eid),
                )
                self._fts_delete(conn, eid)
                snapshot = {k: prior[k] for k in prior.keys()}
                self._record_history(conn, eid, "deleted", now, None, snapshot)
                counts["deleted"] += 1

        return counts

    def _insert_event(self, conn, ev, raw, now) -> None:
        cols = list(_EVENT_COLUMNS) + ["first_seen", "last_seen", "deleted_at", "is_deleted", "revision", "raw_json"]
        vals = [self._coerce(ev.get(c)) for c in _EVENT_COLUMNS] + [
            now, now, None, 0, 0, json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
        ]
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO events ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        self._fts_upsert(conn, ev)

    def _update_event(self, conn, ev, raw, first_seen, now, *, deleted_at, is_deleted, revision) -> None:
        assignments = ", ".join(f"{c} = ?" for c in _EVENT_COLUMNS)
        vals = [self._coerce(ev.get(c)) for c in _EVENT_COLUMNS] + [
            first_seen, now, deleted_at, is_deleted, revision,
            json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
            ev["event_id"],
        ]
        conn.execute(
            f"UPDATE events SET {assignments}, first_seen = ?, last_seen = ?, "
            f"deleted_at = ?, is_deleted = ?, revision = ?, raw_json = ? WHERE event_id = ?",
            vals,
        )
        self._fts_upsert(conn, ev)

    @staticmethod
    def _coerce(value: Any) -> Any:
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    @staticmethod
    def _diff(prior: sqlite3.Row, ev: dict[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for field in _DIFF_FIELDS:
            old = prior[field] if field in prior.keys() else None
            new = ev.get(field)
            if isinstance(new, bool):
                new = 1 if new else 0
            if old != new:
                changes.append({"field": field, "old": old, "new": new})
        return changes

    # --------------------------------------------------------------- crawl log

    def start_crawl(self, now: Optional[str] = None) -> int:
        now = now or _utcnow()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO crawl_log (started_at, status) VALUES (?, 'running')",
                (now,),
            )
            return cur.lastrowid

    def finish_crawl(
        self,
        crawl_id: int,
        *,
        status: str,
        http_status: Optional[int] = None,
        event_count: Optional[int] = None,
        counts: Optional[dict[str, int]] = None,
        error: Optional[str] = None,
        now: Optional[str] = None,
    ) -> None:
        now = now or _utcnow()
        counts = counts or {}
        with self.transaction() as conn:
            conn.execute(
                "UPDATE crawl_log SET finished_at = ?, status = ?, http_status = ?, "
                "event_count = ?, num_created = ?, num_updated = ?, num_deleted = ?, "
                "num_resurrected = ?, error = ? WHERE id = ?",
                (
                    now, status, http_status, event_count,
                    counts.get("created", 0), counts.get("updated", 0),
                    counts.get("deleted", 0), counts.get("resurrected", 0),
                    error, crawl_id,
                ),
            )

    # ------------------------------------------------------------------ reads

    def crawl_status(self) -> dict[str, Any]:
        conn = self.connect()
        last = conn.execute(
            "SELECT * FROM crawl_log WHERE status != 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS c FROM crawl_log").fetchone()["c"]
        # consecutive failures from the tail
        consecutive = 0
        for row in conn.execute(
            "SELECT status FROM crawl_log WHERE status != 'running' ORDER BY id DESC LIMIT 50"
        ).fetchall():
            if row["status"] == "error":
                consecutive += 1
            else:
                break
        return {
            "last_crawl_at": last["finished_at"] if last else None,
            "last_status": last["status"] if last else None,
            "last_http_status": last["http_status"] if last else None,
            "last_event_count": last["event_count"] if last else None,
            "last_error": last["error"] if last else None,
            "total_crawls": total,
            "consecutive_failures": consecutive,
        }

    def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        row = self.connect().execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row else None

    def query_events(
        self,
        *,
        q: Optional[str] = None,
        event_type: Optional[str] = None,
        site: Optional[str] = None,
        creator: Optional[str] = None,
        start_after: Optional[str] = None,
        start_before: Optional[str] = None,
        day: Optional[str] = None,
        filmed: Optional[bool] = None,
        has_av_needs: Optional[bool] = None,
        min_bookmarks: Optional[int] = None,
        include_placeholder: bool = False,
        include_deleted: bool = False,
        sort: str = "start",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conn = self.connect()
        where: list[str] = []
        params: list[Any] = []

        if q:
            ids = [
                r["event_id"]
                for r in conn.execute(
                    "SELECT event_id FROM events_fts WHERE events_fts MATCH ?",
                    (_fts_query(q),),
                ).fetchall()
            ]
            if not ids:
                return [], 0
            where.append(f"event_id IN ({', '.join('?' for _ in ids)})")
            params.extend(ids)

        if not include_deleted:
            where.append("is_deleted = 0")
        if not include_placeholder:
            where.append("is_placeholder = 0")
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if site:
            where.append("event_site_location_name = ?")
            params.append(site)
        if creator:
            where.append("creator_name = ?")
            params.append(creator)
        if start_after:
            where.append("start_datetime >= ?")
            params.append(start_after)
        if start_before:
            where.append("start_datetime <= ?")
            params.append(start_before)
        if day:
            where.append("start_date = ?")
            params.append(day)
        if filmed is not None:
            where.append("will_be_filmed = ?")
            params.append(1 if filmed else 0)
        if has_av_needs is not None:
            where.append("av_needs IS NOT NULL" if has_av_needs else "av_needs IS NULL")
        if min_bookmarks is not None:
            where.append("bookmarks >= ?")
            params.append(min_bookmarks)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM events{clause}", params
        ).fetchone()["c"]

        order = {
            "start": "start_datetime ASC",
            "-start": "start_datetime DESC",
            "bookmarks": "bookmarks DESC",
            "name": "name ASC",
            "recent": "last_seen DESC",
        }.get(sort, "start_datetime ASC")

        rows = conn.execute(
            f"SELECT * FROM events{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [_row_to_event(r) for r in rows], total

    def list_days(self, include_placeholder: bool = False) -> list[dict[str, Any]]:
        conn = self.connect()
        clause = "WHERE is_deleted = 0 AND start_date IS NOT NULL"
        if not include_placeholder:
            clause += " AND is_placeholder = 0"
        rows = conn.execute(
            f"SELECT start_date AS date, COUNT(*) AS event_count, "
            f"MIN(start_datetime) AS first_start, MAX(start_datetime) AS last_start "
            f"FROM events {clause} GROUP BY start_date ORDER BY start_date"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sites(self) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            "SELECT event_site_location, event_site_location_name, COUNT(*) AS event_count "
            "FROM events WHERE is_deleted = 0 AND event_site_location IS NOT NULL "
            "GROUP BY event_site_location, event_site_location_name "
            "ORDER BY event_count DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def history(
        self,
        *,
        event_id: Optional[str] = None,
        change_type: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conn = self.connect()
        where: list[str] = []
        params: list[Any] = []
        if event_id:
            where.append("h.event_id = ?")
            params.append(event_id)
        if change_type:
            where.append("h.change_type = ?")
            params.append(change_type)
        if since:
            where.append("h.changed_at >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM event_history h{clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT h.*, e.name AS event_name FROM event_history h "
            f"LEFT JOIN events e ON e.event_id = h.event_id{clause} "
            f"ORDER BY h.id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        out = []
        for r in rows:
            changes = json.loads(r["changes_json"]) if r["changes_json"] else []
            out.append(
                {
                    "id": r["id"],
                    "event_id": r["event_id"],
                    "change_type": r["change_type"],
                    "changed_at": r["changed_at"],
                    "changes": changes,
                    "event_name": r["event_name"],
                }
            )
        return out, total

    def stats(self) -> dict[str, Any]:
        conn = self.connect()

        def scalar(sql: str, params: Iterable[Any] = ()) -> int:
            return conn.execute(sql, tuple(params)).fetchone()[0]

        total = scalar("SELECT COUNT(*) FROM events")
        active = scalar("SELECT COUNT(*) FROM events WHERE is_deleted = 0")
        deleted = scalar("SELECT COUNT(*) FROM events WHERE is_deleted = 1")
        placeholder = scalar("SELECT COUNT(*) FROM events WHERE is_placeholder = 1")
        now = _utcnow_naive()
        real_upcoming = scalar(
            "SELECT COUNT(*) FROM events WHERE is_deleted = 0 AND is_placeholder = 0 "
            "AND start_datetime >= ?",
            (now,),
        )
        by_type = {
            r["event_type"] or "(none)": r["c"]
            for r in conn.execute(
                "SELECT event_type, COUNT(*) AS c FROM events WHERE is_deleted = 0 "
                "GROUP BY event_type"
            ).fetchall()
        }
        by_site = {
            r["event_site_location_name"]: r["c"]
            for r in conn.execute(
                "SELECT event_site_location_name, COUNT(*) AS c FROM events "
                "WHERE is_deleted = 0 AND event_site_location_name IS NOT NULL "
                "GROUP BY event_site_location_name ORDER BY c DESC"
            ).fetchall()
        }
        filmed = scalar("SELECT COUNT(*) FROM events WHERE is_deleted = 0 AND will_be_filmed = 1")
        av = scalar("SELECT COUNT(*) FROM events WHERE is_deleted = 0 AND av_needs IS NOT NULL")
        bounds = conn.execute(
            "SELECT MIN(start_datetime) AS lo, MAX(start_datetime) AS hi FROM events "
            "WHERE is_deleted = 0 AND is_placeholder = 0"
        ).fetchone()

        return {
            "total_events": total,
            "active_events": active,
            "deleted_events": deleted,
            "placeholder_events": placeholder,
            "real_upcoming_events": real_upcoming,
            "by_type": by_type,
            "by_site": by_site,
            "filmed_events": filmed,
            "events_with_av_needs": av,
            "earliest_event": bounds["lo"],
            "latest_event": bounds["hi"],
            "crawl": self.crawl_status(),
        }


def _utcnow_naive() -> str:
    """Naive wall-clock 'now' for comparison against upstream local times."""
    return datetime.now().isoformat(timespec="seconds")


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["will_be_filmed"] = bool(d.get("will_be_filmed"))
    d["is_placeholder"] = bool(d.get("is_placeholder"))
    d["is_deleted"] = bool(d.get("is_deleted"))
    d.pop("raw_json", None)
    d.pop("content_hash", None)
    return d


def _fts_query(q: str) -> str:
    """Turn a user query into a safe FTS5 prefix-match expression."""
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    if not tokens:
        return '""'
    return " ".join(f"{t}*" for t in tokens)
