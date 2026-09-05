"""CLI: run one DuckDuckGo search through the plugin, for a quick check.

    python -m plugins.duckduckgo_search --query "godot texture filter"
"""
from __future__ import annotations

import argparse
import asyncio

from agent_system.config.models import AgentSystemConfig, MCPConfig

from .server import DuckDuckGoSearchServer


async def _run(query: str, max_results: int) -> None:
    server = DuckDuckGoSearchServer("duckduckgo_search", AgentSystemConfig(),
                                    MCPConfig(type="duckduckgo_search", enabled=True))
    # call_with_status opens the status scope the tool expects in ``_status``.
    result = await server.call_with_status("duckduckgo_search_web_search",
                                           {"query": query, "max_results": max_results})
    if result.get("error"):
        print(f"Error: {result['error']}")
        return
    for i, item in enumerate(result["results"], 1):
        print(f"{i}. {item.get('title')}\n   {item.get('href')}\n   {item.get('body', '')}\n")


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="DuckDuckGo Search MCP Server")
    parser.add_argument("--query", required=True, help="Search query")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum number of results")
    args = parser.parse_args()
    asyncio.run(_run(args.query, args.max_results))


if __name__ == "__main__":
    cli_main()
