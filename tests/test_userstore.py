"""Hermetic tests for the durable user/subscriber store (in-memory SQLite)."""

from __future__ import annotations

from vibecamp_expansion.userstore import UserStore


def test_seen_enrolls_and_counts() -> None:
    s = UserStore()
    assert s.seen(101, "alice") is True   # new
    assert s.seen(101, "alice") is False  # repeat
    assert s.seen(202, "bob") is True
    summ = s.summary()
    assert summ["users"] == 2
    assert summ["subscribed"] == 2      # opt-out model: new users auto-enrolled
    assert summ["messages"] == 3
    assert set(s.subscriber_ids()) == {101, 202}


def test_opt_out_and_back_in() -> None:
    s = UserStore()
    s.seen(101, "alice")
    s.set_subscribed(101, False)
    assert s.subscriber_ids() == []
    assert s.summary()["opted_out"] == 1
    # A later message must NOT silently re-subscribe an opted-out user.
    s.seen(101, "alice")
    assert s.subscriber_ids() == []
    # Explicit /start re-subscribes.
    s.set_subscribed(101, True)
    assert s.subscriber_ids() == [101]


def test_start_before_any_message_creates_row() -> None:
    s = UserStore()
    s.set_subscribed(303, True)   # /start with no prior message
    assert 303 in s.subscriber_ids()


def test_cost_accumulates() -> None:
    s = UserStore()
    s.seen(1, "a")
    s.add_cost(0.10)
    s.add_cost(0.05)
    s.add_cost(0.0)  # ignored
    assert s.summary()["total_cost"] == 0.15


def test_username_backfilled_not_clobbered() -> None:
    s = UserStore()
    s.seen(1)            # no username yet
    s.seen(1, "later")   # username learned on a later message
    with s._lock:
        row = s._db.execute("SELECT username FROM users WHERE chat_id=1").fetchone()
    assert row["username"] == "later"


def test_persists_across_reopen(tmp_path) -> None:
    path = str(tmp_path / "users.db")
    s = UserStore(path)
    s.seen(101, "alice")
    s.add_cost(0.25)
    s.set_subscribed(202, True)
    s.close()
    # Reopen the same file — counts and subscribers survive (the volume case).
    s2 = UserStore(path)
    summ = s2.summary()
    assert summ["users"] == 2
    assert summ["total_cost"] == 0.25
    assert set(s2.subscriber_ids()) == {101, 202}
