"""Single hostable ASGI service: REST API + static exports + remote MCP + crawler.

This is the deployment entry point. One process serves everything:

  GET  /                       -> service info
  GET  /health                 -> liveness
  GET  /events, /stats, ...     -> REST API (see api.py)
  GET  /data/<file>            -> static grep-friendly exports (events.ndjson, …)
  ALL  /mcp/                   -> remote MCP endpoint (streamable-http transport)

A background thread runs the crawler loop so the cache (and the static exports)
stay fresh without a separate worker.

Run locally:
    uvicorn vibecamp_expansion.asgi:app --host 0.0.0.0 --port 8787
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__, config
# Reuse the REST app directly — it already carries every REST route plus the
# /data static mount. We attach a lifespan and graft the MCP endpoint onto it.
from .api import app
from .crawler import crawl_loop
from .mcp_server import mcp
from .store import Store

log = logging.getLogger("vibecamp.asgi")

# Build the MCP ASGI app once. Stateless transport so it scales behind a load
# balancer without sticky sessions — appropriate for a read-only tool server.
# streamable_http_path="/" so that mounting at "/mcp" yields "/mcp/" (the mount
# prefix supplies the path; otherwise the route would be at "/mcp/mcp").
mcp.settings.stateless_http = True
mcp.settings.streamable_http_path = "/"
mcp_app = mcp.streamable_http_app()

_crawler_started = False


def _start_crawler_thread() -> None:
    global _crawler_started
    if _crawler_started:
        return
    if os.environ.get("VIBECAMP_DISABLE_CRAWLER") in ("1", "true", "yes"):
        log.info("crawler disabled via VIBECAMP_DISABLE_CRAWLER")
        return
    _crawler_started = True

    def run() -> None:
        crawl_loop(Store(), interval=config.CRAWL_INTERVAL_SECONDS)

    threading.Thread(target=run, name="crawler", daemon=True).start()
    log.info("crawler thread started (interval=%ds)", config.CRAWL_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    _start_crawler_thread()
    # The MCP streamable-http transport requires its session manager running.
    async with mcp.session_manager.run():
        yield


# Graft our lifespan onto the imported REST app.
app.router.lifespan_context = lifespan


@app.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "vibecamp-expansion",
            "version": __version__,
            "edition": config.CURRENT_EDITION_NAME,
            "endpoints": {
                "rest_api": "/events  (OpenAPI at /docs, /openapi.json)",
                "static_exports": "/data/  (index.json, events.ndjson, schedule.md, llms.txt, …)",
                "remote_mcp": "/mcp/  (streamable-http transport)",
                "health": "/health",
            },
            "agent_quickstart": "Fetch /data/llms.txt, then grep /data/events.ndjson.",
        }
    )


# Friendly redirect so clients configured with the bare /mcp still reach the
# transport (which lives at /mcp/). 307 preserves method + body.
@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def _mcp_redirect() -> RedirectResponse:
    return RedirectResponse(url="/mcp/", status_code=307)


# Mount the MCP transport last; the explicit /mcp redirect route above matches
# the slash-less form first.
app.mount("/mcp", mcp_app)
