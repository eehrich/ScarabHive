from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.plugins.cache import PluginCache

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class DuckDuckGoSearchServer(SchemaBasedMCPServer):
    """DuckDuckGo search server with caching and retry logic."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration (cache_ttl, cache_enabled, etc.)
        """
        super().__init__(name, system_config, mcp_config)
        
        # Initialize cache system 
        # Search results typically change more frequently, so shorter TTL (15 minutes default)
        cache_ttl = getattr(mcp_config, 'cache_ttl', 900)
        self.cache = PluginCache(plugin_name="duckduckgo_search", default_ttl=cache_ttl)
        self.cache_enabled = getattr(mcp_config, 'cache_enabled', True)
    
    def _create_cache_key(self, query: str, max_results: int) -> str:
        """Create a cache key from search parameters."""
        import json
        cache_data = {
            "query": query.strip().lower(),  # Normalize query
            "max_results": max_results
        }
        return json.dumps(cache_data, sort_keys=True, separators=(',', ':'))
    
    async def web_search(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Perform web search using DuckDuckGo.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        query = params.get("query", "")
        max_results = int(params.get("max_results", 10))
        ignore_cache = params.get("ignore_cache", False)
        custom_cache_ttl = params.get("cache_ttl")
        status = params["_status"]  # Status is mandatory from framework
        
        if not query.strip():
            return {"engine": "duckduckgo", "query": query, "results": [], "package": "ddgs", "error": "Empty query"}
        
        # Create cache key and try to get cached result
        cache_key = self._create_cache_key(query, max_results)
        
        if self.cache_enabled and not ignore_cache:
            cached_result = await self.cache.get(cache_key)
            if cached_result is not None:
                await status.end("Retrieved from cache", meta={"cache_hit": True})
                logger.debug(f"Cache hit for query: {query[:50]}...")
                return cached_result
            
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
        await status.progress(f"🔍 Searching: {query}")

        logger.debug("DuckDuckGo search: %s (max_results=%d)", query, max_results)
        results = []
        
        try:
            # Retry logic for rate limiting and temporary failures
            max_retries = 3
            for attempt in range(max_retries + 1):
                # Check for cancellation before each attempt
                cancellation_token = params.get("_cancellation_token")
                if cancellation_token and cancellation_token.is_cancelled:
                    return {"error": "Search cancelled by user", "results": [], "cancelled": True}
                
                try:
                    # Add delay before retry attempts (not before first attempt)
                    if attempt > 0:
                        delay = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff with jitter
                        logger.debug("Search attempt %d failed, retrying in %.2f seconds", attempt, delay)
                        await asyncio.sleep(delay)
                    
                    # Run sync ddgs call in thread pool to avoid blocking event loop
                    def _search_sync() -> list:
                        with DDGS() as ddgs:
                            return list(ddgs.text(query, max_results=max_results))
                    
                    results = await asyncio.to_thread(_search_sync)
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
            
            # Create result object
            search_result = {"engine": "duckduckgo", "query": query, "results": results, "package": pkg}
            
            # Cache the successful result
            if self.cache_enabled:
                await self.cache.set(cache_key, search_result, ttl=custom_cache_ttl)
                logger.debug(f"Cached search results for query: {query[:50]}...")
            
            # Update status with success
            await status.end(f"Search completed: {query} ({len(results)} results)",
                           meta={"results": len(results)})
            
            return search_result
        
        except Exception as e:
            logger.warning("DuckDuckGo search failed for query '%s': %s", query, str(e))
            
            # Update status with error
            await status.error(f"Search failed for query '{query}': {str(e)}", meta={"error": str(e)})
            
            return {
                "engine": "duckduckgo",
                "query": query,
                "results": [],
                "package": pkg,
                "error": f"Search failed for query '{query}': {str(e)}",
                "suggestion": "Try a different search query or use broader terms",
            }


