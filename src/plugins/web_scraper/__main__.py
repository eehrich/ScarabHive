"""CLI: fetch one page through the plugin, for a quick check.

    python -m plugins.web_scraper --url https://example.com
    python -m plugins.web_scraper --url https://example.com --links
"""
from __future__ import annotations

import argparse
import asyncio
import json

from agent_system.config.models import AgentSystemConfig, ToolServerConfig

from .server import WebScraperServer


async def _run(args: argparse.Namespace) -> None:
    server = WebScraperServer("web_scraper", AgentSystemConfig(), ToolServerConfig(type="web_scraper", enabled=True))
    params = {"url": args.url, "operation": "links" if args.links else "content",
              "max_chars": args.max_chars, "ignore_cache": True}
    # call_with_status opens the status scope the tool expects in ``_status``.
    result = await server.call_with_status("web_scraper_page", params)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="Web Scraper Tool Server")
    parser.add_argument("--url", required=True, help="URL to fetch")
    parser.add_argument("--links", action="store_true", help="List the page's links instead of its text")
    parser.add_argument("--max-chars", type=int, default=2000, help="Characters of text to print (0 = all)")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    cli_main()
