"""Generate static, grep-friendly export files from the cache.

The most agent-native interface: an agent can fetch one file and grep/jq the
whole schedule locally — no API calls, no auth, no rate limits. Regenerated on
every successful crawl. Current-edition variants are the primary files; `.all.`
variants carry every edition.

Files written to the export dir:
  index.json        manifest: generated_at, edition, counts, file list
  llms.txt          plain-text guide for agents (entry point)
  events.json       {edition, generated_at, count, events:[...]} (current)
  events.all.json   same, all editions
  events.ndjson     one event JSON per line (current) — grep/jq friendly
  events.all.ndjson one event JSON per line (all editions)
  events.csv        flat spreadsheet (current)
  schedule.md       human+agent readable, grouped by day (current)
  events.ics        iCalendar feed (current)
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, config
from .store import Store

# Calendar weekday names for nice day headings.
_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Columns exported to CSV / used as the "flat" projection.
_CSV_COLUMNS = [
    "event_id", "name", "event_type", "start_datetime", "end_datetime",
    "start_date", "duration_minutes", "location", "creator_name",
    "stars", "will_be_filmed", "av_needs", "description",
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_time(dt: str | None) -> str:
    if not dt:
        return "?"
    s = dt.rstrip("Z")
    try:
        return datetime.fromisoformat(s.split(".")[0]).strftime("%H:%M")
    except ValueError:
        return s


def _dow(date: str) -> str:
    try:
        return _DOW[datetime.fromisoformat(date).weekday()]
    except (ValueError, TypeError):
        return ""


def _all_events(store: Store, historical: bool) -> list[dict[str, Any]]:
    rows, _ = store.query_events(
        include_historical=historical, sort="start", limit=10_000
    )
    return rows


def _events_json(events: list[dict[str, Any]], edition: str, generated: str) -> str:
    return json.dumps(
        {
            "edition": edition,
            "generated_at": generated,
            "count": len(events),
            "events": events,
        },
        ensure_ascii=False,
        indent=2,
    )


def _events_ndjson(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def _events_csv(events: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for e in events:
        row = dict(e)
        # Flatten description to a single line for CSV sanity.
        if row.get("description"):
            row["description"] = " ".join(row["description"].split())
        writer.writerow(row)
    return buf.getvalue()


def _schedule_md(events: list[dict[str, Any]], edition: str, generated: str) -> str:
    lines = [
        f"# {edition} — Schedule",
        "",
        f"_{len(events)} events · generated {generated} · times are local "
        f"wall-clock · ★ = stars (a.k.a. bookmarks)_",
        "",
    ]
    by_day: dict[str, list[dict[str, Any]]] = {}
    undated: list[dict[str, Any]] = []
    for e in events:
        (by_day.setdefault(e["start_date"], []) if e.get("start_date") else undated).append(e)  # type: ignore[arg-type]

    for day in sorted(by_day):
        evs = by_day[day]
        dow = _dow(day)
        lines.append(f"## {day}{f' ({dow})' if dow else ''} — {len(evs)} events")
        lines.append("")
        for e in evs:
            start = _fmt_time(e.get("start_datetime"))
            end = _fmt_time(e.get("end_datetime")) if e.get("end_datetime") else None
            when = f"{start}–{end}" if end else start
            venue = e.get("location") or "TBD"
            stars = e.get("stars", 0)
            creator = e.get("creator_name")
            byline = f" · by {creator}" if creator else ""
            lines.append(f"- **{when}** · {e['name']} · _{venue}_ · {stars}★{byline}")
            desc = (e.get("description") or "").strip()
            if desc:
                flat = " ".join(desc.split())
                if len(flat) > 500:
                    flat = flat[:499].rstrip() + "…"
                lines.append(f"  {flat}")
        lines.append("")

    if undated:
        lines.append(f"## Undated — {len(undated)} events")
        lines.append("")
        for e in undated:
            lines.append(f"- {e['name']} · {e.get('stars', 0)}★")
        lines.append("")

    return "\n".join(lines)


def _ics(events: list[dict[str, Any]], edition: str) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def to_ics_dt(dt: str | None) -> str | None:
        if not dt:
            return None
        s = dt.rstrip("Z").split(".")[0]
        try:
            return datetime.fromisoformat(s).strftime("%Y%m%dT%H%M%S")
        except ValueError:
            return None

    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//vibecamp-expansion//EN",
        f"X-WR-CALNAME:{esc(edition)}",
    ]
    for e in events:
        start = to_ics_dt(e.get("start_datetime"))
        if not start:
            continue
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{e['event_id']}@vibecamp-expansion")
        out.append(f"DTSTART:{start}")
        end = to_ics_dt(e.get("end_datetime"))
        if end:
            out.append(f"DTEND:{end}")
        out.append(f"SUMMARY:{esc(e['name'])}")
        if e.get("location"):
            out.append(f"LOCATION:{esc(e['location'])}")
        if e.get("description"):
            out.append(f"DESCRIPTION:{esc(e['description'])}")
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def _llms_txt(store: Store, edition: str, generated: str, n_current: int, n_all: int) -> str:
    return f"""# Vibe Camp Expansion — agent guide

