"""Hermetic tests for the sliding-window rate limiter.

All time is injected — no real clock, no network, no LLM — so these are fully
deterministic. They cover the per-chat behaviour the Telegram bot relies on:
allow under the limit, block the overflow, recover as the window slides,
independence between keys, and the window boundary.
"""

from __future__ import annotations

import pytest

from vibecamp_expansion.ratelimit import (
    ExponentialLockout,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)


def test_allows_up_to_the_limit() -> None:
    rl = SlidingWindowRateLimiter(max_events=10, window_seconds=600)
    # 10 events at the same instant are all allowed.
    assert all(rl.allow("chat", now=1000.0) for _ in range(10))


def test_blocks_the_overflow_event() -> None:
    rl = SlidingWindowRateLimiter(max_events=10, window_seconds=600)
    for _ in range(10):
        assert rl.allow("chat", now=1000.0) is True
    # The 11th within the window is blocked.
    assert rl.allow("chat", now=1000.5) is False
    # And stays blocked while still inside the window.
    assert rl.allow("chat", now=1300.0) is False


def test_recovers_after_window_slides() -> None:
    rl = SlidingWindowRateLimiter(max_events=3, window_seconds=600)
    for _ in range(3):
        assert rl.allow("chat", now=1000.0) is True
    assert rl.allow("chat", now=1001.0) is False
    # Advance past the window: the original 3 timestamps fall out, so we're
    # allowed again.
    assert rl.allow("chat", now=1000.0 + 600.001) is True


def test_partial_window_slide_frees_one_slot() -> None:
    rl = SlidingWindowRateLimiter(max_events=2, window_seconds=100)
    assert rl.allow("chat", now=0.0) is True   # t=0
    assert rl.allow("chat", now=50.0) is True  # t=50
    assert rl.allow("chat", now=60.0) is False  # full: [0, 50]
    # At t=101 the t=0 event has expired (<= 101-100=1), leaving just [50].
    assert rl.allow("chat", now=101.0) is True


def test_keys_are_independent() -> None:
    rl = SlidingWindowRateLimiter(max_events=2, window_seconds=600)
    assert rl.allow("a", now=1000.0) is True
    assert rl.allow("a", now=1000.0) is True
    assert rl.allow("a", now=1000.0) is False  # 'a' is full
    # 'b' has its own budget.
    assert rl.allow("b", now=1000.0) is True
    assert rl.allow("b", now=1000.0) is True
    assert rl.allow("b", now=1000.0) is False


def test_window_edge_is_consistent() -> None:
    # An event exactly window_seconds later: the boundary uses <= cutoff for
    # eviction, so a timestamp exactly at (now - window) is considered expired.
    rl = SlidingWindowRateLimiter(max_events=1, window_seconds=100)
    assert rl.allow("chat", now=0.0) is True
    # At now=100, cutoff = 0, and the t=0 event (0 <= 0) is evicted -> allowed.
    assert rl.allow("chat", now=100.0) is True
    # Just before the edge it's still counted -> blocked.
    rl2 = SlidingWindowRateLimiter(max_events=1, window_seconds=100)
    assert rl2.allow("chat", now=0.0) is True
    assert rl2.allow("chat", now=99.999) is False


def test_blocked_attempt_does_not_extend_window() -> None:
    rl = SlidingWindowRateLimiter(max_events=1, window_seconds=100)
    assert rl.allow("chat", now=0.0) is True
    # Repeated blocked attempts must not push the window forward.
    assert rl.allow("chat", now=50.0) is False
    assert rl.allow("chat", now=90.0) is False
    # The original t=0 still expires at t=100 regardless of blocked attempts.
    assert rl.allow("chat", now=100.001) is True


def test_check_is_an_alias_for_allow() -> None:
    rl = SlidingWindowRateLimiter(max_events=1, window_seconds=100)
    assert rl.check("chat", now=0.0) is True
    assert rl.check("chat", now=1.0) is False


def test_prune_drops_only_stale_keys() -> None:
    rl = SlidingWindowRateLimiter(max_events=5, window_seconds=100)
    rl.allow("old", now=0.0)
    rl.allow("fresh", now=90.0)
    # At t=150, 'old' (last event t=0 <= 50) is stale; 'fresh' (t=90 > 50) lives.
    rl.prune(now=150.0)
    # 'old' was pruned, so it starts fresh with full budget.
    assert rl.allow("old", now=150.0) is True
    # 'fresh' still has its t=90 event counted within the window.
    assert rl.allow("fresh", now=150.0) is True
    assert rl.allow("fresh", now=150.0) is True


