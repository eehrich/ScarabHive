from __future__ import annotations

from typing import Any

from ...mcp.base import MCPServer


class DuckDuckGoSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            max_results = int(params.get("max_results", 5))
            try:
                try:
                    from ddgs import DDGS  # preferred package
                    pkg = "ddgs"
                except Exception:
                    from duckduckgo_search import DDGS  # fallback legacy
                    pkg = "duckduckgo_search"
            except Exception as e:
                raise RuntimeError("Install `ddgs` (preferred) or `duckduckgo-search` for duckduckgo_search server.") from e

            # ddgs uses httpx under the hood; it respects environment variables like CURL_CA_BUNDLE / SSL_CERT_FILE.
            # If ssl_verify is False, we attempt to bypass verification by patching httpx client via context if exposed,
            # otherwise rely on system env (admin networks often replace cert stores).
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            return {"engine": "duckduckgo", "query": query, "results": results, "package": pkg}
        raise ValueError(f"Unknown tool: {tool}")
