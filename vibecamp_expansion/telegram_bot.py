"""Telegram bot exposing the Vibe Camp schedule for one-on-one chat.

A thin, read-only client over the project's REST API (see ``api.py``), sharing
its HTTP client and field helpers with the Discord bot (see ``bot_api.py``).
It supports the same set of commands and, crucially, treats any plain-text
direct message as a recommendation query -- so a person can just DM the bot
"live music and art" and get a curated list back.

Configuration (environment variables):

- ``TELEGRAM_BOT_TOKEN`` -- required to connect; never hardcode or commit it.
- ``VIBECAMP_API_BASE`` -- REST API base URL (default: the hosted Railway app).

Run with ``vibecamp telegram`` (see ``cli.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import time
from typing import Any

from .bot_api import (
    DEFAULT_API_BASE,
    LIST_LIMIT,
    STAR,
    VibecampAPI,
    event_day,
    event_stars,
    event_time,
    event_venue,
    future_filter,
    now_feed,
    truncate,
)
from . import bot_llm
from .analytics import Analytics
from .bot_llm import curate
from .ratelimit import (
    ExponentialLockout,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)

# Usage analytics. Counts are in-memory by default (reset on redeploy) but
# persist across restarts if VIBECAMP_ANALYTICS_PATH points at durable storage.
_ANALYTICS_PATH = os.environ.get("VIBECAMP_ANALYTICS_PATH")
# Log a summary + persist every this many messages, so growth is visible in the
# deploy logs even without anyone running /stats.
_ANALYTICS_FLUSH_EVERY = 25
# Admin allow-list for /stats. Configure by username (TELEGRAM_ADMIN_USERNAMES,
# comma-separated, with or without a leading @) and/or numeric chat id
# (TELEGRAM_ADMIN_IDS). If neither is set, /stats is open to anyone (it's cheap
# and undocumented). If either is set, /stats only answers listed admins.
_ADMIN_IDS = {
    s.strip() for s in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()
}
_ADMIN_USERNAMES = {
    s.strip().lstrip("@").lower()
    for s in os.environ.get("TELEGRAM_ADMIN_USERNAMES", "").split(",")
    if s.strip()
}


def _user_key(chat_id: int) -> str:
    """A stable, opaque hash of a chat id — for counting unique users without
    ever storing the raw id (no PII)."""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]

logger = logging.getLogger(__name__)

# Per-chat burst limit (token bucket): a fresh chat may fire off
# ``_RATE_LIMIT_BURST`` messages right away, after which it's paced to
# ``_RATE_LIMIT_PER_MINUTE`` sustained — i.e. "a few up front, then a steady
# trickle." Overridable via env in ``run()``. The limit protects the bot (and
# the paid Anthropic LLM call behind each free-text reply) from a single
# spammer.
_RATE_LIMIT_BURST = int(os.environ.get("TELEGRAM_RATE_LIMIT_BURST", "3"))
_RATE_LIMIT_PER_MINUTE = float(
    os.environ.get("TELEGRAM_RATE_LIMIT_PER_MINUTE", "2")
)

# Second tier: a per-user daily cap (rolling 24h) on top of the burst limit
# above, to bound a single user's total cost over a day. Configurable max; the
# window is fixed at one day.
_RATE_LIMIT_DAILY_MAX = int(os.environ.get("TELEGRAM_RATE_LIMIT_DAILY_MAX", "50"))
_RATE_LIMIT_DAILY_WINDOW_SECONDS = 86_400.0

# Exponential lockout: when a chat blows past the burst limit it's put in a
# penalty box, and each fresh overflow doubles the timeout (60s -> 2m -> 4m …)
# up to the cap, so repeat offenders are locked out longer and longer. The
# escalation resets once the chat behaves for ``_LOCKOUT_RESET_SECONDS`` past
# the end of its last lockout. All overridable via env in ``run()``.
_LOCKOUT_BASE_SECONDS = float(os.environ.get("TELEGRAM_LOCKOUT_BASE_SECONDS", "60"))
_LOCKOUT_MAX_SECONDS = float(os.environ.get("TELEGRAM_LOCKOUT_MAX_SECONDS", "900"))
_LOCKOUT_RESET_SECONDS = float(
    os.environ.get("TELEGRAM_LOCKOUT_RESET_SECONDS", "3600")
)

# Once a chat is over the *daily* cap it may keep firing; don't re-send the
# daily notice on every blocked update. Send it at most once per this cooldown.
# (The burst lockout throttles its own notice — see the gate below.)
_DAILY_NOTICE_COOLDOWN_SECONDS = 60.0

_RATE_LIMITED_MESSAGE = (
    "🏕️ Whoa, slow down a sec — you're asking faster than I can keep up! "
    "Give me about {wait} and ask again, and I'll be right here."
)

_RATE_LIMITED_DAILY_MESSAGE = (
    "🏕️ You've reached your daily limit of about {max} questions. It resets "
    "over the next day — check back later! Meanwhile the full schedule is at "
    "my.vibe.camp."
)

# Telegram's hard limit on a single message body.
_MESSAGE_LIMIT = 4096


def _humanize_seconds(seconds: float) -> str:
    """Render a lockout duration as a friendly "N minute(s)" string.

    Rounds to the nearest minute with a floor of one, so a sub-minute lockout
    still reads as "1 minute" rather than "0 minutes."
    """
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"

# Pull the whole edition (it's small) so "most popular" ranks by stars *after*
# dropping events that are already over, rather than letting the API's star-sort
# surface a finished crowd-favourite.
_CANDIDATE_POOL = 250

_HELP = (
    "🏕️ <b>Vibe Camp schedule bot</b>\n"
    "Two great things to ask me:\n\n"
    "⭐ <b>“what's coming up soon?”</b> — what's on right now &amp; next\n"
    "⭐ <b>“what's good for someone into ___?”</b> — picks for your interest\n\n"
    "You can also just say a day (Thursday–Sunday), a vibe, or a place "
    "and I'll find events. Commands:\n\n"
    "/now — what's happening right now\n"
    "/events &lt;text&gt; — search the schedule\n"
    "/pool — events at the Pool\n"
    "/shanties — find the sea shanties\n"
    "/day &lt;Thu–Sun&gt; — a day's schedule\n"
    "/popular — top events by stars\n"
    "/recommend &lt;interest&gt; — curated picks\n"
    "/event &lt;id&gt; — full detail for one event\n"
)


# --------------------------------------------------------------------------- #
# Rendering helpers (Telegram HTML parse mode)                                #
# --------------------------------------------------------------------------- #


def _esc(text: str) -> str:
    """HTML-escape free text for Telegram's HTML parse mode."""
    return html.escape(text, quote=False)