def test_would_allow_does_not_record() -> None:
    rl = SlidingWindowRateLimiter(max_events=1, window_seconds=100)
    # Peeking many times never consumes the single slot.
    assert all(rl.would_allow("chat", now=0.0) for _ in range(5))
    # The real event is still available because nothing was recorded.
    assert rl.allow("chat", now=0.0) is True
    # Now the slot is taken — peek reflects it, still without recording.
    assert rl.would_allow("chat", now=1.0) is False
    assert rl.would_allow("chat", now=1.0) is False


def test_two_tier_block_does_not_consume_other_tier() -> None:
    # Models the bot's gate: a short burst window + a daily window. When the
    # daily tier is exhausted, peeking the burst tier must not burn its budget.
    burst = SlidingWindowRateLimiter(max_events=10, window_seconds=600)
    daily = SlidingWindowRateLimiter(max_events=3, window_seconds=86_400)

    def gate(now: float) -> bool:
        """Return True if allowed, recording against both tiers only if so."""
        if not burst.would_allow("chat", now):
            return False
        if not daily.would_allow("chat", now):
            return False
        burst.allow("chat", now)
        daily.allow("chat", now)
        return True

    # 3 allowed (daily cap), close together so all 3 sit in the burst window.
    assert gate(now=0.0) is True
    assert gate(now=1.0) is True
    assert gate(now=2.0) is True
    # 4th is blocked by the daily cap, while the burst tier still has room.
    assert gate(now=3.0) is False
    # The blocked peek did NOT record against burst: it still holds exactly the
    # 3 allowed events (7 of its 10 slots free), not 4.
    assert len(burst._events["chat"]) == 3
    assert burst.max_events - len(burst._events["chat"]) == 7


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_events=0, window_seconds=600)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_events=10, window_seconds=0)


# --------------------------------------------------------------------------- #
# TokenBucketRateLimiter                                                       #
# --------------------------------------------------------------------------- #


def test_token_bucket_allows_full_burst_then_blocks() -> None:
    rl = TokenBucketRateLimiter(capacity=3, refill_per_minute=2)
    # A fresh key starts full: 3 messages right away.
    assert all(rl.allow("chat", now=0.0) for _ in range(3))
    # 4th in the same instant is blocked — the bucket is dry.
    assert rl.allow("chat", now=0.0) is False


def test_token_bucket_burst_three_then_two_per_minute() -> None:
    # The bot's exact config: a burst of 3, then a steady 2/min trickle.
    rl = TokenBucketRateLimiter(capacity=3, refill_per_minute=2)
    assert all(rl.allow("c", now=0.0) for _ in range(3))   # burst of 3
    assert rl.allow("c", now=0.0) is False

    # Refill is one token per 30s (2/min). Nothing yet at 29s.
    assert rl.would_allow("c", now=29.0) is False
    # One token back at 30s, spent immediately, then dry again.
    assert rl.allow("c", now=30.0) is True
    assert rl.allow("c", now=30.0) is False
    # Second sustained token at 60s -> exactly 2 allowed across the minute.
    assert rl.allow("c", now=60.0) is True


def test_token_bucket_refill_caps_at_capacity() -> None:
    rl = TokenBucketRateLimiter(capacity=3, refill_per_minute=2)
    # Drain it.
    for _ in range(3):
        assert rl.allow("c", now=0.0) is True
    # Idle for a long time: refill is clamped to capacity, not unbounded.
    assert all(rl.allow("c", now=10_000.0) for _ in range(3))
    assert rl.allow("c", now=10_000.0) is False


def test_token_bucket_would_allow_does_not_spend() -> None:
    rl = TokenBucketRateLimiter(capacity=1, refill_per_minute=2)
    # Peeking never consumes the single token.
    assert all(rl.would_allow("c", now=0.0) for _ in range(5))
    assert rl.allow("c", now=0.0) is True
    # Now dry — peek reflects it without recording.
    assert rl.would_allow("c", now=0.0) is False


def test_token_bucket_keys_are_independent() -> None:
    rl = TokenBucketRateLimiter(capacity=2, refill_per_minute=2)
    assert rl.allow("a", now=0.0) is True
    assert rl.allow("a", now=0.0) is True
    assert rl.allow("a", now=0.0) is False  # 'a' drained
    # 'b' has its own full bucket.
    assert rl.allow("b", now=0.0) is True
    assert rl.allow("b", now=0.0) is True
    assert rl.allow("b", now=0.0) is False


