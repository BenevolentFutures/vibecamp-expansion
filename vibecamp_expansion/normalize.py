"""Map raw upstream event payloads into our normalized shape.

Upstream quirk: the backend formats stored *local* times as ISO strings with a
``Z`` suffix (it adds the server's UTC offset before calling ``toISOString``).
So the trailing ``Z`` is a lie — we treat the timestamps as naive wall-clock
local time and never timezone-convert them. The calendar day is simply the date
portion of the start string.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Optional

from . import config

# Raw fields that define an event's *content*. Bookmarks are intentionally
# excluded: they change constantly and would flood the change history.
CONTENT_FIELDS = (
    "name",
    "description",
    "start_datetime",
    "end_datetime",
    "plaintext_location",
    "event_site_location",
    "event_site_location_name",
    "event_type",
    "will_be_filmed",
    "av_needs",
    "creator_name",
    "created_by_account_id",
)


def event_url(event_id: Optional[str], base: Optional[str] = None) -> Optional[str]:
    """Deep link into the my.vibe.camp app at this event's detail view.

    Mirrors the upstream backend's share redirect exactly (compact JSON, like
    JS ``JSON.stringify``). Opening it lets a logged-in user star / RSVP the
    event natively — we never write upstream.
    """
    if not event_id:
        return None
    import json
    import urllib.parse

    base = (base or config.FRONT_END_BASE_URL).rstrip("/")
    frag = urllib.parse.quote(
        json.dumps(
            {"currentView": "Events", "viewingEventDetails": event_id},
            separators=(",", ":"),
        )
    )
    return f"{base}/#{frag}"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an upstream timestamp as naive local wall-clock time."""
    if not value:
        return None
    s = value.strip()
    # Drop the misleading timezone marker so we keep wall-clock time.
    if s.endswith("Z"):
        s = s[:-1]
    # Trim fractional seconds to something fromisoformat reliably handles.
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Last resort: strip fractional seconds.
        if "." in s:
            try:
                return datetime.fromisoformat(s.split(".", 1)[0])
            except ValueError:
                return None
        return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def content_hash(raw: dict[str, Any]) -> str:
    """Stable hash over content fields (ignores bookmarks/volatile fields)."""
    payload = {k: raw.get(k) for k in CONTENT_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_placeholder(raw: dict[str, Any]) -> bool:
    dt = _parse_dt(raw.get("start_datetime"))
    if dt is None:
        return True
    return not (config.REAL_YEAR_MIN <= dt.year <= config.REAL_YEAR_MAX)


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Produce the normalized, derived-field representation of one raw event."""
    start = _parse_dt(raw.get("start_datetime"))
    end = _parse_dt(raw.get("end_datetime"))

    duration = None
    if start and end and end >= start:
        duration = int((end - start).total_seconds() // 60)

    site_name = _clean_str(raw.get("event_site_location_name"))
    plaintext = _clean_str(raw.get("plaintext_location"))
    location = site_name or plaintext

    # Upstream calls this "bookmarks"; the my.vibe.camp UI labels it "stars".
    # We carry both names so callers can speak either dialect.
    bookmarks = raw.get("bookmarks") or 0
    try:
        bookmarks = int(bookmarks)
    except (TypeError, ValueError):
        bookmarks = 0

    return {
        "event_id": raw.get("event_id"),
        "name": _clean_str(raw.get("name")) or "(untitled)",
        "description": raw.get("description") or "",
        "event_type": _clean_str(raw.get("event_type")),
        "start_datetime": raw.get("start_datetime"),
        "end_datetime": raw.get("end_datetime"),
        "start_date": start.date().isoformat() if start else None,
        "duration_minutes": duration,
        "location": location,
        "event_site_location": _clean_str(raw.get("event_site_location")),
        "event_site_location_name": site_name,
        "plaintext_location": plaintext,
        "creator_name": _clean_str(raw.get("creator_name")),
        "created_by_account_id": _clean_str(raw.get("created_by_account_id")),
        "will_be_filmed": bool(raw.get("will_be_filmed")),
        "av_needs": _clean_str(raw.get("av_needs")),
        "bookmarks": bookmarks,
        "is_placeholder": is_placeholder(raw),
        "content_hash": content_hash(raw),
    }
