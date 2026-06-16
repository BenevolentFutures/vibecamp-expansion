# Vibe Camp Expansion

An agent-friendly, better-UX layer over the [Vibe Camp](https://vibe.camp) event
schedule.

The upstream backend (`vibecamp/vibecamp-web`) exposes a single unfiltered
`GET /api/v1/events` endpoint that returns all ~700 events as one blob. This
project crawls that feed on a schedule into a local cache and serves it back as:

- a **clean, filterable REST API** (FastAPI, with auto-generated OpenAPI), and
- an **MCP server** so any agent can query the schedule conversationally.

## What it does well

- **Lifecycle-aware crawling.** Each crawl does a full reconciliation: new
  events are inserted, changed events are updated with a field-level diff,
  events that vanish upstream are **soft-deleted** (never lost — still queryable
  with `include_deleted=true`), and events that reappear are **resurrected**.
  Every change lands in an append-only history log.
- **No history spam.** Bookmark counts churn constantly; they update silently
  and don't pollute the change history. Only genuine content changes are logged.
- **Current edition by default.** The feed contains every past edition's
  events. By default the API/MCP surface only the current edition (**Vibe Camp
  5 / 2026** — the only events attendees care about). Historical events stay in
  the cache and are reachable with `include_historical=true`. Rolling editions
  is a config change, not a code change.
- **`stars` == `bookmarks`.** Upstream names the save-count `bookmarks`; the
  my.vibe.camp UI and people call it **stars**. Every event carries both
  (identical), `sort=stars` works, and `min_stars`/`min_bookmarks` are aliases.
- **Real vs. joke events.** The live data contains placeholder entries with
  implausible dates (year 1999, 3025). These are flagged `is_placeholder` and
  hidden by default.
- **Wall-clock times.** Upstream labels local times with a misleading `Z`
  suffix; we treat them as naive wall-clock and never timezone-convert.
- **Full-text search** over name/description/location/creator (SQLite FTS5).

## Architecture

```
upstream /api/v1/events
        │  (every ~5 min)
        ▼
   crawler.py ── reconcile ──► SQLite cache (store.py)
                                 │  events + event_history + crawl_log + FTS5
                    ┌────────────┴────────────┐
                    ▼                          ▼
             api.py (FastAPI REST)     mcp_server.py (MCP tools)
```

## Quickstart

```bash
pip install -e ".[dev]"

# Pull the feed once into ~/.vibecamp-expansion/vibecamp.db
vibecamp crawl

# Or crawl continuously every 5 minutes
vibecamp crawl --loop --interval 300

# Serve the REST API (http://127.0.0.1:8787/docs for interactive OpenAPI)
vibecamp serve

# Run the MCP server (stdio transport)
vibecamp mcp

# Print stats
vibecamp stats
```

## REST API

| Method & path | Purpose |
|---|---|
| `GET /events` | List/filter/search events (paginated) |
| `GET /events/{id}` | Single event |
| `GET /events/{id}/history` | Change history for one event |
| `GET /history` | Recent changes across all events |
| `GET /days` / `GET /days/{date}` | Day index / events on a day |
| `GET /sites` | On-site venues with counts |
| `GET /stats` | Schedule overview + crawler freshness |
| `GET /crawl/status` | Last crawl result |
| `GET /health` | Liveness |

`GET /events` filters: `q`, `type`, `site`, `creator`, `day`, `start_after`,
`start_before`, `filmed`, `has_av_needs`, `min_stars` (= `min_bookmarks`),
`include_placeholder`, `include_deleted`, `include_historical`, `sort`
(`start | -start | stars | name | recent`), `limit`, `offset`.

## MCP tools

`search_events`, `get_event`, `events_on_day`, `upcoming_events`,
`popular_events`, `list_days`, `list_sites`, `recent_changes`, `schedule_stats`.

### Register with Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "vibecamp": { "command": "vibecamp", "args": ["mcp"] }
  }
}
```

## Keeping it fresh

Run the crawler continuously (`vibecamp crawl --loop`) or on a scheduler. A
macOS launchd plist is provided in `deploy/` for a 5-minute cadence.

## Configuration (env vars)

| Var | Default |
|---|---|
| `VIBECAMP_UPSTREAM_BASE_URL` | `https://backend-2-6ri5.onrender.com/api/v1` |
| `VIBECAMP_DATA_DIR` | `~/.vibecamp-expansion` |
| `VIBECAMP_DB_PATH` | `$DATA_DIR/vibecamp.db` |
| `VIBECAMP_CRAWL_INTERVAL` | `300` |
| `VIBECAMP_REAL_YEAR_MIN` / `_MAX` | `2020` / `2030` |
| `VIBECAMP_EDITION_NAME` | `Vibe Camp 5` |
| `VIBECAMP_EDITION_START` / `_END` | `2026-01-01` / `2027-01-01` |

## Tests

```bash
pytest        # hermetic, no network
```
