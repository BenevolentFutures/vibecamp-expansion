"""Hermetic tests for the usage analytics counters."""

from __future__ import annotations

import json

from vibecamp_expansion.analytics import Analytics


def test_counts_messages_and_unique_users() -> None:
    a = Analytics(started_at=0.0)
    assert a.record("u1", "text") is True   # new user
    assert a.record("u1", "now") is False   # same user again
    assert a.record("u2", "text") is True   # new user
    s = a.summary(now=3600.0)
    assert s["unique_users"] == 2
    assert s["total_messages"] == 3
    assert s["by_kind"] == {"text": 2, "now": 1}
    assert s["uptime_seconds"] == 3600


def test_rate_limited_counts_as_a_user_but_not_a_message() -> None:
    a = Analytics(started_at=0.0)
    a.record("u1", "text")
    a.record_rate_limited("u2")
    s = a.summary(now=0.0)
    assert s["total_messages"] == 1          # the served one only
    assert s["rate_limited"] == 1
    assert s["unique_users"] == 2            # both u1 and u2 count as users


def test_by_kind_orders_by_frequency() -> None:
    a = Analytics(started_at=0.0)
    for _ in range(3):
        a.record("u", "text")
    a.record("u", "now")
    # most_common ordering -> text first
    assert list(a.summary()["by_kind"].items())[0] == ("text", 3)


def test_roundtrip_persistence(tmp_path) -> None:
    a = Analytics(started_at=10.0)
    a.record("u1", "text")
    a.record("u2", "pool")
    a.record_rate_limited("u3")
    path = str(tmp_path / "analytics.json")
    a.save(path)

    # The file holds only hashed/opaque keys + counts.
    raw = json.loads((tmp_path / "analytics.json").read_text())
    assert raw["total"] == 2
    assert raw["rate_limited"] == 1
    assert sorted(raw["users"]) == ["u1", "u2", "u3"]

    b = Analytics.load(path)
    assert b.unique_users == 3
    assert b.total == 2
    assert b.rate_limited == 1
    assert b.by_kind["pool"] == 1
    assert b.started_at == 10.0


def test_tracks_cost_and_per_user() -> None:
    a = Analytics(started_at=0.0)
    a.record("u1", "text")
    a.record_cost("u1", 0.10)
    a.record("u1", "text")
    a.record_cost("u1", 0.02)
    a.record("u2", "now")  # command, no LLM cost
    s = a.summary(now=0.0)
    assert s["total_cost"] == 0.12
    assert s["avg_queries_per_user"] == 1.5  # 3 messages / 2 users
    top = s["top_users"]
    assert top[0] == {"user": "u1", "queries": 2, "cost": 0.12}
    assert top[1] == {"user": "u2", "queries": 1, "cost": 0.0}


def test_record_cost_ignores_zero() -> None:
    a = Analytics(started_at=0.0)
    a.record("u1", "text")
    a.record_cost("u1", 0.0)
    assert a.summary()["total_cost"] == 0.0
    assert a.cost_by_user == {}


def test_cost_and_per_user_survive_roundtrip(tmp_path) -> None:
    a = Analytics(started_at=0.0)
    a.record("u1", "text")
    a.record_cost("u1", 0.25)
    path = str(tmp_path / "a.json")
    a.save(path)
    b = Analytics.load(path)
    assert b.total_cost == 0.25
    assert b.by_user["u1"] == 1
    assert b.cost_by_user["u1"] == 0.25


def test_load_missing_file_returns_fresh() -> None:
    a = Analytics.load("/nonexistent/path/analytics.json")
    assert a.total == 0
    assert a.unique_users == 0


def test_save_to_bad_path_does_not_raise() -> None:
    a = Analytics(started_at=0.0)
    a.record("u1", "text")
    # Should log a warning and swallow the error, not crash the bot.
    a.save("/nonexistent/dir/analytics.json")
