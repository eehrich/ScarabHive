#!/usr/bin/env python3
"""CLI entrypoint for the web_research_agent plugin.

Provides a help/CLI surface so `python -m plugins.web_research_agent --help` works
for tooling and tests.
"""

from __future__ import annotations

import asyncio
import argparse
from .server import WebResearchAgent


async def async_main():
    parser = argparse.ArgumentParser(description="Web Research Agent MCP Server")
    parser.add_argument("--query", default="artificial intelligence trends 2025", help="Research query")
    parser.add_argument("--action", default="research", choices=["research", "fact_check", "compare_sources"], help="Action to perform")
    parser.add_argument("--max-results", type=int, default=3, help="Maximum number of results")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9002, help="Server port")
    args = parser.parse_args()

    server = WebResearchAgent("web_research_agent")

    if args.server:
        from agent_system.http_server import serve_mcp_server
        print(f"Starting Web Research Agent MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            if args.action == "research":
                print(f"Researching: {args.query}")
                result = await server.call("research", {
                    "topic": args.query,
                    "max_results": args.max_results
                })
            elif args.action == "fact_check":
                print(f"Fact-checking: {args.query}")
                result = await server.call("fact_check", {
                    "claim": args.query
                })
            else:
                print(f"Invalid action: {args.action}")
                return

            print("\nResult:")
            if isinstance(result, dict):
                if "error" in result:
                    print(f"Error: {result['error']}")
                else:
                    print(result)
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")


def cli_main():
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
