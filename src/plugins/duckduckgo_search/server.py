"""DuckDuckGo web search through the ``ddgs`` package.

``ddgs`` is the current name of what used to ship as ``duckduckgo-search``;
the author (deedy5) renamed it at 9.x. The old package stopped following
DuckDuckGo's markup and returned zero results without raising -- measured
on 2026-09-05: 0/0/5 hits on three queries against 5/5/5 with ddgs 9.16.
Silence, not an error, is why it went unnoticed for so long.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.plugins.cache import PluginCache

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# ddgs's own sentinel for "every engine answered, none had a hit" -- as
# opposed to "every engine failed", which arrives as the same exception type
# carrying the engine's error.
NO_RESULTS = "No results found"


class DuckDuckGoSearchServer(SchemaBasedToolServer):
    """One tool: ``web_search``. Cached per (query, max_results)."""

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        self.cache = PluginCache(plugin_name="duckduckgo_search",
                                 default_ttl=getattr(server_config, "cache_ttl", 900))
        self.cache_enabled = getattr(server_config, "cache_enabled", True)

    @staticmethod
    def _cache_key(query: str, max_results: int) -> str:
        return json.dumps({"query": query.strip().lower(), "max_results": max_results},
                          sort_keys=True, separators=(",", ":"))

    async def web_search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = (params.get("query") or "").strip()
        max_results = int(params.get("max_results", 10))
        status = params["_status"]

        if not query:
            return {"engine": "duckduckgo", "query": query, "results": [], "error": "Empty query"}

        key = self._cache_key(query, max_results)
        if self.cache_enabled and not params.get("ignore_cache", False):
            cached = await self.cache.get(key)
            if cached is not None:
                await status.end(f"{len(cached['results'])} results (cached) -- {query[:60]}",
                                 meta={"cache_hit": True, "results": len(cached["results"])})
                return cached

        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException
        except ImportError as e:
            raise RuntimeError("Install `ddgs` for the duckduckgo_search plugin: pip install ddgs") from e

        await status.progress(f"Searching: {query}")

        results: list[dict[str, Any]] = []
        for attempt in range(MAX_ATTEMPTS):
            token = params.get("_cancellation_token")
            if token and token.is_cancelled:
                return {"engine": "duckduckgo", "query": query, "results": [],
                        "error": "Search cancelled by user", "cancelled": True}
            try:
                # The ddgs client is synchronous; keep it off the event loop.
                results = await asyncio.to_thread(
                    lambda: DDGS().text(query, max_results=max_results))
                break
            except DDGSException as e:
                # ddgs raises instead of returning an empty list, and the SAME
                # exception type carries two very different things: the
                # sentinel "No results found." when every engine answered with
                # nothing, and the last engine's own exception when they all
                # failed. Only the first is an answer.
                if NO_RESULTS in str(e):
                    break
                # RatelimitException and TimeoutException are subclasses and
                # land here too; every one of these is worth another attempt.
                if attempt == MAX_ATTEMPTS - 1:
                    return await self._failed(status, query, e)
                delay = 2 ** attempt + random.uniform(0, 1)
                logger.warning("DuckDuckGo attempt %d failed (%s); retrying in %.1fs",
                               attempt + 1, type(e).__name__, delay)
                await asyncio.sleep(delay)

        result = {"engine": "duckduckgo", "query": query, "results": results}
        # An empty answer is not worth remembering: it is far more often a
        # hiccup on DuckDuckGo's side than a fact about the query, and a
        # cached "nothing" would repeat the hiccup for the cache lifetime.
        if self.cache_enabled and results:
            await self.cache.set(key, result, ttl=params.get("cache_ttl"))
        await status.end(f"{len(results)} results -- {query[:60]}", meta={"results": len(results)})
        return result

    @staticmethod
    async def _failed(status, query: str, exc: Exception) -> dict[str, Any]:
        message = f"DuckDuckGo search failed for {query!r}: {type(exc).__name__}: {exc}"
        logger.warning(message)
        await status.error(message, meta={"error": str(exc)})
        return {"engine": "duckduckgo", "query": query, "results": [], "error": message}
