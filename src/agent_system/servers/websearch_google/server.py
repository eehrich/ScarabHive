from __future__ import annotations

from typing import Any

from ...mcp.base import MCPServer


class GoogleWebSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            max_results = int(params.get("max_results", 5))
            try:
                from duckduckgo_search import DDGS  # type: ignore
            except Exception as e:
                raise RuntimeError("duckduckgo-search is required for websearch_google. Install it via pip.") from e
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            return {"engine": "google-ddg", "query": query, "results": results}
        raise ValueError(f"Unknown tool: {tool}")
