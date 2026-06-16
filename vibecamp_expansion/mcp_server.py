"""MCP server exposing the cached Vibe Camp schedule as agent tools.

Reads directly from the same SQLite cache the crawler writes and the REST API
serves, so all three layers stay consistent. Run with::

    vibecamp mcp            # stdio transport (for Claude Desktop / clients)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .store import Store

mcp = FastMCP("vibecamp")
_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def set_store(s: Store) -> None:
    global _store
    _store = s


def _now_local() -> str:
    return datetime.now().isoformat(timespec="seconds")


@mcp.tool()
def search_events(
    query: str = "",
    event_type: Optional[str] = None,
    site: Optional[str] = None,
    creator: Optional[str] = None,
    day: Optional[str] = None,
    start_after: Optional[str] = None,
    start_before: Optional[str] = None,
    min_bookmarks: Optional[int] = None,
    include_placeholder: bool = False,
    sort: str = "start",
    limit: int = 25,
) -> dict[str, Any]:
    """Search and filter Vibe Camp events.

    Args:
        query: Free-text search over name, description, location, and creator.
        event_type: UNOFFICIAL, CAMPSITE_OFFICIAL, or TEAM_OFFICIAL.
        site: Exact on-site location name (e.g. "Barn Theater"). Use list_sites.
        creator: Exact creator/host name.
        day: Calendar day YYYY-MM-DD (local wall-clock time).
        start_after / start_before: ISO datetime bounds (e.g. "2026-06-19T18:00").
        min_bookmarks: Only events with at least this many bookmarks.
        include_placeholder: Include joke/placeholder-dated events (default off).
        sort: start | -start | bookmarks | name | recent.
        limit: Max events to return (1-200).
    """
    rows, total = store().query_events(
        q=query or None, event_type=event_type, site=site, creator=creator,
        day=day, start_after=start_after, start_before=start_before,
        min_bookmarks=min_bookmarks, include_placeholder=include_placeholder,
        sort=sort, limit=max(1, min(limit, 200)),
    )
    return {"total": total, "returned": len(rows), "events": rows}


@mcp.tool()
def get_event(event_id: str) -> dict[str, Any]:
    """Get the full details of a single event by its event_id."""
    row = store().get_event(event_id)
    if row is None:
        return {"error": "not_found", "event_id": event_id}
    return row


@mcp.tool()
def events_on_day(date: str) -> dict[str, Any]:
    """List all events on a given calendar day (YYYY-MM-DD), ordered by start time."""
    rows, total = store().query_events(day=date, sort="start", limit=1000)
    return {"date": date, "total": total, "events": rows}


@mcp.tool()
def upcoming_events(limit: int = 15, within_hours: Optional[int] = None) -> dict[str, Any]:
    """The next real (non-placeholder) events from now, in chronological order.

    Args:
        limit: Max events to return.
        within_hours: If set, only events starting within this many hours.
    """
    now = _now_local()
    before = None
    if within_hours is not None:
        from datetime import timedelta

        before = (datetime.now() + timedelta(hours=within_hours)).isoformat(timespec="seconds")
    rows, total = store().query_events(
        start_after=now, start_before=before, sort="start", limit=max(1, min(limit, 200)),
    )
    return {"from": now, "total": total, "events": rows}


@mcp.tool()
def popular_events(limit: int = 15) -> dict[str, Any]:
    """The most-bookmarked events (a proxy for popularity)."""
    rows, total = store().query_events(sort="bookmarks", limit=max(1, min(limit, 200)))
    return {"total": total, "events": rows}


@mcp.tool()
def list_days() -> dict[str, Any]:
    """List every calendar day that has events, with per-day counts."""
    return {"days": store().list_days()}


@mcp.tool()
def list_sites() -> dict[str, Any]:
    """List on-site locations (venues) with how many events each holds."""
    return {"sites": store().list_sites()}


@mcp.tool()
def recent_changes(change_type: Optional[str] = None, limit: int = 25) -> dict[str, Any]:
    """Recently observed schedule changes: created, updated, deleted, resurrected.

    Args:
        change_type: Filter to one of created | updated | deleted | resurrected.
        limit: Max history entries.
    """
    rows, total = store().history(change_type=change_type, limit=max(1, min(limit, 200)))
    return {"total": total, "changes": rows}


@mcp.tool()
def schedule_stats() -> dict[str, Any]:
    """Overview statistics for the whole schedule, plus crawler freshness."""
    return store().stats()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
