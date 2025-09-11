from __future__ import annotations

import logging
from typing import Any
from pathlib import Path

from agent_system.mcp.base import MCPServer  # absolute import to work when executed with -m
from agent_system.mcp.status import (
    publish_status,
    PHASE_START,
    PHASE_END,
)

logger = logging.getLogger(__name__)


class DuckDuckGoSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            request_id = params.get("request_id") or params.get("requestId")
            max_results = int(params.get("max_results", 5))
            
            if not query.strip():
                return {"engine": "duckduckgo", "query": query, "results": [], "package": "ddgs", "error": "Empty query"}
            
            try:
                try:
                    from ddgs import DDGS  # preferred package
                    pkg = "ddgs"
                except Exception:
                    from duckduckgo_search import DDGS  # fallback legacy
                    pkg = "duckduckgo_search"
            except Exception as e:
                raise RuntimeError("Install `ddgs` (preferred) or `duckduckgo-search` for duckduckgo_search server.") from e

            # Try search with error handling
            try:
                # publish start
                try:
                    await publish_status(self.name, f"Searching: {query}", request_id=request_id, phase=PHASE_START)
                except Exception:
                    pass

                logger.debug("DuckDuckGo search: %s (max_results=%d)", query, max_results)
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=max_results))
                logger.debug("DuckDuckGo search returned %d results", len(results))
                # publish end
                try:
                    await publish_status(
                        self.name,
                        f"Search completed: {query} ({len(results)} results)",
                        request_id=request_id,
                        phase=PHASE_END,
                        meta={"results": len(results)},
                    )
                except Exception:
                    pass
                return {"engine": "duckduckgo", "query": query, "results": results, "package": pkg}
            except Exception as e:
                logger.warning("DuckDuckGo search failed for query '%s': %s", query, str(e))
                return {
                    "engine": "duckduckgo",
                    "query": query,
                    "results": [],
                    "package": pkg,
                    "error": f"Search failed: {str(e)}",
                    "suggestion": "Try a different search query or use broader terms",
                }
        raise ValueError(f"Unknown tool: {tool}")

    def get_schema(self) -> dict[str, Any]:
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": Path(__file__).parent.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for duckduckgo_search plugin")
        return schema

    def get_default_action(self) -> str:
        return "search"
