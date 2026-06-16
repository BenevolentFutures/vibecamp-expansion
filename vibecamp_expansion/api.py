"""FastAPI REST layer over the cached Vibe Camp schedule.

Read-only, filterable, paginated. The auto-generated OpenAPI doc at /docs and
/openapi.json makes this directly consumable by agents and codegen.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .models import (
    CrawlStatus,
    DaySummary,
    Event,
    EventList,
    HistoryEntry,
    HistoryList,
    Site,
    Stats,
)
from .store import Store

_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def set_store(store: Store) -> None:
    """Inject a store (used by tests)."""
    global _store
    _store = store


app = FastAPI(
    title="Vibe Camp Expansion API",
    version=__version__,
    description=(
        "An agent-friendly, filterable, cached view of the Vibe Camp event "
        "schedule. Data is crawled from the upstream backend roughly every "
        "five minutes; events that vanish upstream are soft-deleted and "
        "remain queryable with `include_deleted=true`."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/stats", response_model=Stats, tags=["meta"])
def stats(store: Store = Depends(get_store)) -> Stats:
    return Stats(**store.stats())


@app.get("/crawl/status", response_model=CrawlStatus, tags=["meta"])
def crawl_status(store: Store = Depends(get_store)) -> CrawlStatus:
    return CrawlStatus(**store.crawl_status())


@app.get("/events", response_model=EventList, tags=["events"])
def list_events(
    store: Store = Depends(get_store),
    q: Optional[str] = Query(None, description="Full-text search over name/description/location/creator."),
    type: Optional[str] = Query(None, description="UNOFFICIAL | CAMPSITE_OFFICIAL | TEAM_OFFICIAL"),
    site: Optional[str] = Query(None, description="Exact on-site location name, e.g. 'Barn Theater'."),
    creator: Optional[str] = Query(None, description="Exact creator name."),
    start_after: Optional[str] = Query(None, description="ISO datetime lower bound (inclusive)."),
    start_before: Optional[str] = Query(None, description="ISO datetime upper bound (inclusive)."),
    day: Optional[str] = Query(None, description="Calendar day YYYY-MM-DD (local wall-clock)."),
    filmed: Optional[bool] = Query(None),
    has_av_needs: Optional[bool] = Query(None),
    min_bookmarks: Optional[int] = Query(None, ge=0, description="Alias of min_stars."),
    min_stars: Optional[int] = Query(None, ge=0, description="UI name for min_bookmarks; same thing."),
    include_placeholder: bool = Query(False, description="Include joke/placeholder-dated events."),
    include_deleted: bool = Query(False, description="Include soft-deleted events."),
    include_historical: bool = Query(
        False,
        description="Include events from past editions. By default only the "
        "current edition (Vibe Camp 5 / 2026) is returned.",
    ),
    sort: str = Query("start", description="start | -start | bookmarks | stars | name | recent"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> EventList:
    rows, total = store.query_events(
        q=q, event_type=type, site=site, creator=creator,
        start_after=start_after, start_before=start_before, day=day,
        filmed=filmed, has_av_needs=has_av_needs,
        min_bookmarks=min_stars if min_stars is not None else min_bookmarks,
        include_placeholder=include_placeholder, include_deleted=include_deleted,
        include_historical=include_historical,
        sort=sort, limit=limit, offset=offset,
    )
    return EventList(
        events=[Event(**r) for r in rows], total=total, limit=limit, offset=offset
    )


@app.get("/events/{event_id}", response_model=Event, tags=["events"])
def get_event(event_id: str, store: Store = Depends(get_store)) -> Event:
    row = store.get_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return Event(**row)


@app.get("/events/{event_id}/history", response_model=HistoryList, tags=["history"])
def event_history(
    event_id: str,
    store: Store = Depends(get_store),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> HistoryList:
    rows, total = store.history(event_id=event_id, limit=limit, offset=offset)
    return HistoryList(
        history=[HistoryEntry(**r) for r in rows], total=total, limit=limit, offset=offset
    )


@app.get("/history", response_model=HistoryList, tags=["history"])
def history(
    store: Store = Depends(get_store),
    change_type: Optional[str] = Query(None, description="created | updated | deleted | resurrected"),
    since: Optional[str] = Query(None, description="ISO timestamp lower bound."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> HistoryList:
    rows, total = store.history(
        change_type=change_type, since=since, limit=limit, offset=offset
    )
    return HistoryList(
        history=[HistoryEntry(**r) for r in rows], total=total, limit=limit, offset=offset
    )


@app.get("/days", response_model=list[DaySummary], tags=["browse"])
def days(
    store: Store = Depends(get_store),
    include_placeholder: bool = Query(False),
    include_historical: bool = Query(False, description="Include past editions' days."),
) -> list[DaySummary]:
    return [
        DaySummary(**d)
        for d in store.list_days(
            include_placeholder=include_placeholder, include_historical=include_historical
        )
    ]


@app.get("/days/{date}", response_model=EventList, tags=["browse"])
def day_events(
    date: str,
    store: Store = Depends(get_store),
    limit: int = Query(1000, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> EventList:
    rows, total = store.query_events(day=date, sort="start", limit=limit, offset=offset)
    return EventList(
        events=[Event(**r) for r in rows], total=total, limit=limit, offset=offset
    )


@app.get("/sites", response_model=list[Site], tags=["browse"])
def sites(store: Store = Depends(get_store)) -> list[Site]:
    return [Site(**s) for s in store.list_sites()]


# Static, grep-friendly export files (events.ndjson, schedule.md, llms.txt, …)
# served at /data. An agent can fetch one file and work entirely locally.
config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data", StaticFiles(directory=str(config.EXPORT_DIR), html=False), name="data")
