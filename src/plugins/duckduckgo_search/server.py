from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from pathlib import Path

from agent_system.mcp.base import MCPServer  # absolute import to work when executed with -m

logger = logging.getLogger(__name__)


class DuckDuckGoSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool != "web_search":
            return {"error": f"Unknown tool: {tool}. Only 'web_search' supported."}
            
        query = params.get("query", "")
        max_results = int(params.get("max_results", 5))
        status = params.get("_status")  # Get status object from base class
        
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

        # Update status with search progress
        if status:
            await status.progress(f"🔍 Searching: {query}")

        logger.debug("DuckDuckGo search: %s (max_results=%d)", query, max_results)
        results = []
        
        try:
            # Retry logic for rate limiting and temporary failures
            max_retries = 3
            for attempt in range(max_retries + 1):
                try:
                    # Add delay before retry attempts (not before first attempt)
                    if attempt > 0:
                        delay = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff with jitter
                        logger.debug("Search attempt %d failed, retrying in %.2f seconds", attempt, delay)
                        await asyncio.sleep(delay)
                    
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=max_results))
                    break  # Success, exit retry loop
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    is_rate_limit = any(indicator in error_msg for indicator in [
                        '429', 'rate limit', 'too many requests', 'throttle', 'blocked'
                    ])
                    
                    if attempt < max_retries and (is_rate_limit or 'timeout' in error_msg or 'connection' in error_msg):
                        logger.warning("DuckDuckGo search attempt %d failed: %s (will retry)", attempt + 1, str(e))
                        continue
                    else:
                        # Final attempt or non-retryable error
                        raise e
            
            logger.debug("DuckDuckGo search returned %d results", len(results))
            
            # Update status with success
            if status:
                await status.end(f"Search completed: {query} ({len(results)} results)",
                               meta={"results": len(results)})
            
            return {"engine": "duckduckgo", "query": query, "results": results, "package": pkg}
        
        except Exception as e:
            logger.warning("DuckDuckGo search failed for query '%s': %s", query, str(e))
            
            # Update status with error
            if status:
                await status.error(f"Search failed for query '{query}': {str(e)}", meta={"error": str(e)})
            
            return {
                "engine": "duckduckgo",
                "query": query,
                "results": [],
                "package": pkg,
                "error": f"Search failed for query '{query}': {str(e)}",
                "suggestion": "Try a different search query or use broader terms",
            }

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tools from schema.yaml - Multi-Tool format."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema_data = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema_data:
            raise RuntimeError("Missing required schema.yaml for duckduckgo_search plugin")
        
        if 'tools' in schema_data:
            return schema_data['tools']
        else:
            raise RuntimeError("DuckDuckGo Search plugin must use Multi-Tool format with 'tools' array")