An agent-friendly mirror of the Vibe Camp event schedule. Everything here is
static and public: fetch a file and grep/jq it locally. No auth, no rate limits.

Generated: {generated}
Current edition: {edition} ({n_current} events). All editions: {n_all} events.

## Fastest path for an agent

Fetch ONE file and work locally:
  events.ndjson    one JSON event per line (current edition) — pipe to jq/grep
  events.json      same data as a single JSON object with metadata
  schedule.md      human-readable, grouped by day — grep by name/venue
  events.csv       spreadsheet-friendly flat table
  events.ics       import into any calendar app

For every edition (2024, 2025, 2026), use the .all. variants:
  events.all.ndjson, events.all.json

## Event fields

  event_id        stable UUID
  name            event title
  description     free text
  event_type      UNOFFICIAL | CAMPSITE_OFFICIAL | TEAM_OFFICIAL
  start_datetime  local WALL-CLOCK time (ISO; trailing Z is NOT UTC — do not convert)
  end_datetime    local wall-clock end, or null
  start_date      YYYY-MM-DD (local)
  duration_minutes
  location        best human location (named venue or free text)
  creator_name    host
  stars           save count (a.k.a. "bookmarks" in the raw upstream API — same number)
  bookmarks       identical to stars
  will_be_filmed
  av_needs
  is_placeholder  joke/implausible-date entries (excluded from these files)

## Notes

- By default these files contain only the current edition ({edition}); past
  editions are in the .all. files.
- Times are local wall-clock. The upstream API labels them with a misleading
  "Z" — treat them as naive local time.
- Live query API and a remote MCP endpoint are also available; see index.json.
"""


def generate_exports(store: Store, out_dir: Path | None = None) -> dict[str, Any]:
    """Write all export files. Returns the manifest dict."""
    out_dir = Path(out_dir) if out_dir else config.EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = _now_utc()
    edition = config.CURRENT_EDITION_NAME

    current = _all_events(store, historical=False)
    everything = _all_events(store, historical=True)

    files = {
        "events.json": _events_json(current, edition, generated),
        "events.all.json": _events_json(everything, "All editions", generated),
        "events.ndjson": _events_ndjson(current),
        "events.all.ndjson": _events_ndjson(everything),
        "events.csv": _events_csv(current),
        "schedule.md": _schedule_md(current, edition, generated),
        "events.ics": _ics(current, edition),
        "llms.txt": _llms_txt(store, edition, generated, len(current), len(everything)),
    }

    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")

    manifest = {
        "generator": f"vibecamp-expansion/{__version__}",
        "generated_at": generated,
        "edition": edition,
        "current_edition_count": len(current),
        "all_editions_count": len(everything),
        "files": sorted(files.keys()),
        "fields_doc": "llms.txt",
    }
    (out_dir / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
