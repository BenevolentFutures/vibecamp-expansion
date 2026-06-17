"""Hermetic tests for the sliding-window rate limiter.

All time is injected — no real clock, no network, no LLM — so these are fully
deterministic. They cover the per-chat behaviour the Telegram bot relies on:
allow under the limit, block the overflow, recover as the window slides,
independence between keys, and the window boundary.
"""

from __future__ import annotations

import pytest

from vibecamp_expansion.ratelimit import SlidingWindowRateLimiter


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


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_events=0, window_seconds=600)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_events=10, window_seconds=0)
