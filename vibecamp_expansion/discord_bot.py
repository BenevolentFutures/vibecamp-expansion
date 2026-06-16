"""Discord bot exposing the Vibe Camp schedule as slash commands.

A thin, read-only client over the project's REST API (see ``api.py``). Every
command fetches from the live API and renders a clean Discord embed. The bot
holds no state of its own and never writes back upstream.

Configuration (environment variables):

- ``DISCORD_BOT_TOKEN`` -- required to connect; never hardcode or commit it.
- ``VIBECAMP_API_BASE`` -- REST API base URL (default: the hosted Railway app).
- ``DISCORD_GUILD_ID`` -- optional; when set, slash commands are synced to that
  guild for instant availability during development (global sync can take up
  to an hour to propagate).

Run with ``vibecamp discord`` (see ``cli.py``).

Terminology: upstream stores the per-event save count as ``bookmarks``; the
my.vibe.camp UI and attendees call these **stars**. This bot shows "stars".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Hosted REST API; overridable via VIBECAMP_API_BASE.
DEFAULT_API_BASE = "https://vibecamp-expansion-production.up.railway.app"

# Discord embed/field limits we defensively respect.
_EMBED_DESCRIPTION_LIMIT = 4096
_FIELD_VALUE_LIMIT = 1024
_MAX_FIELDS = 25

# How many results each list-style command shows.
_LIST_LIMIT = 8

_STAR = "⭐"  # so output reads "5 ⭐" everywhere


# --------------------------------------------------------------------------- #
# API client                                                                  #
# --------------------------------------------------------------------------- #


class VibecampAPI:
    """Async wrapper around the Vibe Camp Expansion REST API."""

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": "vibecamp-discord-bot"},
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def search_events(self, **params: Any) -> list[dict[str, Any]]:
        """Return events from ``GET /events`` with the given query params.

        ``None`` values are dropped so callers can pass optional filters
        uniformly.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.get("/events", params=clean)
        resp.raise_for_status()
        return resp.json().get("events", [])

    async def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        """Return a single event by id, or ``None`` if not found."""
        resp = await self._client.get(f"/events/{event_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def days(self) -> list[dict[str, Any]]:
        """Return the day index from ``GET /days``."""
        resp = await self._client.get("/days")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- #
# Rendering helpers                                                            #
# --------------------------------------------------------------------------- #


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters with an ellipsis if needed."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _event_time(event: dict[str, Any]) -> str:
    """Return ``HH:MM`` from an event's wall-clock start, or ``"??:??"``.

    Timestamps are naive wall-clock (the trailing ``Z`` is an upstream lie),
    so we read the clock time directly without any timezone conversion.
    """
    raw = event.get("start_datetime")
    if not raw or "T" not in raw:
        return "??:??"
    clock = raw.split("T", 1)[1]
    return clock[:5]


def _event_day(event: dict[str, Any]) -> str:
    """Return the calendar day (YYYY-MM-DD) for an event, or ``"?"``."""
    return event.get("start_date") or "?"


def _event_venue(event: dict[str, Any]) -> str:
    """Return the best human-readable venue for an event."""
    return event.get("location") or "TBA"


def _event_stars(event: dict[str, Any]) -> int:
    """Return an event's star count (== bookmarks)."""
    return int(event.get("stars") or 0)


def _format_line(event: dict[str, Any]) -> str:
    """Render one event as a compact one-line summary for list embeds."""
    name = _truncate(event.get("name") or "(untitled)", 120)
    return (
        f"**{name}**\n"
        f"{_event_day(event)} {_event_time(event)} · "
        f"{_event_venue(event)} · {_event_stars(event)} {_STAR}"
    )


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
        value = _truncate(
            f"{_event_day(event)} {_event_time(event)} · "
            f"{_event_venue(event)} · {_event_stars(event)} {_STAR}\n"
            f"`/event id:{eid}`",
            _FIELD_VALUE_LIMIT,
        )
        embed.add_field(
            name=_truncate(event.get("name") or "(untitled)", 256),
            value=value,
            inline=False,
        )
    return embed


def _new_event_embed(event: dict[str, Any]):
    """Build a detailed embed for a single event."""
    import discord

    embed = discord.Embed(
        title=_truncate(event.get("name") or "(untitled)", 256),
        description=_truncate(event.get("description") or "", _EMBED_DESCRIPTION_LIMIT),
    )
    embed.add_field(name="Day", value=_event_day(event), inline=True)
    embed.add_field(name="Time", value=_event_time(event), inline=True)
    embed.add_field(name="Venue", value=_event_venue(event), inline=True)
    embed.add_field(name="Stars", value=f"{_event_stars(event)} {_STAR}", inline=True)
    if event.get("event_type"):
        embed.add_field(name="Type", value=event["event_type"], inline=True)
    if event.get("creator_name"):
        embed.add_field(name="Host", value=event["creator_name"], inline=True)
    if event.get("duration_minutes"):
        embed.add_field(
            name="Duration", value=f"{event['duration_minutes']} min", inline=True
        )
    embed.set_footer(text=f"id: {event.get('event_id', '')}")
    return embed


# --------------------------------------------------------------------------- #
# Recommendation logic                                                        #
# --------------------------------------------------------------------------- #


def _interest_words(interest: str) -> list[str]:
    """Split a free-text interest into distinct lowercased search words.

    Short filler words are dropped so a phrase like "live music and art"
    becomes ``["live", "music", "art"]``. De-duplicates while preserving order.
    """
    stop = {"and", "the", "for", "with", "a", "an", "of", "to", "in", "on", "or"}
    seen: set[str] = set()
    words: list[str] = []
    for token in interest.lower().replace(",", " ").split():
        token = token.strip()
        if len(token) < 3 or token in stop or token in seen:
            continue
        seen.add(token)
        words.append(token)
    # Cap the number of API calls a single recommend triggers.
    return words[:4] or [interest.strip().lower()]


async def _recommend(api: VibecampAPI, interest: str) -> list[dict[str, Any]]:
    """Union events matching each interest word, de-dup, sort by stars desc."""
    by_id: dict[str, dict[str, Any]] = {}
    for word in _interest_words(interest):
        events = await api.search_events(q=word, sort="stars", limit=_LIST_LIMIT)
        for event in events:
            eid = event.get("event_id")
            if eid and eid not in by_id:
                by_id[eid] = event
    ranked = sorted(by_id.values(), key=_event_stars, reverse=True)
    return ranked[:_LIST_LIMIT]


# --------------------------------------------------------------------------- #
# Bot construction                                                            #
# --------------------------------------------------------------------------- #


def build_bot(api: VibecampAPI, *, guild_id: Optional[int] = None):
    """Construct the Discord client and register all slash commands.

    ``guild_id`` (optional) scopes command sync to a single guild for instant
    availability during development.
    """
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    guild = discord.Object(id=guild_id) if guild_id else None

    async def _first_festival_day() -> Optional[str]:
        """Return the earliest day in the current edition, if any."""
        days = await api.days()
        return days[0]["date"] if days else None

    @client.event
    async def on_ready() -> None:
        if guild is not None:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logger.info("Synced commands to guild %s", guild_id)
        else:
            await tree.sync()
            logger.info("Synced global commands")
        logger.info("Logged in as %s", client.user)

    @tree.command(name="events", description="Full-text search the schedule.")
    @app_commands.describe(query="What to search for (name, description, host, venue).")
    async def events_cmd(interaction, query: str) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = await api.search_events(q=query, sort="stars", limit=_LIST_LIMIT)
        embed = _new_list_embed(
            f"Search: {_truncate(query, 200)}",
            results,
            empty="No events matched that search.",
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="pool", description="Events happening at the Pool.")
    async def pool_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = await api.search_events(site="Pool", sort="stars", limit=_LIST_LIMIT)
        embed = _new_list_embed(
            "Pool events", results, empty="Nothing at the Pool right now."
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="shanties", description="Find the sea shanties.")
    async def shanties_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = await api.search_events(q="shanty", sort="stars", limit=_LIST_LIMIT)
        embed = _new_list_embed(
            "Shanties", results, empty="No shanties found... yet."
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="day", description="Events on a given day (YYYY-MM-DD).")
    @app_commands.describe(date="Calendar day as YYYY-MM-DD; defaults to the first day.")
    async def day_cmd(interaction, date: Optional[str] = None) -> None:  # noqa: ANN001
        await interaction.response.defer()
        target = date or await _first_festival_day()
        if not target:
            await interaction.followup.send("No schedule days are available yet.")
            return
        results = await api.search_events(day=target, sort="start", limit=_MAX_FIELDS)
        embed = _new_list_embed(
            f"Schedule for {target}",
            results,
            empty=f"No events scheduled on {target}.",
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="popular", description="Top events by stars.")
    async def popular_cmd(interaction) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = await api.search_events(sort="stars", limit=_LIST_LIMIT)
        embed = _new_list_embed(
            "Most-starred events", results, empty="No events available."
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="recommend", description="Get a curated pick for an interest.")
    @app_commands.describe(interest="Anything you're into, e.g. 'live music and art'.")
    async def recommend_cmd(interaction, interest: str) -> None:  # noqa: ANN001
        await interaction.response.defer()
        results = await _recommend(api, interest)
        embed = _new_list_embed(
            f"Picks for: {_truncate(interest, 200)}",
            results,
            empty="Couldn't find anything matching that. Try a broader interest.",
        )
        if results:
            embed.description = (
                f"Top {len(results)} events for someone into "
                f"“{_truncate(interest, 100)}”, sorted by stars."
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

    api = VibecampAPI(api_base)
    bot = build_bot(api, guild_id=guild_id)

    logger.info("Starting Vibe Camp Discord bot against %s", api_base)
    try:
        bot.run(token, log_handler=None)
    finally:
        import asyncio

        asyncio.run(api.aclose())
    return 0
