from __future__ import annotations

from typing import Any

from ...mcp.base import MCPServer


class AbstractWebSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            return {"engine": "abstract", "query": query, "results": []}
        raise ValueError(f"Unknown tool: {tool}")
