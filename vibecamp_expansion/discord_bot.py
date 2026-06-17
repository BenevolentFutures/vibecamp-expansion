"""Discord bot exposing the Vibe Camp schedule as slash commands.

A thin, read-only client over the project's REST API (see ``api.py``). Every
command fetches from the live API and renders a clean Discord embed. The bot
holds no state of its own and never writes back upstream. The HTTP client and
field helpers are shared with the Telegram bot (see ``bot_api.py``).

Configuration (environment variables):

- ``DISCORD_BOT_TOKEN`` -- required to connect; never hardcode or commit it.
- ``VIBECAMP_API_BASE`` -- REST API base URL (default: the hosted Railway app).
- ``DISCORD_GUILD_ID`` -- optional; when set, slash commands are synced to that
  guild for instant availability during development (global sync can take up
  to an hour to propagate).

In a server the bot is slash-command only. In a **direct message** it also
answers plain text through the same LLM concierge as the Telegram bot — so you
can just DM it "what's happening now?". That requires the Message Content
privileged intent (enable it in the Developer Portal); without it the bot runs
slash-only and never crashes.

Run with ``vibecamp discord`` (see ``cli.py``).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

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

# Pull the whole edition (it's small) so "most popular" ranks by stars *after*
# dropping events that are already over.
_CANDIDATE_POOL = 250

logger = logging.getLogger(__name__)

# Discord embed/field limits we defensively respect.
_EMBED_DESCRIPTION_LIMIT = 4096
_FIELD_VALUE_LIMIT = 1024
_MAX_FIELDS = 25


# --------------------------------------------------------------------------- #
# Rendering helpers                                                           #
# --------------------------------------------------------------------------- #


def _new_list_embed(title: str, events: list[dict[str, Any]], *, empty: str):
    """Build a Discord embed listing events, one field per event.

    Returns a ``discord.Embed``. Import is local so the module imports without
    discord.py installed (e.g. for byte-compile checks).
    """
    import discord

    if not events:
        return discord.Embed(title=title, description=empty)

    embed = discord.Embed(title=title)
    for event in events[:_MAX_FIELDS]:
        eid = event.get("event_id", "")
        url = event.get("url")
        action = f"[⭐ Star / RSVP →]({url})" if url else f"`/event id:{eid}`"
        value = truncate(
            f"{event_day(event)} {event_time(event)} · "
            f"{event_venue(event)} · {event_stars(event)} {STAR}\n"
            f"{action}",
            _FIELD_VALUE_LIMIT,
        )
        embed.add_field(
            name=truncate(event.get("name") or "(untitled)", 256),
            value=value,
            inline=False,
        )
    return embed


def _new_event_embed(event: dict[str, Any]):
    """Build a detailed embed for a single event."""
    import discord

    embed = discord.Embed(
        title=truncate(event.get("name") or "(untitled)", 256),
        description=truncate(event.get("description") or "", _EMBED_DESCRIPTION_LIMIT),
        url=event.get("url") or None,  # makes the title link into my.vibe.camp
    )
    embed.add_field(name="Day", value=event_day(event), inline=True)
    embed.add_field(name="Time", value=event_time(event), inline=True)
    embed.add_field(name="Venue", value=event_venue(event), inline=True)
    embed.add_field(name="Stars", value=f"{event_stars(event)} {STAR}", inline=True)
    if event.get("event_type"):
        embed.add_field(name="Type", value=event["event_type"], inline=True)
    if event.get("creator_name"):
        embed.add_field(name="Host", value=event["creator_name"], inline=True)
    if event.get("duration_minutes"):
        embed.add_field(
            name="Duration", value=f"{event['duration_minutes']} min", inline=True
        )
    if event.get("url"):
        embed.add_field(
            name="Star / RSVP",
            value=f"[Open in my.vibe.camp →]({event['url']})",
            inline=False,
        )
    embed.set_footer(text=f"id: {event.get('event_id', '')}")
    return embed


# --------------------------------------------------------------------------- #
# Bot construction                                                            #
# --------------------------------------------------------------------------- #


def build_bot(
    api: VibecampAPI,
    *,
    guild_id: Optional[int] = None,
    enable_message_content: bool = True,
):
    """Construct the Discord client and register all slash commands.

    ``guild_id`` (optional) scopes command sync to a single guild for instant
    availability during development. ``enable_message_content`` requests the
    Message Content privileged intent so the bot can read plain-text DMs and
    answer them like the Telegram bot does; set it False to run slash-only
    (the fallback when the intent isn't enabled in the Developer Portal).
    """
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True
    if enable_message_content:
        # DM events + their text, so a person can just message the bot. We never
        # read guild message content (no `guild_messages`) — in servers the bot
        # is slash-command only, which keeps it non-invasive and unprivileged.
        intents.dm_messages = True
        intents.message_content = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    # Expose the command tree on the client for sync/introspection.
    client.tree = tree
    guild = discord.Object(id=guild_id) if guild_id else None

    @client.event
    async def on_ready() -> None:
        if guild is not None:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logger.info("Synced commands to guild %s", guild_id)
        else:
            await tree.sync()
            logger.info("Synced global commands")
        logger.info(
            "Logged in as %s (dm_replies=%s)", client.user, enable_message_content
        )

    @client.event
    async def on_message(message) -> None:  # noqa: ANN001
        """Answer plain-text direct messages via the same concierge as Telegram.

        DMs only — guild messages are ignored (use slash commands there). This
        is a no-op unless the Message Content intent is enabled, since
        ``message.content`` is otherwise empty.
        """
        if message.author.bot or message.guild is not None:
            return
        content = (message.content or "").strip()
        if not content or content.startswith("/"):
            return
        async with message.channel.typing():
            curated = await curate(api, content)
        results = curated["events"]
        if not results:
            await message.channel.send(
                "Couldn't find anything matching that. Try a broader interest, "
                "or a day, a venue, or `/help`."
            )
            return
        title = curated["framing"] or f"Picks for: {truncate(content, 200)}"
        await message.channel.send(embed=_new_list_embed(title, results, empty=""))

    @tree.command(name="now", description="What's happening right now (and starting soon).")
    async def now_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        feed = now_feed(await api.search_events(sort="start", limit=_CANDIDATE_POOL))
        if feed["live"]:
            title, empty = "Happening now", ""
        else:
            title = "Nothing this minute — coming up next"
            empty = "Nothing on the schedule right now."
        await interaction.followup.send(
            embed=_new_list_embed(title, feed["events"], empty=empty)
        )

    @tree.command(name="events", description="Full-text search the schedule.")
    @app_commands.describe(query="What to search for (name, description, host, venue).")
    async def events_cmd(interaction, query: str) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = await api.search_events(q=query, sort="stars", limit=LIST_LIMIT)
        embed = _new_list_embed(
            f"Search: {truncate(query, 200)}",
            results,
            empty="No events matched that search.",
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="pool", description="Upcoming events at the Pool.")
    async def pool_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = future_filter(await api.search_events(site="Pool", sort="start", limit=_MAX_FIELDS))
        embed = _new_list_embed(
            "Pool events", results[:LIST_LIMIT], empty="Nothing coming up at the Pool."
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="shanties", description="Find the sea shanties.")
    async def shanties_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = future_filter(await api.search_events(q="shanty", sort="start", limit=_MAX_FIELDS))
        embed = _new_list_embed("Shanties", results[:LIST_LIMIT], empty="No shanties coming up... yet.")
        await interaction.followup.send(embed=embed)

    @tree.command(name="day", description="Events on a given day (e.g. Friday).")
    @app_commands.describe(date="Weekday (Thursday–Sunday) or YYYY-MM-DD; defaults to the first day.")
    async def day_cmd(interaction, date: Optional[str] = None) -> None:  # noqa: ANN001
        await interaction.response.defer()
        target = await api.resolve_day(date)
        if not target:
            await interaction.followup.send(
                "I didn't recognise that day. Try a weekday, e.g. Friday "
                "(camp runs Thursday–Sunday)."
            )
            return
        results = await api.search_events(day=target, sort="start", limit=_MAX_FIELDS)
        label = event_day({"start_date": target})
        embed = _new_list_embed(
            f"Schedule for {label}",
            results,
            empty=f"No events scheduled on {label}.",
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="popular", description="Top upcoming events by stars.")
    async def popular_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = future_filter(await api.search_events(sort="start", limit=_CANDIDATE_POOL))
        results.sort(key=event_stars, reverse=True)
        embed = _new_list_embed(
            "Most-starred upcoming events", results[:LIST_LIMIT], empty="No events available."
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="recommend", description="Get a curated pick for an interest.")
    @app_commands.describe(interest="Anything you're into, e.g. 'live music and art'.")
    async def recommend_cmd(interaction, interest: str) -> None:  # noqa: ANN001
        await interaction.response.defer()
        curated = await curate(api, interest)
        results = curated["events"]
        embed = _new_list_embed(
            f"Picks for: {truncate(interest, 200)}",
            results,
            empty="Couldn't find anything matching that. Try a broader interest.",
        )
        if results:
            embed.description = curated["framing"] or (
                f"Top {len(results)} events for someone into "
                f"“{truncate(interest, 100)}”."
            )
        await interaction.followup.send(embed=embed)

    @tree.command(name="event", description="Full detail for one event by id.")
    @app_commands.describe(id="The event_id (shown in list results).")
    async def event_cmd(interaction, id: str) -> None:  # noqa: ANN001,A002
        await interaction.response.defer()
        event = await api.get_event(id.strip())
        if event is None:
            await interaction.followup.send(f"No event found with id `{id}`.")
            return
        await interaction.followup.send(embed=_new_event_embed(event))

    return client


def run() -> int:
    """Entry point for ``vibecamp discord``: build and run the bot.

    Reads configuration from the environment and blocks on the Discord
    connection. Returns a non-zero exit code on configuration errors.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        logger.error(
            "DISCORD_BOT_TOKEN is not set. Create a bot application at "
            "https://discord.com/developers/applications and export its token."
        )
        return 1

    api_base = os.environ.get("VIBECAMP_API_BASE", DEFAULT_API_BASE)
    guild_raw = os.environ.get("DISCORD_GUILD_ID")
    guild_id = int(guild_raw) if guild_raw and guild_raw.isdigit() else None

    # Quiet HTTP-client/library loggers to WARNING so request URLs (which can
    # carry the bot token) never land in logs at INFO.
    for noisy in ("httpx", "httpcore", "discord", "discord.http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    import asyncio

    import discord

    # Prefer full DM free-text support; if the Message Content intent isn't
    # enabled in the Developer Portal yet, Discord rejects the connection — so
    # fall back to slash-only rather than crash-loop. A restart picks up DM
    # support automatically once the portal toggle is on.
    for enable_mc in (True, False):
        api = VibecampAPI(api_base)
        bot = build_bot(api, guild_id=guild_id, enable_message_content=enable_mc)
        logger.info(
            "Starting Vibe Camp Discord bot (dm_replies=%s) against %s",
            enable_mc, api_base,
        )
        try:
            bot.run(token, log_handler=None)
            return 0  # clean shutdown
        except discord.errors.PrivilegedIntentsRequired:
            logger.error(
                "Message Content intent is not enabled for this bot, so DM "
                "free-text replies are OFF. Enable it at "
                "https://discord.com/developers/applications -> your app -> Bot "
                "-> Privileged Gateway Intents -> Message Content, then restart. "
                "Falling back to slash-commands-only for now."
            )
        finally:
            asyncio.run(api.aclose())
    return 0
