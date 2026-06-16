"""Configuration, resolved from environment with sensible defaults."""

from __future__ import annotations

import os
from pathlib import Path

# Upstream Vibe Camp API.
UPSTREAM_BASE_URL = os.environ.get(
    "VIBECAMP_UPSTREAM_BASE_URL",
    "https://backend-2-6ri5.onrender.com/api/v1",
).rstrip("/")

EVENTS_ENDPOINT = f"{UPSTREAM_BASE_URL}/events"
ICS_ENDPOINT = f"{UPSTREAM_BASE_URL}/events.ics"

# Where the SQLite cache lives.
DATA_DIR = Path(
    os.environ.get(
        "VIBECAMP_DATA_DIR",
        str(Path.home() / ".vibecamp-expansion"),
    )
).expanduser()

DB_PATH = Path(os.environ.get("VIBECAMP_DB_PATH", str(DATA_DIR / "vibecamp.db"))).expanduser()

# Crawl cadence (seconds) for the built-in loop runner.
CRAWL_INTERVAL_SECONDS = int(os.environ.get("VIBECAMP_CRAWL_INTERVAL", "300"))

# HTTP timeouts. The upstream runs on a Render free tier that cold-starts,
# so the read timeout is generous.
HTTP_CONNECT_TIMEOUT = float(os.environ.get("VIBECAMP_HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.environ.get("VIBECAMP_HTTP_READ_TIMEOUT", "90"))

# Events whose start year falls outside this window are flagged as
# placeholder/joke entries (the live data contains years like 1999 and 3025).
REAL_YEAR_MIN = int(os.environ.get("VIBECAMP_REAL_YEAR_MIN", "2020"))
REAL_YEAR_MAX = int(os.environ.get("VIBECAMP_REAL_YEAR_MAX", "2030"))

# The "current edition" — what people actually care about. The feed contains
# every past edition's events too; by default the API/MCP surface only the
# current edition, but historical events stay in the cache and are reachable
# with include_historical=true. Vibe Camp 5 = 2026.
CURRENT_EDITION_NAME = os.environ.get("VIBECAMP_EDITION_NAME", "Vibe Camp 5")
# Half-open window [start, end) compared against the YYYY-MM-DD start_date.
CURRENT_EDITION_START = os.environ.get("VIBECAMP_EDITION_START", "2026-01-01")
CURRENT_EDITION_END = os.environ.get("VIBECAMP_EDITION_END", "2027-01-01")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
