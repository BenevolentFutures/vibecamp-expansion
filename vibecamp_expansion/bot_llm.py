"""LLM-backed semantic selection for the chat bots' free-text / recommend paths.

The REST API's full-text search is literal (token AND-matching), so it cannot
answer fuzzy intents: "what's the next event?", "anything for someone into AI?"
The AI-relevant events are named things like "Let's Form a Hive Mind!" with no
literal keyword to match. This module hands the user's message plus the current
edition's event pool to Claude, which reads names/descriptions and selects the
genuinely relevant events (and orders "next"/"now" by time).

It degrades gracefully: with no ``ANTHROPIC_API_KEY`` set, or on any API error,
callers fall back to the keyword heuristic in ``bot_api.recommend``.

Uses the Anthropic SDK (``claude-opus-4-8``) with a structured-output schema.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from .bot_api import (
    VibecampAPI,
    apply_sort,
    event_day,
    event_stars,
    event_time,
    event_venue,
    future_filter,
    now_feed,
    now_local,
    truncate,
    upcoming_events,
)

logger = logging.getLogger(__name__)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

# Reasoning effort for the concierge. Adaptive thinking is always on (it lets the
# model reason about multi-constraint requests before choosing); effort tunes how
# hard it thinks. "medium" balances quality against chat latency — raise to
# "high"/"xhigh" via env for sharper picks at the cost of a few more seconds.
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "medium")

# Size of the candidate pool handed to the model, and of the returned list.
_CANDIDATE_LIMIT = 250
_RESULT_LIMIT = 20

# Truncate each candidate's description to keep the prompt compact.
_DESC_CHARS = 200

_SYSTEM = """\
You are the concierge for the Vibe Camp festival schedule. Given a guest's \
message and the list of scheduled events, classify the request and pick the \
events that best answer it.

Camp is a single long weekend (Thursday–Sunday), so days are shown as weekday \
names and times are Eastern. CURRENT_TIME tells you the day and time it is now. \
Only future and currently-happening events are in the list — never apologise \
that something is over.

Set `mode`:
- "now" — they ask what's happening RIGHT NOW / on at the moment / right this \
minute. (The system fills these with events starting around now — just began \
or about to.)
- "upcoming" — they ask what's next / soon / later today / "what's on Friday". \
(The system fills these by time.)
- "popular" — they ask what's most popular / best / top / most-starred. \
(The system fills these by star count.)
- "select" — anything else: an interest ("into AI", "live music", "something \
spiritual"), a venue ("at the pool", "in the barn"), a host ("hosted by Atin", \
"Sarah's events" — match the `host` field), or a specific thing ("shanties", \
"tarot"). For this mode you choose the events.

For mode "select", put up to 20 matching event_ids in `event_ids`, best first. \
Match on meaning, not just words — an AI fan should get "Let's Form a Hive \
Mind!" or "Claude Squad" even though they don't contain "AI". Be strict: only \
include events with a clear, direct connection to the request, and omit \
loosely-related or merely-popular filler. If nothing genuinely fits, return an \
empty list. For "upcoming" and "popular", leave `event_ids` empty.

Set `sort` (used to order "select" picks): "soonest" by default, so the guest \
can catch what's next — use it for plain interests and venues. Use "popular" \
only if they explicitly ask for the best / most-loved. Use "relevance" only if \
they want the closest match regardless of timing.

When the guest names a place (a venue), include ONLY events at that exact \
venue — even if they also say "popular" or "best". Popularity then just orders \
that venue's events; it never pulls in a popular event somewhere else.

Write `framing` as one short, plain sentence introducing the picks (no emoji, \
no hype)."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretation": {
            "type": "string",
            "description": "One short sentence: what the guest is asking for.",
        },
        "mode": {
            "type": "string",
            "enum": ["now", "upcoming", "popular", "select"],
            "description": "now/upcoming/popular are filled deterministically; select uses event_ids.",
        },
        "sort": {
            "type": "string",
            "enum": ["soonest", "popular", "relevance"],
            "description": "Order for 'select' picks: soonest (default), popular, or relevance.",
        },
        "venue": {
            "type": "string",
            "description": "If the guest named a place, the EXACT venue string from "
            "the data to restrict to (e.g. 'Pool'); otherwise empty.",
        },
        "framing": {
            "type": "string",
            "description": "One plain sentence introducing the picks, shown to the guest.",
        },
        "event_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "For mode 'select': chosen event_ids, best first, max 20.",
        },
    },
    "required": ["interpretation", "mode", "sort", "framing", "event_ids"],
    "additionalProperties": False,
}


