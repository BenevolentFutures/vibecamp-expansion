"""Pydantic contract for the API and MCP layers.

These are the normalized, agent-friendly shapes — distinct from the raw
upstream payload (see ``normalize.py`` for the mapping).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    UNOFFICIAL = "UNOFFICIAL"
    CAMPSITE_OFFICIAL = "CAMPSITE_OFFICIAL"
    TEAM_OFFICIAL = "TEAM_OFFICIAL"


class Event(BaseModel):
    """A normalized event as served to agents and clients."""

    event_id: str
    name: str
    description: str = ""
    event_type: Optional[str] = None

    # Wall-clock local times (upstream stores local time labeled as UTC).
    start_datetime: Optional[str] = Field(
        None, description="ISO-8601 local wall-clock start time."
    )
    end_datetime: Optional[str] = Field(
        None, description="ISO-8601 local wall-clock end time, if any."
    )
    start_date: Optional[str] = Field(
        None, description="Calendar day of the start (YYYY-MM-DD), local."
    )
    duration_minutes: Optional[int] = Field(
        None, description="end - start in minutes, when both are present."
    )

    # Location: either a named on-site location or free-text.
    location: Optional[str] = Field(
        None, description="Best human-readable location (site name or plaintext)."
    )
    event_site_location: Optional[str] = None
    event_site_location_name: Optional[str] = None
    plaintext_location: Optional[str] = None

    creator_name: Optional[str] = None
    created_by_account_id: Optional[str] = None
    will_be_filmed: bool = False
    av_needs: Optional[str] = None
    bookmarks: int = Field(
        0, description="Upstream's name for stars. Equal to `stars`."
    )
    stars: int = Field(
        0,
        description="The my.vibe.camp UI label for `bookmarks`. Same number, "
        "exposed under the name people actually use.",
    )

    # Derived flags.
    is_placeholder: bool = Field(
        False,
        description="True for joke/placeholder entries with implausible dates.",
    )
    is_deleted: bool = Field(
        False, description="True if the event has disappeared upstream."
    )

    # Lifecycle metadata from our crawler.
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    deleted_at: Optional[str] = None
    revision: int = Field(
        0, description="Number of content changes observed since first seen."
    )


class EventList(BaseModel):
    events: list[Event]
    total: int = Field(..., description="Total matching events (before paging).")
    limit: int
    offset: int


class FieldChange(BaseModel):
    field: str
    old: object | None = None
    new: object | None = None


class HistoryEntry(BaseModel):
    id: int
    event_id: str
    change_type: Literal["created", "updated", "deleted", "resurrected"]
    changed_at: str
    changes: list[FieldChange] = []
    event_name: Optional[str] = None


class HistoryList(BaseModel):
    history: list[HistoryEntry]
    total: int
    limit: int
    offset: int


class Site(BaseModel):
    event_site_location: str
    event_site_location_name: Optional[str] = None
    event_count: int = 0


class DaySummary(BaseModel):
    date: str
    event_count: int
    first_start: Optional[str] = None
    last_start: Optional[str] = None


class CrawlStatus(BaseModel):
    last_crawl_at: Optional[str] = None
    last_status: Optional[str] = None
    last_http_status: Optional[int] = None
    last_event_count: Optional[int] = None
    last_error: Optional[str] = None
    total_crawls: int = 0
    consecutive_failures: int = 0


class Stats(BaseModel):
    total_events: int
    active_events: int
    deleted_events: int
    placeholder_events: int
    real_upcoming_events: int
    edition_name: str = Field(..., description="The current edition surfaced by default.")
    edition_events: int = Field(..., description="Active events in the current edition.")
    by_type: dict[str, int]
    by_site: dict[str, int]
    filmed_events: int
    events_with_av_needs: int
    earliest_event: Optional[str] = None
    latest_event: Optional[str] = None
    crawl: CrawlStatus
