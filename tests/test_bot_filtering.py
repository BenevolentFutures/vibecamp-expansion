"""Hermetic tests for the bots' time filtering, sorting, and day handling.

No network and no LLM — these cover the deterministic helpers that surround the
concierge: the "never show stale events" rule, sort ordering, weekday rendering
and parsing, the Eastern-time "now", and the help-text education. The
LLM-dependent routing (now/soon/sort inference) is exercised by ``eval_bot.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from vibecamp_expansion.bot_api import (
    VibecampAPI,
    apply_sort,
    event_day,
    future_filter,
    happening_now,
    is_future,
    now_feed,
    now_local,
)
from vibecamp_expansion.telegram_bot import _HELP

# Fixed reference: Friday mid-camp, 14:00 Eastern.
NOW = datetime(2026, 6, 19, 14, 0)


def ev(start: str, *, dur: int = 60, stars: int = 0, eid: str = "x", name: str = "E") -> dict:
    """Build a minimal event dict with a wall-clock start (trailing Z is a lie)."""
    return {
        "event_id": eid,
        "name": name,
        "start_datetime": f"{start}:00.000Z",
        "start_date": start[:10],
        "duration_minutes": dur,
        "stars": stars,
        "location": "Pool",
    }


# --------------------------------------------------------------------------- #
# Future / stale filter                                                       #
# --------------------------------------------------------------------------- #


def test_filter_drops_event_finished_beyond_grace():
    # A1: started 12:00, 30 min long -> ended 12:30, >1h before 14:00.
    assert not is_future(ev("2026-06-19T12:00", dur=30), NOW)


def test_filter_keeps_event_finished_within_grace():
    # A2: ended 13:40, only 20 min ago -> still shown.
    assert is_future(ev("2026-06-19T13:10", dur=30), NOW)


def test_filter_keeps_ongoing_event():
    # A3: started 12:00, runs 180 min -> ends 15:00, happening right now.
    assert is_future(ev("2026-06-19T12:00", dur=180), NOW)


def test_filter_keeps_upcoming_event():
    # A4: starts 14:30.
    assert is_future(ev("2026-06-19T14:30"), NOW)


def test_filter_no_duration_uses_start():
    # A5: no duration, started 12:30 (>1h ago) -> hidden.
    assert not is_future(ev("2026-06-19T12:30", dur=0), NOW)
    # A6: no duration, started 13:50 (<1h ago) -> shown.
    assert is_future(ev("2026-06-19T13:50", dur=0), NOW)


def test_filter_keeps_event_with_unparseable_time():
    # We can't prove it's stale, so we never silently drop it.
    assert is_future({"event_id": "z", "start_datetime": None}, NOW)


def test_future_filter_list():
    events = [
        ev("2026-06-19T09:00", dur=30, eid="past"),      # over
        ev("2026-06-19T12:00", dur=180, eid="ongoing"),  # now
        ev("2026-06-19T16:00", eid="later"),             # upcoming
    ]
    kept = {e["event_id"] for e in future_filter(events, NOW)}
    assert kept == {"ongoing", "later"}


# --------------------------------------------------------------------------- #
# "Happening now" window                                                      #
# --------------------------------------------------------------------------- #


def test_happening_now_window():
    # Window is [NOW-15min, NOW+30min] = [13:45, 14:30] by start time.
    events = [
        ev("2026-06-19T13:30", eid="too_early"),   # started 30 min ago -> out
        ev("2026-06-19T13:50", eid="just_started"),  # 10 min ago -> in
        ev("2026-06-19T14:00", eid="right_now"),     # exactly now -> in
        ev("2026-06-19T14:25", eid="about_to"),      # in 25 min -> in
        ev("2026-06-19T15:00", eid="later"),         # in 60 min -> out
    ]
    ids = [e["event_id"] for e in happening_now(events, NOW)]
    assert ids == ["just_started", "right_now", "about_to"]  # soonest-first, windowed


def test_now_feed_live_when_window_has_events():
    events = [ev("2026-06-19T14:10", eid="soon"), ev("2026-06-19T18:00", eid="tonight")]
    feed = now_feed(events, NOW)
    assert feed["live"] is True
    assert [e["event_id"] for e in feed["events"]] == ["soon"]


def test_now_feed_falls_back_to_upcoming_when_nothing_live():
    # Nothing in the now-window; should fall back to the next upcoming events.
    events = [
        ev("2026-06-19T09:00", dur=30, eid="over"),   # finished long ago
        ev("2026-06-19T17:00", eid="tonight"),         # next up
        ev("2026-06-19T20:00", eid="later"),
    ]
    feed = now_feed(events, NOW)
    assert feed["live"] is False
    assert [e["event_id"] for e in feed["events"]] == ["tonight", "later"]


# --------------------------------------------------------------------------- #
# Sorting                                                                     #
# --------------------------------------------------------------------------- #


def test_apply_sort_soonest():
    a = ev("2026-06-19T16:00", eid="a")
    b = ev("2026-06-19T14:30", eid="b")
    c = ev("2026-06-19T15:00", eid="c")
    assert [e["event_id"] for e in apply_sort([a, b, c], "soonest")] == ["b", "c", "a"]


def test_apply_sort_popular():
    a = ev("2026-06-19T16:00", stars=2, eid="a")
    b = ev("2026-06-19T14:30", stars=9, eid="b")
    c = ev("2026-06-19T15:00", stars=5, eid="c")
    assert [e["event_id"] for e in apply_sort([a, b, c], "popular")] == ["b", "c", "a"]


def test_apply_sort_relevance_preserves_order():
    a, b, c = ev("2026-06-19T16:00", eid="a"), ev("2026-06-19T14:30", eid="b"), ev("2026-06-19T15:00", eid="c")
    assert [e["event_id"] for e in apply_sort([a, b, c], "relevance")] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Weekday rendering + Eastern now                                             #
# --------------------------------------------------------------------------- #


def test_event_day_is_weekday_name():
    assert event_day({"start_date": "2026-06-19"}) == "Friday"
    assert event_day({"start_date": "2026-06-18"}) == "Thursday"
    assert event_day({"start_date": "2026-06-21"}) == "Sunday"
    assert event_day({"start_date": None}) == "?"


def test_now_local_is_naive_and_eastern():
    n = now_local()
    assert n.tzinfo is None  # naive wall-clock, comparable to event times
    # Eastern is always behind UTC, so local "now" must trail the UTC clock —
    # this is the regression guard against the bot using a UTC "now" during camp.
    utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert n < utc_naive


# --------------------------------------------------------------------------- #
# Day resolution (weekday names -> dates)                                     #
# --------------------------------------------------------------------------- #


class _FakeAPI(VibecampAPI):
    """VibecampAPI with the network calls stubbed for resolve_day."""

    def __init__(self, dates: list[str]) -> None:  # noqa: D107 - no HTTP client
        self._dates = dates

    async def days(self) -> list[dict]:
        return [{"date": d} for d in self._dates]

    async def first_festival_day(self):
        return self._dates[0] if self._dates else None


CAMP_DATES = ["2026-06-18", "2026-06-19", "2026-06-20", "2026-06-21"]


def test_resolve_day_weekday_name():
    api = _FakeAPI(CAMP_DATES)
    assert asyncio.run(api.resolve_day("Friday")) == "2026-06-19"
    assert asyncio.run(api.resolve_day("thursday")) == "2026-06-18"


def test_resolve_day_abbreviation():
    api = _FakeAPI(CAMP_DATES)
    assert asyncio.run(api.resolve_day("sat")) == "2026-06-20"
    assert asyncio.run(api.resolve_day("sun")) == "2026-06-21"


def test_resolve_day_explicit_date_passthrough():
    api = _FakeAPI(CAMP_DATES)
    assert asyncio.run(api.resolve_day("2026-06-20")) == "2026-06-20"


def test_resolve_day_default_is_first_day():
    api = _FakeAPI(CAMP_DATES)
    assert asyncio.run(api.resolve_day(None)) == "2026-06-18"


def test_resolve_day_unknown_returns_none():
    api = _FakeAPI(CAMP_DATES)
    assert asyncio.run(api.resolve_day("someday")) is None


# --------------------------------------------------------------------------- #
# User education                                                              #
# --------------------------------------------------------------------------- #


def test_help_highlights_the_two_best_queries():
    assert "coming up soon" in _HELP
    assert "into ___" in _HELP  # the interest prompt
    assert "Thursday–Sunday" in _HELP  # tells people how to name days
