# Vibe Camp Telegram Bot — Publicity & Scaling Audit

The schedule concierge attendees talk to. This doc has two halves:

1. **Tell people how to use it** — copy/paste-ready blurbs + the link.
2. **What happens when many people use it at once** — a scaling audit with
   severity, current status, and what's left to do.

---

## Part 1 — How people talk to the bot

**Bot:** **VibeCampBot** — **[@FiveCampEvent2026_bot](https://t.me/FiveCampEvent2026_bot)**
**Link to share:** `https://t.me/FiveCampEvent2026_bot`

It's a **direct-message concierge**: open the link, hit **Start**, and just say
what you want in plain English. No commands to memorize.

### The two best things to ask

> ⭐ **"What's coming up soon?"** — what's happening right now and next
> ⭐ **"What's good for someone into ___?"** — picks for your interest

It also understands days (**Thursday–Sunday**), places ("anything at the pool?"),
vibes ("something chill and social", "live music"), and specific things
("sea shanties", "tarot").

### Copy-paste announcement (for Discord / signage / the group chat)

> 🏕️ **Lost in the schedule? DM our bot.**
> Open **t.me/FiveCampEvent2026_bot**, tap Start, and ask it anything:
> • "what's happening right now?"
> • "what's good for someone into AI?"
> • "anything at the pool Saturday?"
> It reads the whole schedule and gives you real picks — not a wall of text.

### Short version (for a sign / sticker / slide)

> **Schedule bot →** t.me/FiveCampEvent2026_bot
> Tap Start. Ask "what's on now?"

### Commands (optional — plain English works too)

| Command | Does |
|---|---|
| `/now` | What's happening right now & starting soon |
| `/day <Thu–Sun>` | A day's schedule (e.g. `/day Friday`) |
| `/pool` | Upcoming events at the Pool |
| `/popular` | Top upcoming events by stars |
| `/events <text>` | Keyword search the schedule |
| `/event <id>` | Full detail for one event |
| `/help` | The quick guide |

### Using it in a group chat

The bot can be added to a group, but **privacy mode is ON** — in a group it only
sees **commands** (`/now`) or messages that **@mention** it
(`@FiveCampEvent2026_bot what's on?`). Free-text "just ask it" only works in a
**direct message**. For attendees, **DMing it is the experience to promote.**

---

## Part 2 — Scaling audit (many users at once)

Severity legend: 🔴 must address · 🟡 watch / address if it grows · 🟢 handled.

### 🟢 Concurrency — users no longer queue behind each other *(fixed this pass)*
Each free-text reply now makes a multi-second LLM call (the concierge *thinks*
before choosing). python-telegram-bot processes updates **sequentially** by
default, so 10 simultaneous users would have waited ~60s in line. **Fixed:**
`concurrent_updates(True)` is now set, so users are served in parallel. An
**error handler** is also registered so one bad update can't crash the worker or
spam tracebacks.

### 🟢 Single-instance polling — exactly one poller, by design
A long-poll bot **must** be a single instance, or Telegram throws
`Conflict: terminated by other getUpdates`. The Railway `vibecamp-telegram`
service runs **one replica, one active deployment**; Railway's `ON_FAILURE`
restart policy brings it back if it dies. *Operational rule:* never scale this
service past 1 replica, and never run a second copy locally/on Atlas while prod
is live. Watch deploy logs for a recurring (not just at-handover) `Conflict`.

### 🟡 Anthropic API cost & rate limits — the main cost driver at scale
Every free-text message = **one `claude-opus-4-8` call** with adaptive thinking.
Mitigations already in place:
- The **event pool is prompt-cached** (5-min ephemeral). Concurrent users within
  that window share the cached prefix, so input cost is paid roughly once per
  window, not once per user.
- Effort is **`medium`** (env `ANTHROPIC_EFFORT`) — dial to `low` to cut
  cost/latency, or `high` for sharper picks.
- On any API error or rate-limit (429), `smart_select` returns `None` and the
  bot **falls back to keyword search** — it degrades, it doesn't break.

*To do:* set a **billing/usage alert** on the Anthropic account before wide
promotion, and skim logs for `RateLimitError`. Output + thinking tokens are
billed per message, so cost scales with traffic even with caching.

### 🟡 No per-user rate limiting — abuse/spam runs up cost
Nothing throttles a single chat today; a bored user (or a script) could hammer
the bot and drive API spend. *To do if abuse appears:* a simple per-chat cap
(e.g. N messages / minute) in `text_message`, or a cooldown reply.

### 🟢 Graceful degradation — the brain failing doesn't break the bot
No `ANTHROPIC_API_KEY`, SDK error, rate-limit, or non-JSON response all route to
the keyword-search fallback. Telegram's 4096-char limit is handled by truncation.
The bot is **read-only** — it never writes upstream and stores no user data/PII.

### 🟡 Shared REST backend — one SQLite-backed service feeds everything
Each bot reply makes a few `/events` calls to the `vibecamp-expansion` API
(also serving Discord + the static site). Reads are cheap and SQLite handles
concurrent reads fine, and data is cache-backed (crawler refreshes ~every 5 min).
It is a **shared single point of failure**, though. *To do:* confirm it has
adequate resources before a traffic spike; if it ever struggles, the bots'
static-file tier (`/data`) is the cheaper bulk path.

### 🟢 Telegram platform limits — not a concern at this scale
Telegram allows ~30 messages/sec globally and ~1/sec per chat. The bot replies
once per user message, so these limits aren't in play unless we start
broadcasting. Token-leaking INFO logs were silenced (httpx/telegram at WARNING).

### Pre-promotion checklist
- [ ] Anthropic **billing/usage alert** set (🟡 cost).
- [ ] Confirm `vibecamp-telegram` is **1 replica**; nothing else polling the token.
- [ ] Confirm the `vibecamp-expansion` API service is healthy / has headroom.
- [ ] Decide `ANTHROPIC_EFFORT` (`low` cheap/fast · `medium` default · `high` sharp).
- [ ] (Optional) add per-chat rate limit before mass promotion.
- [ ] Smoke-test the share link end-to-end from a phone that's never used it.

---
*Bot handle and link are public. The bot token and API key are secrets and live
only in Railway env vars — never put them in this doc or anywhere in the repo.*
