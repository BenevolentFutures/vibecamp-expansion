# Kickoff: Vibe Camp Bot — Personality + Atin's Microphone

**Mission.** Give the Vibe Camp Telegram bot (`@FiveCampEvent2026_bot`) a strong,
lovable personality, and add a manual **broadcast / "PA system"** that is
strictly a *microphone for Atin* — never an autonomous sender. Two workstreams;
ship A first (low risk), gate B behind a plan checkpoint (consent + persistence).

**Read first:** repo `CLAUDE.md`; `vibecamp_expansion/{telegram_bot,bot_llm,bot_api,analytics,ratelimit}.py`;
`eval_bot.py`; `tests/`. This is a **live bot during camp** — single-instance
long-poll on Railway, structured-output concierge, deterministic day/venue
listing, admin gating already exists (`TELEGRAM_ADMIN_USERNAMES`, currently
`AtinAtinAtin`). The model default is `claude-sonnet-4-6`.

## 🔒 Core invariant (non-negotiable)
The bot **never originates an outbound message** except:
1. A **direct reply** to a user's own incoming message (normal day-to-day operation), or
2. A **broadcast that Atin personally wrote and explicitly confirmed**.

No AI-generated announcements. No automated, scheduled, or triggered messages of
any kind. The PA system is a microphone — it transmits Atin's exact words, nothing
the bot invents.

## Workstream A — Personality
- **Voice:** maximally playful, whimsical, community-fluent camp insider —
  "extremely teapot." Fluent in the crowd's in-jokes (AI/Claude, vibes, jhana,
  post-rat Twitter, gentle shitposting), warm underneath, never mean. Greets
  first-timers warmly.
  - *First build step:* draft **5 sample lines** in this voice (a greeting, a
    "couldn't find that", a day listing intro, a rate-limit notice, a playful
    small-talk deflection) and get Atin's thumbs-up **before** rewriting the
    system prompt. Pin down whether "teapot" is a specific reference or just
    "max whimsy" while doing this.
- **Where it lives:** the concierge `framing` line + static copy (`_HELP`,
  "couldn't find", rate-limit/daily messages, command titles). Voice is **tone
  only** — it must NOT change which events are returned.
- **Scope guardrail:** stays a schedule concierge. Small talk → one charming line
  that steers back to events. No open-ended chit-chat (a later project).
- **Hard rules:** (1) Correctness is sacred — day/venue/popular/now logic and the
  structured output stay reliable; personality rides on top. (2) "Dial it back"
  when someone clearly needs a fast real answer: lead with the answer, garnish
  after. (3) Public-facing safety: edgy-playful, never offensive/NSFW/exclusionary.
- **Validation:** add a personality dimension to `eval_bot.py` (LLM judge scoring
  "on-voice AND still correct"); confirm all existing correctness checks (day-only,
  venue-only, no-stale, sort) still pass; manual spot-check in chat. Watch token
  cost (personality adds output tokens — measure).

## Workstream B — Atin's Microphone (manual broadcast, opt-out)
- **Foundation first (the real work):** durably store **real `chat_id`s** + opt-out
  state. Today only *hashed* IDs are kept, in memory (resets on redeploy) — that
  must change for outbound. Needs a **persistent store** (Railway volume +
  SQLite recommended; the bot fs is ephemeral). Store the **minimum**: `chat_id`,
  subscribed/opted-out flag, first-seen. **No message content.**
- **Consent (opt-out):** everyone who has messaged the bot is auto-enrolled;
  **`/stop`** mutes, **`/start`** re-subscribes; every broadcast footer notes
  "reply /stop to mute." Opt-outs are durable and always honored.
- **`/broadcast` (admin = Atin only):** sends Atin's **verbatim** text to opted-in
  users. The bot does **not** rewrite or generate the content. Flow: Atin types
  the message → bot shows a **preview + recipient count** → Atin **explicitly
  confirms** → send. Support a dry-run. No confirm, no send.
- **Sending discipline (Telegram bans spammy bots):** throttle ~25/sec; on
  `403 blocked`/`deactivated`, auto-remove the dead `chat_id`; on `429`, honor
  `retry_after`; log a delivery summary (sent / failed / removed).
- **Explicitly out of scope:** automated reminders, digests, personalized nudges,
  and any AI-authored outbound. Design the store so these *could* be added later,
  but do not build them.
- **Validation:** hermetic tests for the subscriber store (add / opt-out / persist /
  round-trip) and throttle + dead-chat handling (injected clock + fake send); a
  real end-to-end test broadcast to **only Atin** before any wider send.

## Cross-cutting constraints
- Single-instance invariant; secrets only in Railway env (never logged/committed —
  the bot token already leaked once, keep it quiet); hermetic tests stay fast &
  green; run `eval_bot.py` before shipping LLM-affecting changes; commit in logical
  units, push, redeploy `vibecamp-telegram`, verify healthy single-instance polling.
- **Privacy note:** storing real `chat_id`s is a deliberate, consented reversal of
  the current no-PII hashing — scope it to the microphone only and document it.

## Phasing
1. **A (personality):** sample lines → Atin approval → build → eval → ship.
2. **B plan:** short spec for persistence + consent + `/broadcast` → **Atin checkpoint.**
3. **B build:** subscriber store → `/broadcast` with preview+confirm → test send to
   Atin only → enable. Nothing goes out without Atin's confirm, ever.

## Open questions for Atin
1. Sign off on the 5 sample voice lines (drafted as build step 1).
2. Persistence: **Railway volume + SQLite** (recommended) vs. volume + JSON.
3. Any broadcast frequency ceiling you want enforced to protect goodwill?
