#!/usr/bin/env python3
"""CLI entrypoint for the duckduckgo_search plugin.

Provides a help/CLI surface so `python -m plugins.duckduckgo_search --help` works
for tooling and tests.
"""

from __future__ import annotations

import asyncio
import argparse
from .server import DuckDuckGoSearchServer


async def async_main():
    parser = argparse.ArgumentParser(description="DuckDuckGo Search MCP Server")
    parser.add_argument("--query", default="Python programming", help="Search query")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum number of results")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9001, help="Server port")
    args = parser.parse_args()

    server = DuckDuckGoSearchServer("duckduckgo_search")

    if args.server:
        from agent_system.http_server import serve_mcp_server
        print(f"Starting DuckDuckGo Search MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            result = await server.call("search", {"query": args.query, "max_results": args.max_results})
            print(f"DuckDuckGo search results for '{args.query}':")
            if isinstance(result, dict) and "results" in result:
                for i, item in enumerate(result["results"], 1):
                    if isinstance(item, dict):
                        title = item.get("title") or item.get("text") or "No title"
                        url = item.get("url") or item.get("href") or "No URL"
                        snippet = item.get("snippet") or item.get("text") or ""
                        print(f"{i}. {title}")
                        print(f"   {url}")
                        if snippet:
                            print(f"   {snippet}\n")
                    else:
                        print(f"{i}. {item}")
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")


def cli_main():
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
