from __future__ import annotations

import logging
from typing import Any

from ...mcp.base import MCPServer

logger = logging.getLogger(__name__)


class DuckDuckGoSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
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
                logger.debug("DuckDuckGo search: %s (max_results=%d)", query, max_results)
                
                # ddgs uses httpx under the hood; it respects environment variables like CURL_CA_BUNDLE / SSL_CERT_FILE.
                # If ssl_verify is False, we attempt to bypass verification by patching httpx client via context if exposed,
                # otherwise rely on system env (admin networks often replace cert stores).
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=max_results))
                
                logger.debug("DuckDuckGo search returned %d results", len(results))
                return {"engine": "duckduckgo", "query": query, "results": results, "package": pkg}
                
            except Exception as e:
                # Handle DDGSException and other errors gracefully
                logger.warning("DuckDuckGo search failed for query '%s': %s", query, str(e))
                
                # Return empty results instead of raising exception
                return {
                    "engine": "duckduckgo", 
                    "query": query, 
                    "results": [], 
                    "package": pkg,
                    "error": f"Search failed: {str(e)}",
                    "suggestion": "Try a different search query or use broader terms"
                }
                
        raise ValueError(f"Unknown tool: {tool}")

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for DuckDuckGo search."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Search the web using DuckDuckGo search engine. Returns search results with titles, URLs, and snippets.",
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
        """Return the default action for DuckDuckGo search."""
        return "search"
