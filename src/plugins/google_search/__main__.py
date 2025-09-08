#!/usr/bin/env python3
"""Google Search plugin entrypoint shim."""

from __future__ import annotations

import asyncio
import argparse
from plugins.google_search.server import GoogleSearchServer
from agent_system.utils.logging import setup_logging


async def async_main():
    # Setup logging for proper color output
    setup_logging(True, "INFO", "logs/google_search.log")
    
    parser = argparse.ArgumentParser(description="Google Search MCP Server")
    parser.add_argument("--query", default="Python programming", help="Search query")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum number of results")
    parser.add_argument("--api-key", help="Google API key (or set GOOGLE_API_KEY env var)")
    parser.add_argument("--cx", help="Google Custom Search Engine ID (or set GOOGLE_CX env var)")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9006, help="Server port")
    args = parser.parse_args()
    
    # Create server config
    config = {}
    if args.api_key:
        config["api_key"] = args.api_key
    if args.cx:
        config["cx"] = args.cx
    
    server = GoogleSearchServer("google_search", config)
    
    if args.server:
        from agent_system.servers.http_server import serve_mcp_server
        print(f"Starting Google Search MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            result = await server.call("search", {
                "query": args.query,
                "max_results": args.max_results
            })
            print(result)
        except Exception as e:
            print(f"Error: {e}")


def cli_main():
    asyncio.run(async_main())

if __name__ == "__main__":
    cli_main()
