"""Shared API client and field helpers for the chat bots.

Both the Discord bot (``discord_bot.py``) and the Telegram bot
(``telegram_bot.py``) are thin, read-only clients over the project's REST API
(see ``api.py``). The HTTP client, event field accessors, and the
recommendation logic live here so the two front-ends stay in sync; only the
platform-specific rendering differs.

Terminology: upstream stores the per-event save count as ``bookmarks``; the
my.vibe.camp UI and attendees call these **stars**. Bots show "stars".
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

# Hosted REST API; overridable via VIBECAMP_API_BASE in each bot's entry point.
DEFAULT_API_BASE = "https://vibecamp-expansion-production.up.railway.app"

# How many results list-style commands show. Telegram's real ceiling is the
# 4096-char message body (~20+ compact events); the renderer trims to fit and
# notes any overflow, so this can be generous without breaking a message.
LIST_LIMIT = 10

STAR = "⭐"  # output reads "5 ⭐" everywhere

# Camp-local timezone. Every event timestamp is naive wall-clock in this zone
# (see CLAUDE.md: upstream's trailing ``Z`` is a lie, not real UTC). The bots
# run on a UTC host, so "now" must be converted to this zone or "what's soon"
# would be hours off during camp. Vibe Camp runs on Eastern.
LOCAL_TZ = os.environ.get("VIBECAMP_LOCAL_TZ", "America/New_York")

# Keep showing an event until this long after it ends, so "happening right now"
# lingers and a just-finished thing doesn't vanish mid-conversation.
STALE_GRACE = timedelta(hours=1)

# "What's happening now" — the headline feature. An event counts as "now" if it
# started within the last NOW_LOOKBACK or starts within the next NOW_LOOKAHEAD,
# i.e. its start time falls in [now - 15min, now + 30min]. This is the
# just-walk-up-to-it window: things kicking off around you right this minute.
NOW_LOOKBACK = timedelta(minutes=15)
NOW_LOOKAHEAD = timedelta(minutes=30)

# Camp is a single long weekend; people refer to days by weekday name.
_WEEKDAY_ALIASES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


class VibecampAPI:
    """Async wrapper around the Vibe Camp Expansion REST API."""

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": "vibecamp-bot"},
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def search_events(self, **params: Any) -> list[dict[str, Any]]:
        """Return events from ``GET /events`` for the given query params.

        ``None`` values are dropped so callers can pass optional filters
        uniformly.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.get("/events", params=clean)
        resp.raise_for_status()
        return resp.json().get("events", [])

    async def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        """Return a single event by id, or ``None`` if not found."""
        resp = await self._client.get(f"/events/{event_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def days(self) -> list[dict[str, Any]]:
        """Return the day index from ``GET /days``."""
        resp = await self._client.get("/days")
        resp.raise_for_status()
        return resp.json()

    async def first_festival_day(self) -> Optional[str]:
        """Return the earliest day in the current edition, if any."""
        days = await self.days()
        return days[0]["date"] if days else None

    async def resolve_day(self, ref: Optional[str]) -> Optional[str]:
        """Resolve a day reference to a ``YYYY-MM-DD`` calendar day.

        Accepts a weekday name or abbreviation (``"Friday"``, ``"fri"``), an
        explicit ``YYYY-MM-DD`` (passed through), or ``None`` (the first camp
        day). Weekday names map to the matching day of the current edition.
        Returns ``None`` if the reference can't be matched.
        """
        if not ref or not ref.strip():
            return await self.first_festival_day()
        ref = ref.strip().lower()
        if len(ref) == 10 and ref[4] == "-" and ref[7] == "-":
            return ref  # explicit date
        weekday = _WEEKDAY_ALIASES.get(ref)
        if weekday is None:
            return None
        for day in await self.days():
            try:
                if date.fromisoformat(day["date"]).weekday() == weekday:
                    return day["date"]
            except (ValueError, KeyError):
                continue
        return None


# --------------------------------------------------------------------------- #
# Field accessors (work on the raw event dicts from the API)                  #
# --------------------------------------------------------------------------- #


def truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters with an ellipsis if needed."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def now_local() -> datetime:
    """Current wall-clock time in the camp's timezone, as a naive ``datetime``.

    Event timestamps are naive local wall-clock, so "now" must be too. The bots
    run on a UTC host; ``datetime.now()`` there would be hours ahead of camp,
    silently breaking every "what's happening now / soon" answer.
    """
    return datetime.now(ZoneInfo(LOCAL_TZ)).replace(tzinfo=None)


def event_start(event: dict[str, Any]) -> Optional[datetime]:
    """Parse an event's naive wall-clock start, or ``None`` if unparseable."""
    raw = event.get("start_datetime")
    if not raw or "T" not in raw:
        return None
    try:
        return datetime.fromisoformat(raw[:19])  # drop millis + the fake ``Z``
    except ValueError:
        return None


def event_end(event: dict[str, Any]) -> Optional[datetime]:
    """Parse an event's end (start + duration), or ``None`` if no valid start."""
    start = event_start(event)
    if start is None:
        return None
    return start + timedelta(minutes=int(event.get("duration_minutes") or 0))


def is_future(event: dict[str, Any], now: datetime, *, grace: timedelta = STALE_GRACE) -> bool:
    """True unless the event ended more than ``grace`` ago.

    Keeps still-running and recently-finished events (so "happening now" works)
    and drops anything long over. Events with an unparseable time are kept — we
    can't prove they're stale, and dropping them would silently lose data.
    """
    end = event_end(event)
    if end is None:
        return True
    return end > now - grace


def future_filter(
    events: list[dict[str, Any]],
    now: Optional[datetime] = None,
    *,
    grace: timedelta = STALE_GRACE,
) -> list[dict[str, Any]]:
    """Drop events that ended more than ``grace`` ago (default: 1h)."""
    now = now or now_local()
    return [e for e in events if is_future(e, now, grace=grace)]


def apply_sort(events: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Order events by ``"soonest"`` (time) or ``"popular"`` (stars).

    Any other value (e.g. ``"relevance"``) preserves the given order.
    """
    if sort == "soonest":
        return sorted(events, key=lambda e: event_start(e) or datetime.max)
    if sort == "popular":
        return sorted(events, key=event_stars, reverse=True)
    return events


def happening_now(
    events: list[dict[str, Any]],
    now: Optional[datetime] = None,
    *,
    lookback: timedelta = NOW_LOOKBACK,
    lookahead: timedelta = NOW_LOOKAHEAD,
) -> list[dict[str, Any]]:
    """Events whose start falls in ``[now - lookback, now + lookahead]``.

    The "what's happening now" window: things that just started or are about to.
    Ordered soonest-first. Events with an unparseable start are excluded (we
    can't place them on the clock).
    """
    now = now or now_local()
    lo, hi = now - lookback, now + lookahead
    live = [e for e in events if (s := event_start(e)) is not None and lo <= s <= hi]
    return sorted(live, key=lambda e: event_start(e) or datetime.max)


def upcoming_events(
    events: list[dict[str, Any]],
    now: Optional[datetime] = None,
    *,
    grace: timedelta = NOW_LOOKBACK,
) -> list[dict[str, Any]]:
    """Events starting from ``now - grace`` onward, soonest first.

    This is the right basis for "what's next / coming up": it orders by *start*
    and drops things that began well before now. Contrast ``future_filter``,
    which keeps anything not yet *ended* — including a multi-day installation
    that opened yesterday and would otherwise dominate a "soonest" sort by its
    stale start time. Events with an unparseable start are excluded.
    """
    now = now or now_local()
    cutoff = now - grace
    soon = [e for e in events if (s := event_start(e)) is not None and s >= cutoff]
    return sorted(soon, key=lambda e: event_start(e) or datetime.max)


def now_feed(
    events: list[dict[str, Any]],
    now: Optional[datetime] = None,
    *,
    limit: int = LIST_LIMIT,
) -> dict[str, Any]:
    """Resolve the "what's happening now" feed, with a graceful fallback.

    Returns ``{"events": [...], "live": bool}``. When something is in the now
    window, ``live`` is True. When nothing is (e.g. a lull, or before camp
    starts), it falls back to the next upcoming events so the answer is still
    useful, with ``live`` False.
    """
    now = now or now_local()
    live = happening_now(events, now)
    if live:
        return {"events": live[:limit], "live": True}
    return {"events": upcoming_events(events, now)[:limit], "live": False}


# Coarse time-of-day buckets by local hour on the named day. "night" is that
# day's late evening (21:00+), not its post-midnight wee hours — "Friday night"
# means Friday evening, not Friday 00:30.
_TIME_OF_DAY = {
    "morning": (5, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 24),
}


def weekday_index(name: str) -> Optional[int]:
    """Map a weekday name/abbreviation to 0=Mon..6=Sun, or None if unrecognised."""
    return _WEEKDAY_ALIASES.get(name.strip().lower())


def _in_time_of_day(hour: int, bucket: str) -> bool:
    rng = _TIME_OF_DAY.get(bucket)
    return rng is None or rng[0] <= hour < rng[1]


def events_on_day(
    events: list[dict[str, Any]],
    day_name: str,
    time_of_day: str = "",
) -> list[dict[str, Any]]:
    """Events on a given weekday (optionally a time-of-day bucket), soonest first.

    The model *detects* the day/time from the guest's words; this lists it
    deterministically — reliable where asking the model to enumerate a busy
    day's 60 events is not. Returns [] if the weekday name isn't recognised.
    """
    wi = weekday_index(day_name)
    if wi is None:
        return []
    tod = time_of_day.strip().lower()
    matched = [
        e
        for e in events
        if (s := event_start(e)) is not None
        and s.weekday() == wi
        and (not tod or _in_time_of_day(s.hour, tod))
    ]
    return sorted(matched, key=lambda e: event_start(e) or datetime.max)


def event_time(event: dict[str, Any]) -> str:
    """Return ``HH:MM`` from an event's wall-clock start, or ``"??:??"``.

    Timestamps are naive wall-clock (the trailing ``Z`` is an upstream lie),
    so we read the clock time directly without any timezone conversion.
    """
    raw = event.get("start_datetime")
    if not raw or "T" not in raw:
        return "??:??"
    return raw.split("T", 1)[1][:5]


def event_day(event: dict[str, Any]) -> str:
    """Return the weekday name (e.g. ``"Friday"``) for an event's day.

    Camp is a single long weekend, so attendees think in weekdays, not dates.
    Falls back to the raw date string, then ``"?"``.
    """
    raw = event.get("start_date")
    if not raw:
        return "?"
    try:
        return date.fromisoformat(raw).strftime("%A")
    except ValueError:
        return raw


def event_venue(event: dict[str, Any]) -> str:
    """Return the best human-readable venue for an event."""
    return event.get("location") or "TBA"


def event_stars(event: dict[str, Any]) -> int:
    """Return an event's star count (== bookmarks)."""
    return int(event.get("stars") or 0)


# --------------------------------------------------------------------------- #
# Recommendation logic                                                        #
# --------------------------------------------------------------------------- #


def interest_words(interest: str) -> list[str]:
    """Split a free-text interest into distinct lowercased search words.

    Short filler words are dropped so a phrase like "live music and art"
    becomes ``["live", "music", "art"]``. De-duplicates while preserving order
    and caps the count so a single call triggers only a few API requests.
    """
    stop = {"and", "the", "for", "with", "a", "an", "of", "to", "in", "on", "or"}
    seen: set[str] = set()
    words: list[str] = []
    for token in interest.lower().replace(",", " ").split():
        token = token.strip()
        if len(token) < 3 or token in stop or token in seen:
            continue
        seen.add(token)
        words.append(token)
    return words[:4] or [interest.strip().lower()]


async def recommend(api: VibecampAPI, interest: str) -> list[dict[str, Any]]:
    """Union events matching each interest word, de-dup, sort by stars desc."""
    by_id: dict[str, dict[str, Any]] = {}
    for word in interest_words(interest):
        for event in await api.search_events(q=word, sort="stars", limit=LIST_LIMIT):
            eid = event.get("event_id")
            if eid and eid not in by_id:
                by_id[eid] = event
    ranked = sorted(by_id.values(), key=event_stars, reverse=True)
    return ranked[:LIST_LIMIT]