def llm_available() -> bool:
    """Return True if an Anthropic API key is configured."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _compact(event: dict[str, Any]) -> dict[str, Any]:
    """Reduce an event to the fields the model needs to choose well."""
    return {
        "event_id": event.get("event_id"),
        "name": event.get("name") or "",
        "host": event.get("creator_name") or "",
        "day": event_day(event),
        "time": event_time(event),
        "venue": event_venue(event),
        "stars": event_stars(event),
        "desc": truncate(event.get("description") or "", _DESC_CHARS),
    }


async def smart_select(
    api: VibecampAPI,
    query: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Select events for a free-text query via the LLM.

    Returns ``{"events": [...], "framing": str, "interpretation": str}`` with
    full event dicts (in the model's chosen order), or ``None`` if the LLM is
    unavailable or errors — signalling the caller to use the keyword fallback.
    """
    if not llm_available():
        return None

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; falling back to keyword search")
        return None

    now = now or now_local()
    # Only ever consider events that haven't already finished — stale events are
    # never surfaced (and never offered to the model to pick).
    candidates = future_filter(await api.search_events(sort="start", limit=_CANDIDATE_LIMIT), now)
    if not candidates:
        return {"events": [], "framing": "", "interpretation": ""}
    by_id = {e["event_id"]: e for e in candidates if e.get("event_id")}
    compact = [_compact(e) for e in candidates]

    # Stable prefix (instructions + event pool) is cached; the volatile query
    # goes last so repeated calls within the cache window reuse the prefix.
    events_block = (
        f"CURRENT_TIME: {now.strftime('%A %H:%M')} (Eastern)\n\n"
        f"EVENTS (JSON):\n{json.dumps(compact, ensure_ascii=False)}"
    )

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=8000,  # headroom for adaptive thinking + the small JSON answer
            system=[
                {"type": "text", "text": _SYSTEM},
                {"type": "text", "text": events_block, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": f"Guest message: {query}"}],
            # Adaptive thinking lets the model reason about multi-constraint asks
            # before committing to picks; effort tunes depth vs. chat latency.
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}, "effort": EFFORT},
        )
    except Exception:  # noqa: BLE001 — any API failure degrades to the fallback
        logger.exception("smart_select LLM call failed; using keyword fallback")
        return None
    finally:
        await client.close()

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("smart_select returned non-JSON; using keyword fallback")
        return None

    # Temporal and popularity intents are answered deterministically — the model
    # is unreliable at time-sorting and we already have authoritative orderings.
    # All three modes draw from the already-future-filtered candidate pool, so
    # nothing stale (and, for "upcoming", currently-running events too) is shown.
    mode = parsed.get("mode", "select")
    sort = parsed.get("sort", "soonest")
    # Observability: confirm the brain ran and how it read the request. Logs the
    # classification only — never the events or any secret.
    logger.info(
        "smart_select mode=%s sort=%s interpretation=%s",
        mode, sort, parsed.get("interpretation", ""),
    )
    framing = parsed.get("framing", "")
    if mode == "now":
        # The headline feature: events starting around right now. Falls back to
        # the next upcoming events when nothing's live, with honest framing the
        # model couldn't know to write (it can't see that the window is empty).
        feed = now_feed(candidates, now, limit=_RESULT_LIMIT)
        ordered = feed["events"]
        if not feed["live"]:
            framing = "Nothing's kicking off in the next half hour — here's what's coming up next."
        elif not framing:
            framing = "Happening right now and starting soon:"
    elif mode == "upcoming":
        # Order by upcoming start, not end-based future — so a thing that opened
        # yesterday and runs all weekend doesn't masquerade as the "next" event.
        ordered = upcoming_events(candidates, now)[:_RESULT_LIMIT]
    elif mode == "popular":
        ordered = apply_sort(candidates, "popular")[:_RESULT_LIMIT]
    else:
        picks = [by_id[i] for i in parsed.get("event_ids", []) if i in by_id]
        # When the guest named a place, enforce it deterministically — the model
        # is reliable at *choosing* but occasionally lets a popular off-venue
        # event slip in. Filtering by the venue it reported guarantees precision.
        venue = parsed.get("venue", "").strip()
        if venue:
            picks = [e for e in picks if venue.lower() in (event_venue(e) or "").lower()]
        ordered = apply_sort(picks, sort)[:_RESULT_LIMIT]

    return {
        "events": ordered,
        "framing": framing,
        "interpretation": parsed.get("interpretation", ""),
    }


async def curate(api: VibecampAPI, query: str) -> dict[str, Any]:
    """Resolve a free-text query to events + a framing line.

    Tries the LLM concierge first; on any unavailability or error, falls back
    to the keyword-union heuristic. Always returns
    ``{"events": [...], "framing": str}``.
    """
    from .bot_api import recommend  # local import avoids a cycle at import time

    result = await smart_select(api, query)
    if result is not None:
        return {"events": result["events"], "framing": result["framing"]}
    # Keyword fallback: still drop anything already over before showing it.
    events = future_filter(await recommend(api, query))
    return {"events": events, "framing": ""}
