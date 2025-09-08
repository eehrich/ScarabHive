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
    parser.add_argument("--max-chars", type=int, default=1000, help="Maximum characters to return")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (MCP server)")
    parser.add_argument("--port", type=int, default=9003, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="web_scraper plugin 1.0.0")

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
    result = await server.call("fetch", {
        "url": args.url,
        "max_chars": args.max_chars,
        "timeout": 10
    })

    print(f"URL: {result.get('url')}")
    print(f"Status: {result.get('status_code')}")
    print(f"Title: {result.get('title', 'N/A')}")
    print(f"Text length: {len(result.get('text', ''))}")
    print("=" * 50)
    print("Cleaned text preview:")
    print("=" * 50)
    text = result.get('text', '')
    if text:
        preview = text[:2000]
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
