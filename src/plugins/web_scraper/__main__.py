#!/usr/bin/env python3
"""CLI entrypoint for the web_scraper plugin.

Provides a help/CLI surface so `python -m plugins.web_scraper --help` works
for tooling and tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.web_scraper", description="Web Scraper MCP Server")

    # Core web scraper parameters
    parser.add_argument("--url", default="https://example.com", help="URL to scrape for testing")
    parser.add_argument("--max-chars", type=int, default=1000, help="Maximum characters to return (0 = unlimited)")
    parser.add_argument("--max-links", type=int, default=200, help="Maximum number of links to display when --show-links is used (0 = unlimited)")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (MCP server)")
    parser.add_argument("--port", type=int, default=9003, help="Port to listen on when in server mode")
    # Print only raw HTML when requested
    parser.add_argument("--html", dest="html", action="store_true", help="Print only the raw HTML of the fetched page to stdout")

    # Misc
    parser.add_argument("--version", action="version", version="web_scraper plugin 1.0.0")
    # Output helpers
    parser.add_argument("--show-links", dest="show_links", action="store_true", help="Print extracted links only")
    parser.add_argument("--full", dest="full", action="store_true", help="Print full JSON result")

    return parser


async def async_main():
    """Async main function for actual execution."""
    from agent_system.utils.logging import setup_logging

    # Setup logging for proper color output
    setup_logging(True, "INFO", "logs/web_scraper.log")

    parser = build_parser()
    args = parser.parse_args()

    # Lazy imports
    from .plugin import PLUGIN_FACTORY
    from agent_system.servers.http_server import serve_mcp_server

    server = PLUGIN_FACTORY("web_scraper", {})

    if args.server:
        print(f"Starting Web Scraper MCP Server on port {args.port}")
        await serve_mcp_server(server, port=args.port)
    else:
        # Direct test (async)
        await test_scraper(server, args)


async def test_scraper(server, args):
    params = {
        "url": args.url,
        "max_chars": int(getattr(args, "max_chars", 0) or 0),
        "max_links": int(getattr(args, "max_links", 0) or 0),
    "include_html": bool(getattr(args, "html", False)),
        "timeout": 10,
    }

    # If the user only wants links, call the 'links' action to avoid extra work
    if getattr(args, "show_links", False):
        result = await server.call("links", params)
    else:
        result = await server.call("fetch", params)
    # If user requested raw HTML, call fetch with include_html and print only the html
    if getattr(args, "html", False):
        # Ensure server includes raw HTML
        params["include_html"] = True
        result = await server.call("fetch", params)
        html = result.get("html") or ""
        # Respect max_chars when printing raw HTML: 0 means unlimited
        maxc = int(getattr(args, "max_chars", 0) or 0)
        if maxc and maxc > 0:
            print(html[:maxc])
        else:
            print(html)
        return

    # If full result requested, print the entire JSON payload
    if getattr(args, "full", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # If show-links requested, print only links
    links = result.get("links") or []
    if getattr(args, "show_links", False):
        print(f"URL: {result.get('url')}")
        print(f"Status: {result.get('status_code')}")
        print(f"Links found: {len(links)}")
        # Determine display limit: if max_links is 0 -> unlimited, else use that value
        display_limit = int(getattr(args, "max_links", 0) or 0)
        if display_limit <= 0:
            display_limit = len(links)
        for i, link in enumerate(links[:display_limit]):
            print(f"{i+1}. {link.get('abs_url')} ({link.get('text')})")
        return

    # Default human-friendly output (text preview)
    print(f"URL: {result.get('url')}")
    print(f"Status: {result.get('status_code')}")
    print(f"Title: {result.get('title', 'N/A')}")
    print(f"Text length: {len(result.get('text', ''))}")
    print("=" * 50)
    print("Cleaned text preview:")
    print("=" * 50)
    text = result.get('text', '')
    if text:
        # Use max_chars as preview length when set; otherwise fallback to 2000 for human preview
        preview_len = int(getattr(args, "max_chars", 0) or 0)
        if preview_len <= 0:
            preview_len = 2000
        preview = text[:preview_len]
        print(repr(preview))
        print("\n" + "=" * 50)
        print("Formatted output:")
        print("=" * 50)
        print(preview)
    else:
        print("No text extracted")


def main(argv: list[str] | None = None) -> None:
    """Main function for test/validation purposes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # For tests, print a concise summary showing that the parser accepted the args.
    summary: dict[str, Any] = {
        "description": "Web Scraper MCP Server",
        "url": args.url,
        "max_chars": args.max_chars,
    "max_links": args.max_links,
    "html": getattr(args, "html", False),
        "server_mode": args.server,
        "port": args.port,
    }

    print("Web Scraper MCP Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