def test_token_bucket_prune_drops_full_buckets() -> None:
    rl = TokenBucketRateLimiter(capacity=2, refill_per_minute=2)
    rl.allow("spent", now=0.0)    # 1 token left -> not full
    rl.allow("drained", now=0.0)
    rl.allow("drained", now=0.0)  # 0 tokens -> not full
    # At t=120 both have refilled to capacity (full) -> pruned.
    rl.prune(now=120.0)
    assert rl._state == {}
    # Pruned keys behave identically to fresh ones (full bucket).
    assert all(rl.allow("drained", now=120.0) for _ in range(2))
    assert rl.allow("drained", now=120.0) is False


def test_token_bucket_check_is_an_alias_for_allow() -> None:
    rl = TokenBucketRateLimiter(capacity=1, refill_per_minute=2)
    assert rl.check("c", now=0.0) is True
    assert rl.check("c", now=0.0) is False


def test_token_bucket_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=0, refill_per_minute=2)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=3, refill_per_minute=0)


# --------------------------------------------------------------------------- #
# ExponentialLockout                                                           #
# --------------------------------------------------------------------------- #


def test_lockout_first_offense_uses_base_duration() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=3600)
    armed, seconds = lk.register("c", now=0.0)
    assert armed is True
    assert seconds == 60.0
    assert lk.is_locked("c", now=59.0) is True
    assert lk.remaining("c", now=10.0) == 50.0
    # At exactly base_seconds the lockout has expired.
    assert lk.is_locked("c", now=60.0) is False
    assert lk.remaining("c", now=60.0) == 0.0


def test_lockout_doubles_on_consecutive_offenses() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=3600)
    # 1st offense: 60s (locked until 60).
    assert lk.register("c", now=0.0) == (True, 60.0)
    # Fresh offense right after it expires -> doubles to 120 (locked until 180).
    assert lk.register("c", now=60.0) == (True, 120.0)
    # And again -> 240 (locked until 420).
    assert lk.register("c", now=180.0) == (True, 240.0)


def test_lockout_register_while_locked_does_not_escalate() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=3600)
    assert lk.register("c", now=0.0) == (True, 60.0)
    # A concurrent overflow that slips past the gate while still locked:
    # no re-arm, no escalation, just reports time remaining.
    assert lk.register("c", now=10.0) == (False, 50.0)
    # The next *fresh* offense is only level 1 (120s), proving the mid-lock
    # register above did not bump the escalation level.
    assert lk.register("c", now=60.0) == (True, 120.0)


def test_lockout_caps_at_max() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=300, reset_after_seconds=3600)
    assert lk.register("c", now=0.0)[1] == 60.0
    assert lk.register("c", now=60.0)[1] == 120.0
    assert lk.register("c", now=180.0)[1] == 240.0
    # 480 would be next, but it's capped at max_seconds=300.
    assert lk.register("c", now=420.0)[1] == 300.0
    assert lk.register("c", now=720.0)[1] == 300.0


def test_lockout_resets_after_clean_period() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=3600)
    assert lk.register("c", now=0.0) == (True, 60.0)   # locked until 60
    # Behaves for reset_after past the lockout's end -> escalation resets.
    armed, seconds = lk.register("c", now=60.0 + 3600.0)
    assert armed is True
    assert seconds == 60.0  # back to base, not doubled


def test_lockout_keys_are_independent() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=3600)
    assert lk.register("a", now=0.0) == (True, 60.0)
    assert lk.register("a", now=60.0) == (True, 120.0)  # 'a' escalated
    # 'b' is on its own clock — first offense is still base.
    assert lk.register("b", now=60.0) == (True, 60.0)


def test_lockout_prune_drops_reset_keys() -> None:
    lk = ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=3600)
    lk.register("old", now=0.0)        # locked until 60
    lk.register("fresh", now=4000.0)   # locked until 4060
    # At t=4000, 'old' is 3940s past its lockout end (>= 3600) -> pruned;
    # 'fresh' is still active.
    lk.prune(now=4000.0)
    # A pruned key starts fresh at base on its next offense.
    assert lk.register("old", now=4000.0) == (True, 60.0)


def test_lockout_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        ExponentialLockout(base_seconds=0, max_seconds=900, reset_after_seconds=3600)
    with pytest.raises(ValueError):
        ExponentialLockout(base_seconds=120, max_seconds=60, reset_after_seconds=3600)
    with pytest.raises(ValueError):
        ExponentialLockout(base_seconds=60, max_seconds=900, reset_after_seconds=-1)
