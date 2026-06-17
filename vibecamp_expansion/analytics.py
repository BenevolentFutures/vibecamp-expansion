"""Lightweight usage analytics for the chat bots — how many people use this.

Tracks unique users, total messages, a breakdown by command, and how many
requests got rate-limited. It is deliberately small and clock-injectable so it
stays hermetic to test.

Privacy: callers pass an already-hashed, opaque user key (never a raw chat id),
so nothing personally identifiable is held in memory or written to disk — only
counts and the cardinality of the hashed set. This matches the project's
read-only / no-PII posture (see CLAUDE.md).

State is in-memory by default (fine for the single-instance bot), with optional
JSON persistence so counts survive a restart when a durable path is configured
(e.g. a mounted volume). Without that, counts reset on redeploy — the periodic
log line still leaves a durable record of growth in the deploy logs.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Analytics:
    """Aggregate usage counters keyed on opaque (hashed) user keys."""

    def __init__(self, *, started_at: Optional[float] = None) -> None:
        self.started_at = started_at if started_at is not None else time.time()
        self.total = 0
        self.rate_limited = 0
        self.total_cost = 0.0
        self._users: set[str] = set()
        self.by_kind: Counter = Counter()
        self.by_user: Counter = Counter()       # served messages per user
        self.cost_by_user: dict[str, float] = {}  # estimated $ per user

    def record(self, user_key: str, kind: str) -> bool:
        """Record one served message; return True if this user is newly seen.

        ``kind`` is a coarse label — a command name (``"now"``) or ``"text"`` for
        a free-text concierge query.
        """
        self.total += 1
        self.by_kind[kind] += 1
        self.by_user[user_key] += 1
        is_new = user_key not in self._users
        self._users.add(user_key)
        return is_new

    def record_cost(self, user_key: str, amount: float) -> None:
        """Attribute the estimated $ cost of an LLM call to the running totals."""
        if not amount:
            return
        self.total_cost += amount
        self.cost_by_user[user_key] = self.cost_by_user.get(user_key, 0.0) + amount

    def record_rate_limited(self, user_key: str) -> None:
        """Record a request that was rejected by the rate limiter.

        They still count as a user who tried to use the bot.
        """
        self.rate_limited += 1
        self._users.add(user_key)

    @property
    def unique_users(self) -> int:
        return len(self._users)

    def top_users(self, n: int = 5) -> list[dict[str, Any]]:
        """The busiest users by served message count, with their est. spend."""
        return [
            {"user": key, "queries": count, "cost": round(self.cost_by_user.get(key, 0.0), 4)}
            for key, count in self.by_user.most_common(n)
        ]

    def summary(self, now: Optional[float] = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        users = self.unique_users
        return {
            "unique_users": users,
            "total_messages": self.total,
            "rate_limited": self.rate_limited,
            "total_cost": round(self.total_cost, 4),
            "avg_queries_per_user": round(self.total / users, 1) if users else 0.0,
            "by_kind": dict(self.by_kind.most_common()),
            "top_users": self.top_users(5),
            "uptime_seconds": int(max(0.0, now - self.started_at)),
        }

    # --- optional JSON persistence ----------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "total": self.total,
            "rate_limited": self.rate_limited,
            "total_cost": self.total_cost,
            "users": sorted(self._users),  # hashed keys only
            "by_kind": dict(self.by_kind),
            "by_user": dict(self.by_user),
            "cost_by_user": self.cost_by_user,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Analytics":
        a = cls(started_at=data.get("started_at"))
        a.total = int(data.get("total", 0))
        a.rate_limited = int(data.get("rate_limited", 0))
        a.total_cost = float(data.get("total_cost", 0.0))
        a._users = set(data.get("users", []))
        a.by_kind = Counter(data.get("by_kind", {}))
        a.by_user = Counter(data.get("by_user", {}))
        a.cost_by_user = {k: float(v) for k, v in data.get("cost_by_user", {}).items()}
        return a

    def save(self, path: str) -> None:
        """Atomically persist to ``path`` (best-effort; never raises)."""
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f)
            os.replace(tmp, path)
        except OSError:
            logger.warning("Failed to persist analytics to %s", path, exc_info=True)

    @classmethod
    def load(cls, path: str) -> "Analytics":
        """Load from ``path``, or return a fresh instance if absent/invalid."""
        try:
            with open(path, encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            return cls()
