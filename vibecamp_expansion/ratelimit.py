"""A tiny, dependency-free sliding-window rate limiter.

Used by the Telegram bot to cap how many messages a single chat can send in a
rolling window, so one spammer can't run up the (paid, multi-second) Anthropic
LLM bill or starve other users. The limiter is deliberately self-contained and
clock-injected: the logic takes ``now`` as an argument and never reads a real
clock, which keeps it fully hermetic and deterministic to test.

State is in-memory only. That's fine for the bot's single-instance polling
deployment (one process owns the chat); it intentionally does not survive a
restart and is not shared across instances. If we ever run multiple bot
instances this would need a shared store (e.g. Redis) instead.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict


class SlidingWindowRateLimiter:
    """Allow at most ``max_events`` events per ``window_seconds`` per key.

    Each key (for the bot, a Telegram chat id) gets a ``deque`` of the
    timestamps of its recent allowed events. On each :meth:`allow` call we evict
    timestamps older than ``now - window_seconds``; if fewer than ``max_events``
    remain, the event is allowed and its timestamp recorded. Empty keys are
    pruned opportunistically so the map doesn't grow without bound as users come
    and go.
    """

    def __init__(self, max_events: int, window_seconds: float) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_events = max_events
        self.window_seconds = float(window_seconds)
        self._events: Dict[str, Deque[float]] = {}

    def allow(self, key: str, now: float) -> bool:
        """Record and allow this event, or return ``False`` if over the limit.

        ``now`` is a monotonic-ish timestamp in seconds (e.g. ``time.time()``);
        it is injected so the logic is deterministic and clock-free in tests.
        When allowed, the event's timestamp is recorded against the window; when
        blocked, nothing is recorded (a blocked attempt doesn't extend the
        window or count against future allowance).
        """
        cutoff = now - self.window_seconds
        bucket = self._events.get(key)
        if bucket is None:
            bucket = deque()
            self._events[key] = bucket

        # Evict timestamps that have slid out of the window.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self.max_events:
            # Over the limit. Prune the key if it somehow emptied (it can't here,
            # since we're at capacity) — kept symmetric with the allow path.
            return False

        bucket.append(now)
        return True

    # ``check`` is an alias some callers prefer to read; same behaviour.
    check = allow

    def would_allow(self, key: str, now: float) -> bool:
        """Whether an event *would* be allowed now, without recording it.

        Lets a caller test several limiters (e.g. a burst window and a daily
        window) and only record the event against all of them once they all
        agree — so a block on one tier doesn't consume allowance on another.
        Evicting stale timestamps here is harmless (it only prunes the window).
        """
        cutoff = now - self.window_seconds
        bucket = self._events.get(key)
        if not bucket:
            return True
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return len(bucket) < self.max_events

    def prune(self, now: float) -> None:
        """Drop keys with no events left inside the current window.

        Called opportunistically to bound memory when many distinct keys have
        gone quiet. Safe to call any time; it never affects allow decisions.
        """
        cutoff = now - self.window_seconds
        stale = [
            key
            for key, bucket in self._events.items()
            if not bucket or bucket[-1] <= cutoff
        ]
        for key in stale:
            del self._events[key]
