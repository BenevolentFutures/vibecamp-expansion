"""Fetch the upstream event feed and reconcile it into the local cache."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from . import config
from .normalize import normalize
from .store import Store

log = logging.getLogger("vibecamp.crawler")


class CrawlError(Exception):
    pass


def fetch_events(client: Optional[httpx.Client] = None) -> tuple[list[dict[str, Any]], int]:
    """Fetch the raw events array from upstream. Returns (events, http_status)."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=httpx.Timeout(
                connect=config.HTTP_CONNECT_TIMEOUT,
                read=config.HTTP_READ_TIMEOUT,
                write=config.HTTP_READ_TIMEOUT,
                pool=config.HTTP_READ_TIMEOUT,
            ),
            headers={"User-Agent": "vibecamp-expansion/0.1 (+crawler)"},
        )
    try:
        resp = client.get(config.EVENTS_ENDPOINT)
        status = resp.status_code
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events") if isinstance(data, dict) else data
        if not isinstance(events, list):
            raise CrawlError(f"Unexpected payload shape: {type(data).__name__}")
        return events, status
    finally:
        if owns_client:
            client.close()


def crawl_once(store: Store, client: Optional[httpx.Client] = None) -> dict[str, Any]:
    """Run a single crawl + reconcile pass. Returns a summary dict."""
    crawl_id = store.start_crawl()
    try:
        raw_events, http_status = fetch_events(client)
    except Exception as exc:  # noqa: BLE001 — we log every failure mode
        log.warning("crawl fetch failed: %s", exc)
        store.finish_crawl(
            crawl_id, status="error", error=f"{type(exc).__name__}: {exc}"
        )
        raise CrawlError(str(exc)) from exc

    raw_by_id: dict[str, Any] = {}
    normalized: list[dict[str, Any]] = []
    skipped = 0
    for raw in raw_events:
        eid = raw.get("event_id")
        if not eid:
            skipped += 1
            continue
        raw_by_id[eid] = raw
        normalized.append(normalize(raw))

    counts = store.reconcile(normalized, raw_by_id)
    store.finish_crawl(
        crawl_id,
        status="ok",
        http_status=http_status,
        event_count=len(normalized),
        counts=counts,
    )

    if config.EXPORT_ON_CRAWL:
        try:
            from .export import generate_exports

            generate_exports(store)
        except Exception as exc:  # noqa: BLE001 — exports are best-effort
            log.warning("export generation failed: %s", exc)
    summary = {
        "crawl_id": crawl_id,
        "fetched": len(raw_events),
        "skipped_no_id": skipped,
        "http_status": http_status,
        **counts,
    }
    log.info(
        "crawl ok: fetched=%d created=%d updated=%d deleted=%d resurrected=%d unchanged=%d",
        summary["fetched"], counts["created"], counts["updated"],
        counts["deleted"], counts["resurrected"], counts["unchanged"],
    )
    return summary


def crawl_loop(
    store: Store,
    interval: Optional[int] = None,
    *,
    stop_after: Optional[int] = None,
) -> None:
    """Crawl forever on a fixed interval. ``stop_after`` bounds iterations (tests)."""
    interval = interval or config.CRAWL_INTERVAL_SECONDS
    client = httpx.Client(
        timeout=httpx.Timeout(
            connect=config.HTTP_CONNECT_TIMEOUT,
            read=config.HTTP_READ_TIMEOUT,
            write=config.HTTP_READ_TIMEOUT,
            pool=config.HTTP_READ_TIMEOUT,
        ),
        headers={"User-Agent": "vibecamp-expansion/0.1 (+crawler)"},
    )
    iterations = 0
    log.info("crawl loop started: interval=%ds endpoint=%s", interval, config.EVENTS_ENDPOINT)
    try:
        while True:
            start = time.monotonic()
            try:
                crawl_once(store, client)
            except CrawlError:
                pass  # already logged + recorded in crawl_log
            iterations += 1
            if stop_after is not None and iterations >= stop_after:
                return
            elapsed = time.monotonic() - start
            time.sleep(max(1.0, interval - elapsed))
    finally:
        client.close()
