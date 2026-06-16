"""Command-line entry point: crawl, serve the API, or run the MCP server."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, config
from .crawler import crawl_loop, crawl_once
from .store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_crawl(args: argparse.Namespace) -> int:
    store = Store()
    if args.loop:
        crawl_loop(store, interval=args.interval)
        return 0
    summary = crawl_once(store)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "vibecamp_expansion.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = Store()
    print(json.dumps(store.stats(), indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import generate_exports

    manifest = generate_exports(Store())
    print(json.dumps(manifest, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vibecamp", description="Vibe Camp Expansion toolkit")
    p.add_argument("--version", action="version", version=f"vibecamp-expansion {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("crawl", help="Crawl the upstream feed into the local cache.")
    pc.add_argument("--loop", action="store_true", help="Crawl continuously.")
    pc.add_argument("--interval", type=int, default=config.CRAWL_INTERVAL_SECONDS,
                    help="Loop interval in seconds (default %(default)s).")
    pc.set_defaults(func=cmd_crawl)

    ps = sub.add_parser("serve", help="Run the REST API server.")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8787)
    ps.add_argument("--reload", action="store_true")
    ps.set_defaults(func=cmd_serve)

    pm = sub.add_parser("mcp", help="Run the MCP server (stdio).")
    pm.set_defaults(func=cmd_mcp)

    pt = sub.add_parser("stats", help="Print schedule + crawl statistics.")
    pt.set_defaults(func=cmd_stats)

    pe = sub.add_parser("export", help="Regenerate static export files from the cache.")
    pe.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
