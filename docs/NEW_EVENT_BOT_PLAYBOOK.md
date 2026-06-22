# Playbook: Spin up an event schedule bot for a new event

This codifies how to reproduce the Vibe Camp bot for **any** event with a
schedule feed. Goal: a smart Telegram/Discord concierge + REST/MCP/static
access, live on Railway, in an afternoon. Read this top-to-bottom once; then use
the checklist at the bottom.

## What you're building (architecture)

```
upstream schedule feed ──► crawler+cache (SQLite, FTS) ──► reconcile (never lose data)
                                     │
        ┌────────────────────────────┼─────────────────────────────┐
        ▼                            ▼                              ▼
  static exports (/data)        REST API (/events,/days)      remote MCP (/mcp)
   ndjson, schedule.md             FastAPI                     FastMCP tools
        │                            │
        └──────────────► chat bots (Telegram + Discord) ◄────────┘
                          bot_api (shared HTTP client + helpers)
                          bot_llm (LLM concierge, structured output)
```

One crawler/cache feeds three consumption tiers (static files, REST, MCP) plus
the bots. The bots are thin, read-only clients over the REST API. The
"intelligence" is `bot_llm.smart_select`: it hands the event pool to the model,
which classifies the request and either picks events (semantic) or sets
day/venue/mode flags that **code** then executes deterministically.

## What changes per event vs. what's reused

**Reused as-is (~90% of the code):** the crawler/reconcile/cache, REST API, MCP
server, static exports, both bots, the LLM concierge, rate limiting, analytics,
the durable user store, `/broadcast`, the eval harness.

**Swap per event (all env-driven — usually no code edits):**

| Concern | Where | Note |
|---|---|---|
| Upstream feed URL | `VIBECAMP_UPSTREAM_BASE_URL` (`config.py`) | the new event's schedule API |
| Edition window | `VIBECAMP_EDITION_START/_END/_NAME` | which dates are "current" |
| **Timezone** | `VIBECAMP_LOCAL_TZ` (default `America/New_York`) | event-local wall clock; gets "now/soon/today" right |
| Bot tokens | `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN` | from @BotFather / Discord dev portal |
| Anthropic key | `ANTHROPIC_API_KEY` | the LLM brain |
| Admin | `TELEGRAM_ADMIN_USERNAMES` | who can `/stats`, `/model`, `/broadcast` |
| Model / effort | `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT` | cost/quality dial |
| Durable store | `VIBECAMP_USER_DB=/data/users.db` | needs a mounted volume |

If the new feed has a **different payload shape**, the only real code work is
`normalize.py` (raw upstream → normalized event + content hash) — keep the
`CONTENT_FIELDS` discipline so churny fields (e.g. star counts) don't spam the
history log.

## Setup, step by step

1. **Clone & rename.** Copy the repo. Rename the package/`prog` if you want a new
   brand; nothing else depends on the name.
2. **Point at the new feed.** Set `VIBECAMP_UPSTREAM_BASE_URL`, the edition
   window, and `VIBECAMP_LOCAL_TZ`. Run `vibecamp crawl` locally and eyeball
   `vibecamp stats` + `/events?limit=3` to confirm the shape parses (adjust
   `normalize.py` if needed).
3. **Create the bot.** Telegram: @BotFather → new bot → token. Set bot privacy as
   you like (privacy ON = only sees commands/@mentions in groups; DMs always
   work).
4. **Secrets.** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN` (never commit; Railway
   env only). The Dockerfile dispatches by `VIBECAMP_ROLE` (`telegram` /
   `discord` / `api`) — one image, three services.
5. **Deploy on Railway.** One project, a service per role. Entry point is
   `vibecamp_expansion.asgi:app` for the API; the bots run `vibecamp telegram` /
   `vibecamp discord`. Deploy with `railway up --ci`.
6. **Provision a volume** on the telegram service (mount `/data`) and set
   `VIBECAMP_USER_DB=/data/users.db` so the subscriber list + usage survive
   redeploys. (GraphQL `volumeCreate{ projectId, environmentId, serviceId,
   mountPath:"/data" }`.)
7. **Set admin** `TELEGRAM_ADMIN_USERNAMES=<your-handle>` to lock `/stats`,
   `/model`, `/broadcast` to you.
8. **Run the eval** (`ANTHROPIC_API_KEY=… python eval_bot.py`) against the live
   API before announcing — it checks day/venue/now/popular correctness + an LLM
   judge. Keep `pytest` green (hermetic, fast).
9. **Go live & promote.** Discoverability is the adoption lever (see
   `BOT_PUBLICITY.md`), not the bot itself.

## Hard-won design lessons (don't relearn these)

- **Model detects, code executes.** Let the model do *semantic* judgment (which
  interest/venue/day/host) and have code do the *mechanical listing*. Asking the
  model to enumerate a 60-event day returns empty ~25% of the time; a
  deterministic `events_on_day` is 100% reliable. Same pattern for venue
  enforcement. This is the single biggest reliability lesson.
- **Only show actionable info.** Future-filter everything in conversational
  paths (hide events ended >1h ago); answer "now/soon" from a real event-local
  clock, not the UTC host clock.
- **Single-instance long-poll.** Exactly one bot instance may `getUpdates` or
  Telegram throws `Conflict`. Keep replicas = 1; never run a second copy.
- **Prompt-cache the event pool**, and keep the volatile timestamp in the *user
  turn* (after the cache breakpoint) — otherwise the ~20–30k-token pool is
  rewritten every minute (~10× cost).
- **Cost knobs:** model (`sonnet` ≈ half `opus`; `haiku` cheaper still), effort
  (`low`/`medium`), and trimming event descriptions in the prompt. Measure real
  `usage` — cold (cache-write) calls dominate.
- **Quiet token-leaking logs:** raise `httpx`/`telegram` loggers to WARNING so
  the bot token never lands in logs.
- **Privacy:** analytics hash chat ids; the broadcast store deliberately keeps
  real ids (opt-out, consented). Don't blur the two.
- **Outbound = microphone only.** Never auto-generate/auto-send. `/broadcast`
  sends the operator's verbatim text behind an explicit confirm.

## Gotchas

- Upstream timestamps may be naive wall-clock with a fake trailing `Z` — treat
  as local, never timezone-convert.
- Ephemeral container FS: anything that must persist (subscriber store) needs a
  volume.
- Telegram bots can only message users who messaged them first; you can't
  recover past users if you didn't durably store their chat ids.
- Cold-start latency: the first call after a redeploy is slow (cache miss +
  warmup) — not a bug.

## Validation & ops

- `pytest -q` (hermetic, ~0.5s, $0) on every change; `eval_bot.py` (~$0.35,
  live) before shipping LLM-affecting changes.
- **Pause** between events: remove each service's active deployment
  (`deploymentRemove`) — stops compute, keeps services/env/volume. **Resume**
  with `railway up`. **Don't delete** services (can take the volume with it).

## Checklist
- [ ] Feed URL + edition window + **timezone** set; `crawl` parses cleanly
- [ ] Bot token(s) + Anthropic key in Railway env (not committed)
- [ ] Three services deploying off `VIBECAMP_ROLE`; replicas = 1
- [ ] Volume mounted + `VIBECAMP_USER_DB` set (durable audience)
- [ ] `TELEGRAM_ADMIN_USERNAMES` set
- [ ] `pytest` green, `eval_bot.py` passes against live
- [ ] Promotion plan (the adoption lever)
- [ ] Pause plan documented for after the event
