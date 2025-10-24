#!/usr/bin/env python3
"""CLI entrypoint for the twitter_search plugin.

Provides a help/CLI surface so `python -m plugins.twitter_search --help` works
for tooling and tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from agent_system.utils.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.twitter_search", description="Twitter Search MCP Server")

    # Core twitter search parameters
    parser.add_argument("--query", default="Python", help="Search query")
    parser.add_argument("--max-results", type=int, default=10, help="Maximum number of tweets")
    parser.add_argument("--lang", default="en", help="Language filter (e.g., en, de, fr)")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (MCP server)")
    parser.add_argument("--port", type=int, default=9004, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="twitter_search plugin 1.0.0")

    return parser


async def async_main():
    """Async main function for actual execution."""
    # Setup logging for proper color output
    setup_logging(True, "INFO", "logs/twitter_search.log")

    parser = build_parser()
    args = parser.parse_args()

    # Lazy import to avoid importing server code on plain `import plugins.twitter_search`
    from .plugin import PLUGIN_FACTORY
    from agent_system.servers.http_server import serve_mcp_server
    from agent_system.config.models import AgentSystemConfig, MCPConfig

    # Create minimal config for CLI usage
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="twitter_search", enabled=True)
    
    server = PLUGIN_FACTORY("twitter_search", system_config, mcp_config)

    if args.server:
        print(f"Starting Twitter Search MCP Server on port {args.port}")
        await serve_mcp_server(server, port=args.port)
    else:
        try:
            from unittest.mock import AsyncMock
            mock_status = AsyncMock()
            result = await server.call("twitter_search", {
                "query": args.query,
                "max_results": args.max_results,
                "lang": args.lang,
                "_status": mock_status
            })
            print(f"Twitter search results for '{args.query}':")
            if isinstance(result, dict) and "tweets" in result:
                for i, tweet in enumerate(result["tweets"], 1):
                    print(f"{i}. @{tweet.get('username', 'unknown')}: {tweet.get('text', 'No text')[:100]}...")
                    print(f"   Date: {tweet.get('date', 'Unknown')}")
                    print(f"   URL: {tweet.get('url', 'No URL')}\n")
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")


def main(argv: list[str] | None = None) -> None:
    """Main function for test/validation purposes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # For tests, print a concise summary showing that the parser accepted the args.
    summary: dict[str, Any] = {
        "description": "Twitter Search MCP Server",
        "query": args.query,
        "max_results": args.max_results,
        "lang": args.lang,
        "server_mode": args.server,
        "port": args.port,
    }

    print("Twitter Search MCP Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
