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

import html
import logging
import os
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
from .bot_llm import curate

logger = logging.getLogger(__name__)

# Telegram's hard limit on a single message body.
_MESSAGE_LIMIT = 4096

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
    """Render events as a single HTML message, respecting Telegram's limit."""
    if not events:
        return f"<b>{_esc(title)}</b>\n\n{_esc(empty)}"

    lines = [f"<b>{_esc(title)}</b>", ""]
    for event in events:
        name = _esc(truncate(event.get("name") or "(untitled)", 120))
        url = event.get("url")
        heading = f'<a href="{_esc(url)}">{name}</a>' if url else f"<b>{name}</b>"
        lines.append(
            f"{heading}\n"
            f"{event_day(event)} {event_time(event)} · "
            f"{_esc(event_venue(event))} · {event_stars(event)} {STAR}"
        )
        lines.append("")
    return truncate("\n".join(lines), _MESSAGE_LIMIT)


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


def build_app(api: VibecampAPI, token: str):
    """Construct the Telegram ``Application`` and register all handlers.

    ``token`` is the bot token from @BotFather. Import of ``telegram`` is local
    so this module imports without python-telegram-bot installed (e.g. for
    byte-compile checks). The API client is closed on shutdown.
    """
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    async def _post_shutdown(_app) -> None:
        await api.aclose()

    async def _reply(update, text: str) -> None:
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

    async def start_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        await _reply(update, _HELP)

    async def now_cmd(update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        feed = now_feed(await api.search_events(sort="start", limit=_CANDIDATE_POOL))
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
        curated = await curate(api, interest)
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

    app = Application.builder().token(token).post_shutdown(_post_shutdown).build()
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
    app = build_app(api, token)

    logger.info("Starting Vibe Camp Telegram bot against %s", api_base)
    app.run_polling()
    return 0