def _render_list(title: str, events: list[dict[str, Any]], *, empty: str) -> str:
    """Render events as a single HTML message, respecting Telegram's limit.

    Adds events until the next one wouldn't fit in Telegram's 4096-char body,
    then appends an "…and N more" note — so a long result set never silently
    looks complete when it was actually cut off.
    """
    if not events:
        return f"<b>{_esc(title)}</b>\n\n{_esc(empty)}"

    head = f"<b>{_esc(title)}</b>\n"
    blocks: list[str] = []
    length = len(head)
    # Reserve room for the overflow footer so adding it never busts the limit.
    budget = _MESSAGE_LIMIT - 80
    for i, event in enumerate(events):
        name = _esc(truncate(event.get("name") or "(untitled)", 120))
        url = event.get("url")
        heading = f'<a href="{_esc(url)}">{name}</a>' if url else f"<b>{name}</b>"
        block = (
            f"\n{heading}\n"
            f"{event_day(event)} {event_time(event)} · "
            f"{_esc(event_venue(event))} · {event_stars(event)} {STAR}\n"
        )
        if length + len(block) > budget and blocks:
            remaining = len(events) - i
            blocks.append(f"\n<i>…and {remaining} more — narrow your search to see them.</i>")
            break
        blocks.append(block)
        length += len(block)
    return truncate(head + "".join(blocks), _MESSAGE_LIMIT)


