from __future__ import annotations

from typing import Any

from ...mcp.base import MCPServer


class GoogleSearchServer(MCPServer):
    """Uses Google Custom Search JSON API. Expects server config to include:
    api_key: <GOOGLE_API_KEY>
    cx: <CUSTOM_SEARCH_ENGINE_ID>
    """

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            max_results = int(params.get("max_results", 5))
            cfg = self.config or {}
            api_key = cfg.get("api_key")
            cx = cfg.get("cx")
            if not api_key or not cx:
                raise RuntimeError("google_search requires 'api_key' and 'cx' in server config")

            # Use simple requests call (synchronous) - fine for this scaffold
            try:
                import requests
            except Exception as e:
                raise RuntimeError("requests package required for google_search") from e

            params_req = {"key": api_key, "cx": cx, "q": query, "num": min(max_results, 10)}
            resp = requests.get("https://www.googleapis.com/customsearch/v1", params=params_req, timeout=15, verify=self.ssl_verify)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            results = []
            for it in items:
                results.append({
                    "title": it.get("title"),
                    "href": it.get("link"),
                    "body": it.get("snippet"),
                })
            return {"engine": "google", "query": query, "results": results}
        raise ValueError(f"Unknown tool: {tool}")

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for Google search."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Search the web using Google Custom Search API. Returns search results with titles, URLs, and snippets.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["search"], "description": "Use 'search' to perform web search"},
                        "query": {"type": "string", "description": "Search query terms"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "Number of search results to return"},
                    },
                    "required": ["query"],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for Google search."""
        return "search"
