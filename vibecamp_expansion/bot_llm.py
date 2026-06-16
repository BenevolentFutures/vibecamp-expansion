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

from .bot_api import VibecampAPI, event_day, event_time, event_venue, event_stars, truncate

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

# Size of the candidate pool handed to the model, and of the returned list.
_CANDIDATE_LIMIT = 250
_RESULT_LIMIT = 8

# Truncate each candidate's description to keep the prompt compact.
_DESC_CHARS = 200

_SYSTEM = """\
You are the concierge for the Vibe Camp festival schedule. Given a guest's \
message and the full list of scheduled events, pick the events that best answer \
them, best first.

Interpret the intent:
- "next" / "what's happening now/soon" -> the events starting soonest at or \
after CURRENT_TIME, in chronological order. Never return events that already \
ended long ago when they ask what's next.
- "popular" / "what's good" -> the highest-starred events.
- a venue (e.g. "at the pool", "barn") -> events at that venue.
- an interest ("into AI", "live music", "something spiritual") -> events whose \
name or description genuinely match that interest, even when the words differ \
(e.g. an AI fan would want "Let's Form a Hive Mind!" or "Claude Squad"). Prefer \
relevance over star count, but use stars to break ties.
- a specific thing ("shanties", "tarot") -> events about that.

Return up to 8 event_ids that actually exist in the provided list, ordered best \
first. If nothing genuinely fits, return an empty list rather than padding with \
unrelated popular events. Write `framing` as one short, plain sentence \
introducing the picks (no emoji, no hype)."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretation": {
            "type": "string",
            "description": "One short sentence: what the guest is asking for.",
        },
        "framing": {
            "type": "string",
            "description": "One plain sentence introducing the picks, shown to the guest.",
        },
        "event_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Selected event_ids, best first, max 8. Empty if nothing fits.",
        },
    },
    "required": ["interpretation", "framing", "event_ids"],
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

    candidates = await api.search_events(sort="start", limit=_CANDIDATE_LIMIT)
    if not candidates:
        return {"events": [], "framing": "", "interpretation": ""}
    by_id = {e["event_id"]: e for e in candidates if e.get("event_id")}
    compact = [_compact(e) for e in candidates]
    now = now or datetime.now()

    # Stable prefix (instructions + event pool) is cached; the volatile query
    # goes last so repeated calls within the cache window reuse the prefix.
    events_block = (
        f"CURRENT_TIME: {now.isoformat(timespec='minutes')}\n\n"
        f"EVENTS (JSON):\n{json.dumps(compact, ensure_ascii=False)}"
    )

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=[
                {"type": "text", "text": _SYSTEM},
                {"type": "text", "text": events_block, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": f"Guest message: {query}"}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except Exception:  # noqa: BLE001 — any API failure degrades to the fallback
        logger.exception("smart_select LLM call failed; using keyword fallback")
        return None
    finally:
        await client.aclose()

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("smart_select returned non-JSON; using keyword fallback")
        return None

    ordered = [by_id[i] for i in parsed.get("event_ids", []) if i in by_id][:_RESULT_LIMIT]
    return {
        "events": ordered,
        "framing": parsed.get("framing", ""),
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
    events = await recommend(api, query)
    return {"events": events, "framing": ""}