def _render_event(event: dict[str, Any]) -> str:
    """Render full detail for one event as an HTML message."""
    name = _esc(truncate(event.get("name") or "(untitled)", 200))
    parts = [
        f"<b>{name}</b>",
        f"{event_day(event)} {event_time(event)} · "
        f"{_esc(event_venue(event))} · {event_stars(event)} {STAR}",
    ]
    if event.get("creator_name"):
        parts.append(f"Host: {_esc(event['creator_name'])}")
    if event.get("duration_minutes"):
        parts.append(f"Duration: {event['duration_minutes']} min")
    description = (event.get("description") or "").strip()
    if description:
        parts.append("")
        parts.append(_esc(description))
    if event.get("url"):
        parts.append("")
        parts.append(f'<a href="{_esc(event["url"])}">⭐ Star / RSVP in the app →</a>')
    return truncate("\n".join(parts), _MESSAGE_LIMIT)


# --------------------------------------------------------------------------- #
# Bot construction                                                            #
# --------------------------------------------------------------------------- #


def build_app(
    api: VibecampAPI,
    token: str,
    *,
    rate_limit_burst: int = _RATE_LIMIT_BURST,
    rate_limit_per_minute: float = _RATE_LIMIT_PER_MINUTE,
    rate_limit_daily_max: int = _RATE_LIMIT_DAILY_MAX,
    rate_limit_daily_window_seconds: float = _RATE_LIMIT_DAILY_WINDOW_SECONDS,
    lockout_base_seconds: float = _LOCKOUT_BASE_SECONDS,
    lockout_max_seconds: float = _LOCKOUT_MAX_SECONDS,
    lockout_reset_seconds: float = _LOCKOUT_RESET_SECONDS,
    analytics_path: "str | None" = _ANALYTICS_PATH,
):
    """Construct the Telegram ``Application`` and register all handlers.

    ``token`` is the bot token from @BotFather. Import of ``telegram`` is local
    so this module imports without python-telegram-bot installed (e.g. for
    byte-compile checks). The API client is closed on shutdown.

    ``rate_limit_burst`` / ``rate_limit_per_minute`` configure the per-chat
    burst token bucket (default 3 burst, 2/min sustained). A chat that exceeds
    it is put in an exponential lockout (``lockout_*`` params); ``rate_limit_
    daily_*`` is the per-chat daily backstop. See the pre-check handler below.
    """
    from telegram import Update
    from telegram.constants import ChatAction
    from telegram.ext import (
        Application,
        ApplicationHandlerStop,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        TypeHandler,
        filters,
    )

    # In-memory, per-chat limiters (fine for single-instance polling). When a
    # chat is over a limit we stop the update from reaching any real handler —
    # so the paid LLM call never runs for a spammer. The burst tier is a token
    # bucket (a few messages up front, then a steady trickle); blowing past it
    # arms an exponential lockout. The daily tier is a 24h backstop.
    limiter = TokenBucketRateLimiter(rate_limit_burst, rate_limit_per_minute)
    daily_limiter = SlidingWindowRateLimiter(
        rate_limit_daily_max, rate_limit_daily_window_seconds
    )
    lockout = ExponentialLockout(
        lockout_base_seconds, lockout_max_seconds, lockout_reset_seconds
    )
    last_notice: dict[int, float] = {}

    # Usage analytics — unique users + message counts. Loaded from disk if a
    # durable path is configured, else fresh in-memory.
    analytics = Analytics.load(analytics_path) if analytics_path else Analytics()

    def _flush_analytics() -> None:
        """Log a summary and persist (if configured). Cheap; safe to call often."""
        s = analytics.summary()
        logger.info(
            "analytics: users=%d messages=%d rate_limited=%d by_kind=%s",
            s["unique_users"], s["total_messages"], s["rate_limited"], s["by_kind"],
        )
        if analytics_path:
            analytics.save(analytics_path)

    async def _post_shutdown(_app) -> None:
        await api.aclose()

    async def _reply(update, text: str) -> None:
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

    async def _with_typing(update, coro):
        """Await ``coro`` while showing Telegram's "typing…" bubble.

        The chat action expires after ~5s, so we re-send it on a short loop until
        the work (often a multi-second LLM call) finishes.
        """
        chat = update.effective_chat

        async def _pulse() -> None:
            try:
                while True:
                    await chat.send_action(ChatAction.TYPING)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass

        pulse = asyncio.create_task(_pulse())
        try:
            return await coro
        finally:
            pulse.cancel()

    async def start_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        await _reply(update, _HELP)

    async def now_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        feed = now_feed(await _with_typing(update, api.search_events(sort="start", limit=_CANDIDATE_POOL)))
        title = (
            "Happening now" if feed["live"]
            else "Nothing this minute — coming up next"
        )
        await _reply(
            update,
            _render_list(title, feed["events"], empty="Nothing on the schedule right now."),
        )

    async def events_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = " ".join(context.args).strip()
        if not query:
            await _reply(update, "Usage: /events &lt;search text&gt;")
            return
        results = await api.search_events(q=query, sort="stars", limit=LIST_LIMIT)
        await _reply(
            update,
            _render_list(f"Search: {query}", results, empty="No events matched that search."),
        )

    async def pool_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        results = future_filter(await api.search_events(site="Pool", sort="start", limit=25))
        await _reply(
            update,
            _render_list("Pool events", results[:LIST_LIMIT], empty="Nothing coming up at the Pool."),
        )

    async def shanties_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        results = future_filter(await api.search_events(q="shanty", sort="start", limit=25))
        await _reply(
            update,
            _render_list("Shanties", results[:LIST_LIMIT], empty="No shanties coming up... yet."),
        )

    async def day_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        ref = context.args[0] if context.args else None
        target = await api.resolve_day(ref)
        if not target:
            await _reply(
                update,
                "I didn't recognise that day. Try a weekday, e.g. /day Friday "
                "(camp runs Thursday–Sunday).",
            )
            return
        results = await api.search_events(day=target, sort="start", limit=25)
        label = event_day({"start_date": target})  # weekday name for the title
        await _reply(
            update,
            _render_list(
                f"Schedule for {label}", results, empty=f"No events scheduled on {label}."
            ),
        )

    async def popular_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        results = future_filter(await api.search_events(sort="start", limit=_CANDIDATE_POOL))
        results.sort(key=event_stars, reverse=True)
        await _reply(
            update,
            _render_list(
                "Most-starred upcoming events", results[:LIST_LIMIT], empty="No events available."
            ),
        )

    async def recommend_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        interest = " ".join(context.args).strip()
        if not interest:
            await _reply(update, "Usage: /recommend &lt;what you're into&gt;")
            return
        await _recommend_reply(update, interest)

    async def event_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not context.args:
            await _reply(update, "Usage: /event &lt;event_id&gt;")
            return
        event = await api.get_event(context.args[0].strip())
        if event is None:
            await _reply(update, "No event found with that id.")
            return
        await _reply(update, _render_event(event))

    async def text_message(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        """Treat any plain-text DM as a recommendation query."""
        interest = (update.message.text or "").strip()
        if not interest:
            await _reply(update, _HELP)
            return
        await _recommend_reply(update, interest)

    async def _recommend_reply(update, interest: str) -> None:
        # The concierge call takes a few seconds (it now thinks) — show "typing…".
        curated = await _with_typing(update, curate(api, interest))
        # Attribute the LLM call's estimated cost to this user for /stats.
        if update.effective_chat is not None:
            analytics.record_cost(_user_key(update.effective_chat.id), curated.get("cost", 0.0))
        results = curated["events"]
        if not results:
            await _reply(
                update,
                "Couldn't find anything matching that. Try a broader interest, "
                "or /help for commands.",
            )
            return
        title = curated["framing"] or f"Picks for: {truncate(interest, 100)}"
        await _reply(update, _render_list(title, results, empty=""))

    def _is_admin(update) -> bool:
        """True if the requester may use admin commands (/stats, /model).

        Open to everyone when no allow-list is configured; otherwise restricted
        to the configured admin usernames / chat ids.
        """
        if not (_ADMIN_IDS or _ADMIN_USERNAMES):
            return True
        chat = update.effective_chat
        user = update.effective_user
        uname = (user.username or "").lower() if user else ""
        return (chat is not None and str(chat.id) in _ADMIN_IDS) or (
            uname != "" and uname in _ADMIN_USERNAMES
        )

    async def stats_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        """Report usage analytics (admin only when an allow-list is configured)."""
        if not _is_admin(update):
            await _reply(update, _HELP)  # not an admin — treat like an unknown cmd
            return
        s = analytics.summary()
        lines = [
            "📊 <b>Bot usage</b>",
            f"Unique users: <b>{s['unique_users']}</b>",
            f"Total messages: <b>{s['total_messages']}</b> "
            f"(avg {s['avg_queries_per_user']}/user)",
            f"Est. spend: <b>${s['total_cost']:.2f}</b>",
            f"Rate-limited: {s['rate_limited']}",
            f"Model: {_esc(bot_llm.MODEL)}",
            f"Uptime: {s['uptime_seconds'] / 3600:.1f}h",
        ]
        if s["by_kind"]:
            top = ", ".join(f"{k} {v}" for k, v in list(s["by_kind"].items())[:6])
            lines.append(f"By type: {_esc(top)}")
        if s["top_users"]:
            lines.append("Top users (id · queries · spend):")
            for u in s["top_users"]:
                lines.append(f"  {u['user'][:8]} · {u['queries']} · ${u['cost']:.2f}")
        if not analytics_path:
            lines.append("<i>(counts reset on redeploy — no durable store configured)</i>")
        await _reply(update, "\n".join(lines))

    async def model_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        """Switch the concierge model at runtime (admin only). e.g. /model haiku."""
        if not _is_admin(update):
            await _reply(update, _HELP)
            return
        arg = (context.args[0] if context.args else "").strip()
        if not arg:
            await _reply(
                update,
                f"Current model: <b>{_esc(bot_llm.MODEL)}</b>\n"
                "Switch with /model haiku · /model sonnet · /model opus.",
            )
            return
        try:
            resolved = bot_llm.set_model(arg)
        except ValueError:
            await _reply(update, "Unknown model. Try: /model haiku, /model sonnet, or /model opus.")
            return
        logger.info("Concierge model switched to %s", resolved)
        await _reply(
            update,
            f"✅ Concierge model set to <b>{_esc(resolved)}</b>. "
            "(Reverts to the default on redeploy.)",
        )

    async def _send_notice(update, context: "ContextTypes.DEFAULT_TYPE", text: str) -> None:
        """Best-effort reply for a rate-limit notice; never crashes the gate."""
        message = update.effective_message
        try:
            if message is not None:
                await message.reply_text(
                    text, parse_mode="HTML", disable_web_page_preview=True
                )
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
        except Exception:  # noqa: BLE001 — never let a notice failure crash the gate
            logger.warning("Failed to send rate-limit notice", exc_info=True)

    async def _rate_limit_gate(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        """Pre-check every update against the per-chat rate limit.

        Registered in a lower group so it runs before any command/message
        handler. If the chat is over a limit we raise ``ApplicationHandlerStop``
        so the update never reaches a real handler — crucially skipping the paid
        LLM call. The flow, in order:

        1. Already in an exponential lockout -> drop silently (we already told
           them, and the lockout doubles on the next *fresh* overflow, not on
           every blocked message).
        2. Out of burst tokens -> arm/escalate the lockout and send one friendly
           "slow down for ~N min" notice.
        3. Over the daily cap -> send the daily notice (throttled by cooldown).
        4. Otherwise -> spend a token + a daily slot and proceed.
        """
        chat = update.effective_chat
        if chat is None:
            return  # nothing to key on; let normal handling proceed
        now = time.time()
        key = str(chat.id)
        user = _user_key(chat.id)

        # 1. Inside an active lockout: count it, but stay quiet.
        if lockout.is_locked(key, now):
            analytics.record_rate_limited(user)
            raise ApplicationHandlerStop

        # 2. Burst tier (peeked, not spent, so a block here doesn't consume the
        #    daily allowance). Out of tokens -> escalating lockout.
        if not limiter.would_allow(key, now):
            analytics.record_rate_limited(user)
            newly_armed, seconds = lockout.register(key, now)
            if newly_armed:
                text = _RATE_LIMITED_MESSAGE.format(wait=_humanize_seconds(seconds))
                await _send_notice(update, context, text)
            raise ApplicationHandlerStop

        # 3. Daily backstop. Throttle the notice so a chat that's over its daily
        #    cap (a 24h window) isn't reminded on every single message.
        if not daily_limiter.would_allow(key, now):
            analytics.record_rate_limited(user)
            last = last_notice.get(chat.id)
            if last is None or now - last >= _DAILY_NOTICE_COOLDOWN_SECONDS:
                last_notice[chat.id] = now
                text = _RATE_LIMITED_DAILY_MESSAGE.format(max=rate_limit_daily_max)
                await _send_notice(update, context, text)
            raise ApplicationHandlerStop

        # 4. Under both limits — spend against both and proceed.
        limiter.allow(key, now)
        daily_limiter.allow(key, now)
        # Analytics: classify the request and tally it.
        raw = (update.effective_message.text or "") if update.effective_message else ""
        kind = raw[1:].split()[0].lower() if raw.startswith("/") else "text"
        is_new = analytics.record(user, kind or "text")
        if is_new or analytics.total % _ANALYTICS_FLUSH_EVERY == 0:
            _flush_analytics()

    async def _on_error(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        # One bad update must not take the bot down or spam unhandled tracebacks.
        logger.error("Error handling update", exc_info=context.error)

    # concurrent_updates lets many users be served in parallel — essential now
    # that each free-text reply makes a multi-second LLM call. Without it,
    # python-telegram-bot processes updates one at a time and users queue behind
    # each other's concierge calls.
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_error_handler(_on_error)
    # Rate-limit gate runs before everything else (lower group). One chokepoint
    # covers free-text DMs and every command at once.
    app.add_handler(TypeHandler(Update, _rate_limit_gate), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("now", now_cmd))
    app.add_handler(CommandHandler("events", events_cmd))
    app.add_handler(CommandHandler("pool", pool_cmd))
    app.add_handler(CommandHandler("shanties", shanties_cmd))
    app.add_handler(CommandHandler("day", day_cmd))
    app.add_handler(CommandHandler("popular", popular_cmd))
    app.add_handler(CommandHandler("recommend", recommend_cmd))
    app.add_handler(CommandHandler("event", event_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))  # usage analytics (undocumented)
    app.add_handler(CommandHandler("model", model_cmd))  # admin: switch concierge model
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return app


def run() -> int:
    """Entry point for ``vibecamp telegram``: build and run the bot.

    Reads configuration from the environment and blocks on long-polling.
    Returns a non-zero exit code on configuration errors.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather on "
            "Telegram and export the token it gives you."
        )
        return 1

    # python-telegram-bot / httpx log the full getUpdates URL — which embeds the
    # bot token — at INFO. Quiet them to WARNING so the token never lands in logs.
    for noisy in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    api_base = os.environ.get("VIBECAMP_API_BASE", DEFAULT_API_BASE)
    api = VibecampAPI(api_base)
    app = build_app(
        api,
        token,
        rate_limit_burst=_RATE_LIMIT_BURST,
        rate_limit_per_minute=_RATE_LIMIT_PER_MINUTE,
        rate_limit_daily_max=_RATE_LIMIT_DAILY_MAX,
        rate_limit_daily_window_seconds=_RATE_LIMIT_DAILY_WINDOW_SECONDS,
        lockout_base_seconds=_LOCKOUT_BASE_SECONDS,
        lockout_max_seconds=_LOCKOUT_MAX_SECONDS,
        lockout_reset_seconds=_LOCKOUT_RESET_SECONDS,
        analytics_path=_ANALYTICS_PATH,
    )

    logger.info(
        "Starting Vibe Camp Telegram bot against %s (rate limit: %d burst + "
        "%g/min sustained, lockout %gs→%gs doubling, %d msgs / day per chat)",
        api_base,
        _RATE_LIMIT_BURST,
        _RATE_LIMIT_PER_MINUTE,
        _LOCKOUT_BASE_SECONDS,
        _LOCKOUT_MAX_SECONDS,
        _RATE_LIMIT_DAILY_MAX,
    )
    app.run_polling()
    return 0
