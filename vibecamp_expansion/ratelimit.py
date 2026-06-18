"""Tiny, dependency-free, clock-injected rate-limiting primitives.

Used by the Telegram bot to cap how many messages a single chat can send, so
one spammer can't run up the (paid, multi-second) Anthropic LLM bill or starve
other users. Three primitives live here, all clock-injected (the logic takes
``now`` as an argument and never reads a real clock, which keeps it fully
hermetic and deterministic to test):

- :class:`SlidingWindowRateLimiter` — at most N events per rolling window.
  Used for the per-chat daily cap.
- :class:`TokenBucketRateLimiter` — a small burst allowance that refills at a
  steady sustained rate. Used for the per-chat burst tier ("a few right away,
  then a steady trickle").
- :class:`ExponentialLockout` — an escalating penalty box: each fresh overflow
  doubles how long the key is locked out, capped, and resets after the key
  behaves for a while.

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


class TokenBucketRateLimiter:
    """Allow a small burst, then a steady sustained rate, per key.

    Each key starts with a full bucket of ``capacity`` tokens and refills at
    ``refill_per_minute`` tokens per minute (clamped to ``capacity``). Each
    allowed event spends one token; an event with no whole token available is
    blocked. This models "a few questions right away, then a steady trickle":
    a fresh user can fire off ``capacity`` messages immediately, after which
    they're paced to the sustained refill rate.

    Like :class:`SlidingWindowRateLimiter`, this is clock-injected — every
    method takes ``now`` (seconds, e.g. ``time.time()``) so it's deterministic
    and clock-free in tests. State is per key: ``(tokens, last_refill_ts)``.
    """

    def __init__(self, capacity: int, refill_per_minute: float) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if refill_per_minute <= 0:
            raise ValueError("refill_per_minute must be > 0")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_minute) / 60.0
        # key -> (tokens, last_refill_ts)
        self._state: Dict[str, tuple[float, float]] = {}

    def _tokens_at(self, key: str, now: float) -> float:
        """Current token balance for ``key`` after refilling up to ``now``.

        Pure read: does not mutate state. An unknown key is treated as a full
        bucket, so a brand-new user gets the full burst allowance.
        """
        tokens, last = self._state.get(key, (self.capacity, now))
        if now > last:
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        return tokens

    def would_allow(self, key: str, now: float) -> bool:
        """Whether an event *would* be allowed now, without spending a token.

        Mirrors :meth:`SlidingWindowRateLimiter.would_allow` so the bot's gate
        can peek several limiters and only record once they all agree.
        """
        return self._tokens_at(key, now) >= 1.0

    def allow(self, key: str, now: float) -> bool:
        """Spend a token and allow this event, or return ``False`` if dry.

        Refills first, then consumes one token if at least one whole token is
        available. A blocked call still advances the refill clock (so refill
        accrues correctly) but spends nothing.
        """
        tokens = self._tokens_at(key, now)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return True
        self._state[key] = (tokens, now)
        return False

    # ``check`` is an alias some callers prefer to read; same behaviour.
    check = allow

    def prune(self, now: float) -> None:
        """Drop keys that have refilled to a full bucket.

        A full bucket is indistinguishable from a never-seen key (both start at
        ``capacity``), so dropping them is safe and bounds memory as users go
        quiet. Never affects allow decisions.
        """
        full = [key for key in self._state if self._tokens_at(key, now) >= self.capacity]
        for key in full:
            del self._state[key]


class ExponentialLockout:
    """An escalating per-key penalty box.

    The first time a key overflows it's locked out for ``base_seconds``. Each
    *fresh* overflow that happens before the key has behaved long enough
    doubles the lockout (``base``, ``2·base``, ``4·base`` …) up to
    ``max_seconds``. Once a key goes ``reset_after_seconds`` past the end of
    its last lockout without re-offending, its escalation resets to the base.

    Clock-injected like the limiters above. State is per key:
    ``(level, locked_until_ts)``.
    """

    def __init__(
        self,
        base_seconds: float,
        max_seconds: float,
        reset_after_seconds: float,
    ) -> None:
        if base_seconds <= 0:
            raise ValueError("base_seconds must be > 0")
        if max_seconds < base_seconds:
            raise ValueError("max_seconds must be >= base_seconds")
        if reset_after_seconds < 0:
            raise ValueError("reset_after_seconds must be >= 0")
        self.base = float(base_seconds)
        self.max = float(max_seconds)
        self.reset_after = float(reset_after_seconds)
        # key -> (level, locked_until_ts)
        self._state: Dict[str, tuple[int, float]] = {}

    def is_locked(self, key: str, now: float) -> bool:
        """Whether ``key`` is currently inside an active lockout."""
        state = self._state.get(key)
        return state is not None and now < state[1]

    def remaining(self, key: str, now: float) -> float:
        """Seconds left on ``key``'s lockout, or ``0.0`` if not locked."""
        state = self._state.get(key)
        if state is None:
            return 0.0
        return max(0.0, state[1] - now)

    def register(self, key: str, now: float) -> tuple[bool, float]:
        """Record an overflow for ``key``.

        Returns ``(newly_armed, seconds)``:

        - If ``key`` is already inside an active lockout (e.g. a concurrent
          overflow slipped past the gate's :meth:`is_locked` check), the
          existing lockout is left untouched — no escalation, no re-arm — and
          ``(False, seconds_remaining)`` is returned. Callers use the ``False``
          to avoid sending a duplicate notice.
        - Otherwise a new lockout is armed at the escalated duration and
          ``(True, duration)`` is returned.
        """
        state = self._state.get(key)
        if state is not None and now < state[1]:
            return False, state[1] - now

        if state is None:
            level = 0
        else:
            prev_level, prev_until = state
            # Behaved long enough since the last lockout ended -> start over.
            level = 0 if (now - prev_until >= self.reset_after) else prev_level + 1

        duration = min(self.base * (2 ** level), self.max)
        self._state[key] = (level, now + duration)
        return True, duration

    def prune(self, now: float) -> None:
        """Drop keys whose escalation has reset (lockout long expired).

        Safe to call any time; a pruned key simply starts fresh at the base
        duration on its next overflow.
        """
        stale = [
            key
            for key, (_level, until) in self._state.items()
            if now - until >= self.reset_after
        ]
        for key in stale:
            del self._state[key]
