# CLAUDE.md — Vibe Camp Expansion

Agent-friendly REST API + MCP server over the Vibe Camp event schedule. Read
this before working in the repo.

## Terminology gotchas (read first)

- **`bookmarks` == `stars`.** Upstream (`vibecamp/vibecamp-web`) stores the
  per-event save count in a field literally named `bookmarks`. But the
  **my.vibe.camp user interface — and the people using it — call these
  "stars."** They are the same number. We expose both: every event carries
  `bookmarks` *and* `stars` (identical values), `sort=stars` is an alias for
  `sort=bookmarks`, and the API accepts both `min_stars` and `min_bookmarks`.
  When talking to a human, say "stars." When reading raw upstream payloads,
  expect "bookmarks."

- **Timestamps are wall-clock, not UTC.** Upstream formats local event times
  with a trailing `Z` (a backend hack: it adds the server's offset then calls
  `toISOString`). The `Z` is a lie. We treat all timestamps as naive local
  wall-clock and never timezone-convert. The calendar day is just the date
  portion of `start_datetime`.

## Editions

The feed contains every past edition's events. "Vibe Camp 5" is the 2026
edition; 2024 (Vibeclipse + VC3) and 2025 (VC4) are prior editions.

- **Default behavior: only the current edition is surfaced.** The current
  edition is the date window `[CURRENT_EDITION_START, CURRENT_EDITION_END)` in
  `config.py` (default `2026-01-01` .. `2027-01-01`). `GET /events`, `/days`,
  and the MCP `search_events` / `list_days` / `popular_events` tools all clamp
  to it by default.
- **Historical events are never deleted from the cache** — they're just hidden.
  Pass `include_historical=true` (API + MCP) to see all editions.
- An **explicit date narrowing** (`day`, `start_after`, or `start_before`)
  overrides the edition clamp — if a caller asks for a specific historical
  day, they get it without needing `include_historical`.
- Rolling to the next edition is a config change (env vars
  `VIBECAMP_EDITION_START` / `_END` / `VIBECAMP_EDITION_NAME`), no code edit.

## Data lifecycle (the crawler's job)

Each crawl fully reconciles the feed against the cache:
- new event → insert + `created` history
- content change → update + `updated` history **with a field-level diff**
- vanished upstream → **soft-delete** (`is_deleted`, `deleted_at`); still in DB,
  queryable with `include_deleted=true`
- reappeared → **resurrected**
- **bookmark/star changes alone do NOT create history** — they churn every
  crawl and would flood the log. They update silently. Only genuine content
  changes are logged. (See `normalize.CONTENT_FIELDS`.)

Never break the "never lose data" invariant: do not hard-delete events, and do
not let bookmark churn into the content hash.

## Layout

```
vibecamp_expansion/
  config.py       env-driven config (upstream URL, edition window, year bounds)
  models.py       pydantic contract for API + MCP
  normalize.py    raw upstream payload -> normalized event (+ content_hash)
  store.py        SQLite + FTS5; reconcile(), history, crawl_log, queries
  crawler.py      fetch + reconcile; crawl_once / crawl_loop
  api.py          FastAPI REST (auto OpenAPI at /docs) + /data static mount
  mcp_server.py   FastMCP tools (stdio locally; mounted at /mcp/ when hosted)
  export.py       static grep-friendly files (events.ndjson, schedule.md, llms.txt, …)
  asgi.py         DEPLOY ENTRY: one app = REST + /data + remote MCP + crawler thread
  cli.py          vibecamp crawl|serve|mcp|stats|export
```

## Three consumption tiers (all from one crawler/cache)

1. **Static files** (`export.py` → served at `/data`): an agent fetches one
   file and greps/jq's locally. Most resilient, zero API. Regenerated every
   crawl. This is the primary agent interface for bulk use.
2. **REST API** (`api.py`): server-side filter/search.
3. **Remote MCP** (`mcp_server.py` mounted at `/mcp/` by `asgi.py`): turnkey
   URL for MCP clients. Stateless streamable-http transport.

Deploy = `vibecamp_expansion.asgi:app` (Dockerfile / render.yaml / Procfile).
The MCP transport mounts at `/mcp/` with `streamable_http_path="/"` (mounting
the default `/mcp`-rooted app at `/mcp` would yield `/mcp/mcp` — don't). A
slash-less `/mcp` 307-redirects to `/mcp/`.

## Conventions

- Clean, typed Python. Tests are hermetic (no network) — keep them that way;
  the crawler is the only component that touches the network, and its fetch is
  injectable for tests.
- `pytest` must stay green and fast.
- Placeholder/joke events (implausible years like 1999, 3025) are flagged
  `is_placeholder` and hidden by default — keep that filter.
