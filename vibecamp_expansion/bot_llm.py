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
    events_on_day,
    future_filter,
    now_feed,
    now_local,
    truncate,
    upcoming_events,
    weekday_index,
)

logger = logging.getLogger(__name__)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Friendly aliases the admin can switch between at runtime (see set_model).
MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}

# Approx pricing in $/1M tokens by model id: (input, output, cache_write, cache_read).
_PRICING = {
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
}


def set_model(name: str) -> str:
    """Switch the concierge model at runtime. Accepts an alias (haiku/sonnet/
    opus) or a full model id; returns the resolved model id. Raises ValueError
    on an unrecognised name."""
    global MODEL
    resolved = MODEL_ALIASES.get(name.strip().lower(), name.strip())
    if resolved not in _PRICING:
        raise ValueError(f"unknown model: {name!r}")
    MODEL = resolved
    return resolved


def usage_cost(model: str, usage: Any) -> float:
    """Estimate the $ cost of one call from its token usage (0 if unknown)."""
    rates = _PRICING.get(model)
    if rates is None or usage is None:
        return 0.0
    pin, pout, pcw, pcr = rates
    return (
        (getattr(usage, "input_tokens", 0) or 0) * pin
        + (getattr(usage, "output_tokens", 0) or 0) * pout
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * pcw
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * pcr
    ) / 1_000_000

# Reasoning effort for the concierge. Adaptive thinking is always on (it lets the
# model reason about multi-constraint requests before choosing); effort tunes how
# hard it thinks. "medium" balances quality against chat latency — raise to
# "high"/"xhigh" via env for sharper picks at the cost of a few more seconds.
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "medium")

# Size of the candidate pool handed to the model, and of the returned list.
_CANDIDATE_LIMIT = 250
_RESULT_LIMIT = 10

# Truncate each candidate's description to keep the prompt compact. Descriptions
# dominate the cached event-pool token count, so this is the main cost lever —
# 80 chars is enough for the model to disambiguate intent without paying for full
# blurbs on every event. (~halves the pool's tokens vs. 200.)
_DESC_CHARS = 80

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
- "upcoming" — they ask what's next / soon / later today, with NO specific \
weekday named. (The system fills these by time.)
- "popular" — they ask what's most popular / best / top / most-starred. \
(The system fills these by star count.)
- "select" — anything else: an interest ("into AI", "live music", "something \
spiritual"), a venue ("at the pool", "in the barn"), a host ("hosted by Atin", \
"Sarah's events" — match the `host` field), a specific day or time ("what's on \
Friday", "Saturday morning", "Thursday night"), or a specific thing ("shanties", \
"tarot"). For this mode you choose the events.

When the guest names a weekday (Thursday–Sunday), set `day` to that weekday \
name (and `time_of_day` if they say morning/afternoon/evening/night) — the \
system then lists that day's events for you, so you do NOT need to choose them \
(leave `event_ids` empty). This applies even when phrased as "what's happening \
Friday" or "what's on Saturday"; do NOT use "now"/"upcoming" for a named \
weekday. Leave `day` empty when no weekday is named.

CRITICAL for "select" (when you DO choose events): whenever matching events \
exist, `event_ids` MUST contain their actual ids (up to 10). A `framing` \
sentence is NOT a substitute for ids — never return an empty `event_ids` while \
describing events in `framing`.

For mode "select", put up to 10 matching event_ids in `event_ids`, best first. \
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
        "day": {
            "type": "string",
            "description": "If the guest named a weekday (e.g. 'Friday'), that weekday "
            "name; otherwise empty. When set, the system lists that day deterministically.",
        },
        "time_of_day": {
            "type": "string",
            "enum": ["", "morning", "afternoon", "evening", "night"],
            "description": "If the guest named a time of day, the bucket; otherwise empty.",
        },
        "framing": {
            "type": "string",
            "description": "One plain sentence introducing the picks, shown to the guest.",
        },
        "event_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "For mode 'select': chosen event_ids, best first, max 10.",
        },
    },
    "required": [
        "interpretation", "mode", "sort", "venue", "day", "time_of_day",
        "framing", "event_ids",
    ],
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
        return {"events": [], "framing": "", "interpretation": "", "cost": 0.0}
    by_id = {e["event_id"]: e for e in candidates if e.get("event_id")}
    compact = [_compact(e) for e in candidates]

    # Cached prefix = instructions + event pool. CURRENT_TIME is volatile (it
    # changes every minute) so it must NOT live in the cached block — it goes in
    # the user turn below, after the cache breakpoint. Otherwise the ~34k-token
    # pool would be rewritten to cache every minute (~$0.24/call) instead of
    # reused across the 5-min TTL (~$0.02/call).
    events_block = f"EVENTS (JSON):\n{json.dumps(compact, ensure_ascii=False)}"

    client = AsyncAnthropic()
    current_model = MODEL  # capture in case an admin switches mid-flight
    try:
        resp = await client.messages.create(
            model=current_model,
            max_tokens=8000,  # headroom for adaptive thinking + the small JSON answer
            system=[
                {"type": "text", "text": _SYSTEM},
                {"type": "text", "text": events_block, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{
                "role": "user",
                "content": f"CURRENT_TIME: {now.strftime('%A %H:%M')} (Eastern)\nGuest message: {query}",
            }],
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
    day = parsed.get("day", "").strip()
    if day and weekday_index(day) is not None:
        # Day-specific question: the model detected the weekday (and maybe a time
        # of day); list that day deterministically rather than trusting the model
        # to enumerate a busy day's events (which it does unreliably).
        ordered = events_on_day(candidates, day, parsed.get("time_of_day", ""))[:_RESULT_LIMIT]
    elif mode == "now":
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
        "cost": usage_cost(current_model, resp.usage),
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
        return {
            "events": result["events"],
            "framing": result["framing"],
            "cost": result.get("cost", 0.0),
        }
    # Keyword fallback: still drop anything already over before showing it.
    events = future_filter(await recommend(api, query))
    return {"events": events, "framing": "", "cost": 0.0}
