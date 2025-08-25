from __future__ import annotations

from typing import Any

from ...mcp.base import MCPServer
from duckduckgo_search import DDGS


class GoogleWebSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            max_results = int(params.get("max_results", 5))
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            return {"engine": "google-ddg", "query": query, "results": results}
        raise ValueError(f"Unknown tool: {tool}")
