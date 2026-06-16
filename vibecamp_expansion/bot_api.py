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

from typing import Any, Optional

import httpx

# Hosted REST API; overridable via VIBECAMP_API_BASE in each bot's entry point.
DEFAULT_API_BASE = "https://vibecamp-expansion-production.up.railway.app"

# How many results list-style commands show.
LIST_LIMIT = 8

STAR = "⭐"  # output reads "5 ⭐" everywhere


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


# --------------------------------------------------------------------------- #
# Field accessors (work on the raw event dicts from the API)                  #
# --------------------------------------------------------------------------- #


def truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters with an ellipsis if needed."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


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
    """Return the calendar day (YYYY-MM-DD) for an event, or ``"?"``."""
    return event.get("start_date") or "?"


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
