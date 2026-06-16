# Vibe Camp Expansion

An agent-friendly, better-UX layer over the [Vibe Camp](https://vibe.camp) event
schedule.

The upstream backend (`vibecamp/vibecamp-web`) exposes a single unfiltered
`GET /api/v1/events` endpoint that returns all ~700 events as one blob. This
project crawls that feed on a schedule into a local cache and serves it back as:

- a **clean, filterable REST API** (FastAPI, with auto-generated OpenAPI), and
- an **MCP server** so any agent can query the schedule conversationally.

## Point your agent at it (10 seconds)

Live endpoint: **https://vibecamp-expansion-production.up.railway.app** — visit it
in a browser for copy-paste setup, or:

**Claude Code** — one command, then just chat:
```bash
claude mcp add --transport http vibecamp https://vibecamp-expansion-production.up.railway.app/mcp/
```

**Claude Desktop · Cursor · OpenClaw · any MCP client** — add to your config:
```json
{ "mcpServers": { "vibecamp": { "url": "https://vibecamp-expansion-production.up.railway.app/mcp/" } } }
```

Then talk to it: *"What's on at the Pool Saturday night?"*, *"I'm into consciousness
and AI — what should I go to?"*, *"Find the sea shanties."* Each result links into
my.vibe.camp so you can star / RSVP natively.

No agent? `curl https://vibecamp-expansion-production.up.railway.app/data/events.ndjson` and grep.

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

## Three ways to consume it

| Tier | Best for | How |
|---|---|---|
| **Static files** (`/data/*`) | agents that fetch once and grep/jq locally; max resilience | `curl …/data/events.ndjson` |
| **REST API** | server-side filtering & search | `GET …/events?q=music&day=2026-06-19` |
| **Remote MCP** | turnkey "add a URL" in Claude/Cursor | point client at `…/mcp/` |

All three are fed by one crawler and one SQLite cache.

## Architecture

```
upstream /api/v1/events
        │  (every ~5 min, in-process thread)
        ▼
   crawler.py ── reconcile ──► SQLite cache (store.py) ──► export.py (static files)
                                 │  events + history + crawl_log + FTS5
        ┌────────────────────────┼───────────────────────────┬─────────────┐
        ▼                        ▼                           ▼             ▼
  api.py (REST)         mcp_server.py (MCP tools)     /data static     export files
                                                                       (events.ndjson,
   all served by one ASGI app: vibecamp_expansion.asgi:app             schedule.md, …)
```

## Static exports (the grep layer)

Every crawl regenerates a set of flat files under `/data` (and `vibecamp export`
writes them locally). An agent can fetch one file and work entirely offline:

| File | What |
|---|---|
| `index.json` | manifest: generated_at, edition, counts, file list |
| `llms.txt` | plain-text guide for agents (start here) |
| `events.ndjson` | one event JSON per line (current edition) — `curl … \| jq`/`grep` |
| `events.json` | single JSON object with metadata |
| `schedule.md` | day-grouped, human + agent readable |
| `events.csv` | spreadsheet-friendly flat table |
| `events.ics` | iCalendar feed |
| `events.all.ndjson` / `events.all.json` | every edition (2024/25/26) |

```bash
# Everything an agent needs, no API:
curl -s https://HOST/data/events.ndjson | jq 'select(.location=="Pool")'
curl -s https://HOST/data/schedule.md | grep -i shanty
```

## Deployment

The whole thing is one ASGI app (`vibecamp_expansion.asgi:app`) that serves
REST + static + remote MCP and runs the crawler in a background thread.

- **Docker:** `docker build -t vibecamp . && docker run -p 8787:8787 vibecamp`
- **Render:** connect the repo as a Blueprint (`render.yaml` included).
- **Railway / Fly / Heroku-likes:** `Procfile` included; honors `$PORT`.

Notes:
- The cache + exports live in `VIBECAMP_DATA_DIR` (`/data` in the container).
  Mount a volume there for persistent history; otherwise it rebuilds on boot
  (upstream is the source of truth, so this is safe — only the change log
  resets).
- On hosts that sleep idle web services (e.g. Render free tier), the in-process
  crawler pauses while asleep. For an always-fresh crawler use a non-sleeping
  host, or run the crawler externally and set `VIBECAMP_DISABLE_CRAWLER=1`.

### Connect an MCP client to the hosted server

```json
{
  "mcpServers": {
    "vibecamp": { "url": "https://HOST/mcp/" }
  }
}
```

(Or run it locally over stdio — see below.)

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

## Discord bot

A read-only Discord bot exposes the schedule as slash commands. It is a thin
client over the REST API above — it holds no state and never writes upstream.
It ships as an optional extra so it doesn't weigh down the core install.

```bash
pip install -e ".[discord]"
export DISCORD_BOT_TOKEN=...          # from the Discord Developer Portal (never commit this)
# export VIBECAMP_API_BASE=https://vibecamp-expansion-production.up.railway.app  # default
# export DISCORD_GUILD_ID=123456789   # optional: instant slash-command sync to one guild
vibecamp discord
```

### Slash commands

| Command | What |
|---|---|
| `/events query:<text>` | Full-text search; top results by stars |
| `/pool` | Events at the Pool venue |
| `/shanties` | Find the sea shanties |
| `/day date:<YYYY-MM-DD>` | Events on a day (defaults to the first festival day) |
| `/popular` | Top events by stars |
| `/recommend interest:<text>` | Curated picks — searches each word of the interest, unions, de-dups, ranks by stars |
| `/event id:<event_id>` | Full detail for one event (ids appear in list results) |

Each list shows name, day + `HH:MM`, venue, and **stars** (the my.vibe.camp
label for upstream `bookmarks`).

### Creating the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**, name it (e.g. "Vibe Camp").
2. Open the **Bot** tab → **Add Bot**. Click **Reset Token** to reveal the
   token and set it as `DISCORD_BOT_TOKEN`. **Treat it like a password — never
   commit it.**
3. No privileged intents are required. The bot only uses the default `guilds`
   intent (slash commands), so you can leave Presence / Server Members /
   Message Content **off**.
4. Invite the bot to your server with the **`bot`** and **`applications.commands`**
   scopes. Build the URL from your application's Client ID:

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=0
   ```

   (`permissions=0` is fine — the bot only posts embeds in response to slash
   commands.) Or use the Developer Portal's **OAuth2 → URL Generator** with the
   same two scopes.
5. Run `vibecamp discord`. With `DISCORD_GUILD_ID` set, commands appear in that
   guild immediately; without it, global commands can take up to an hour to
   propagate.

### Bot environment variables

| Var | Default | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | **Required.** From the Developer Portal. |
| `VIBECAMP_API_BASE` | hosted Railway app | Point at a local `vibecamp serve` for dev. |
| `DISCORD_GUILD_ID` | — | Optional; instant per-guild command sync during dev. |

## Telegram bot

A read-only Telegram bot for **one-on-one chat**. It mirrors the Discord
commands and, crucially, treats any plain-text direct message as a
recommendation query — a person can just DM it "live music and art" and get a
curated list back. Same shared API client as the Discord bot (`bot_api.py`).

```bash
pip install -e ".[telegram]"
export TELEGRAM_BOT_TOKEN=...   # from @BotFather (never commit this)
vibecamp telegram
```

Commands: `/start` · `/help` · `/events <text>` · `/pool` · `/shanties` ·
`/day <YYYY-MM-DD>` · `/popular` · `/recommend <interest>` · `/event <id>`,
plus **any plain message → recommendations**.

### Creating the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot`,
   choose a name and username.
2. BotFather replies with a token — set it as `TELEGRAM_BOT_TOKEN`. **Treat it
   like a password; never commit it.**
3. (Optional) `/setcommands` in BotFather, pasting the command list above, so
   they autocomplete in the chat.
4. Run `vibecamp telegram`. Open a DM with your bot and say hi.

## Hosting the bots (Railway / Render)

Both bots are long-running workers (a persistent connection, not a web
request), so they run as **separate services** alongside the web service. To
keep one Docker image for everything, the container picks its role from
`VIBECAMP_ROLE` (see the `Dockerfile`):

| Service | `VIBECAMP_ROLE` | Required secret | Other env |
|---|---|---|---|
| web (REST + MCP + crawler) | `web` (default) | — | `VIBECAMP_DATA_DIR=/data` |
| Discord bot | `discord` | `DISCORD_BOT_TOKEN` | `VIBECAMP_API_BASE=<web URL>` |
| Telegram bot | `telegram` | `TELEGRAM_BOT_TOKEN` | `VIBECAMP_API_BASE=<web URL>` |

**Railway** — add one service per bot from this repo, then set its variables:

```bash
# Discord worker (same repo + image, role via env var):
railway add --service vibecamp-discord --repo BenevolentFutures/vibecamp-expansion \
  --variables VIBECAMP_ROLE=discord \
  --variables DISCORD_BOT_TOKEN=xxxxx \
  --variables VIBECAMP_API_BASE=https://vibecamp-expansion-production.up.railway.app

# Telegram worker:
railway add --service vibecamp-telegram --repo BenevolentFutures/vibecamp-expansion \
  --variables VIBECAMP_ROLE=telegram \
  --variables TELEGRAM_BOT_TOKEN=xxxxx \
  --variables VIBECAMP_API_BASE=https://vibecamp-expansion-production.up.railway.app
```

Because the role is just an env var, no per-service start command is needed.
The bots have no inbound HTTP, so they don't need a public domain.

**Render** — the workers are already declared in `render.yaml` (`type: worker`);
fill in each token (marked `sync: false`) in the dashboard after the blueprint
syncs.

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
