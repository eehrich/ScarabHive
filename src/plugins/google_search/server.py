from __future__ import annotations

from typing import Any
from pathlib import Path

from agent_system.mcp.base import MCPServer
from agent_system.mcp.status import (
    publish_status,
    StatusPhase,
)


class GoogleSearchServer(MCPServer):
    """Uses Google Custom Search JSON API. Expects server config to include:
    api_key: <GOOGLE_API_KEY>
    cx: <CUSTOM_SEARCH_ENGINE_ID>
    """

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            request_id = params.get("request_id") or params.get("requestId")
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

            # publish start
            try:
                await publish_status(self.name, f"Google search: {query}", request_id=request_id, phase=StatusPhase.START)
            except Exception:
                pass

            params_req = {"key": api_key, "cx": cx, "q": query, "num": min(max_results, 10)}
            resp = requests.get("https://www.googleapis.com/customsearch/v1", params=params_req, timeout=15, verify=self.ssl_verify)
            try:
                resp.raise_for_status()
            except Exception as e:
                try:
                    await publish_status(self.name, f"Google search failed: {str(e)}", request_id=request_id, level="error", phase=StatusPhase.ERROR)
                except Exception:
                    pass
                raise
            data = resp.json()
            items = data.get("items", [])
            results = []
            for it in items:
                results.append({
                    "title": it.get("title"),
                    "href": it.get("link"),
                    "body": it.get("snippet"),
                })
            try:
                await publish_status(self.name, f"Google search completed: {query} ({len(results)} results)", request_id=request_id, phase=StatusPhase.END, meta={"results": len(results)})
            except Exception:
                pass
            return {"engine": "google", "query": query, "results": results}
        raise ValueError(f"Unknown tool: {tool}")

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for Google search."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": Path(__file__).parent.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for google_search plugin")
        return schema

    def get_default_action(self) -> str:
        """Return the default action for Google search."""
        return "search"
