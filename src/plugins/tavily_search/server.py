"""Tavily search server - AI-powered web search and content extraction."""

from __future__ import annotations

import logging
import os
from typing import Any, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.plugins.cache import PluginCache

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONTENT_CHARS = 20000


def _content_cap(value: Any) -> int:
    """`max_content_chars` as configured; anything but a positive int is the default."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_CONTENT_CHARS


def _cut_pages(data: dict[str, Any], cap: int) -> dict[str, Any]:
    """`data` with every result's raw_content cut to `cap` characters.

    A cut item gets "truncated": <original length>. The cache holds the uncut
    answer, so a changed cap applies to cached answers too; this builds new
    dicts and never touches `data`.
    """
    results = []
    for item in data.get("results", []):
        raw = item.get("raw_content")
        if isinstance(raw, str) and len(raw) > cap:
            item = {**item, "raw_content": raw[:cap], "truncated": len(raw)}
        results.append(item)
    return {**data, "results": results}


def _error_text(exc: Exception) -> str:
    """What the model is told about a failed Tavily call.

    tavily-python raises one class per HTTP status and carries only the API's
    detail text -- no status code, and None or "" when the body had none. So
    the class decides, not the words: 403/432/433 (forbidden, plan or credit
    limit) are ForbiddenError, and retrying them later does not help.
    """
    kind = type(exc).__name__
    if kind == "InvalidAPIKeyError":
        return "Invalid Tavily API key. Please check your configuration."
    if kind == "UsageLimitExceededError":
        return "Tavily API rate limit exceeded. Please try again later."
    # httpx's 5xx text adds a second line with a docs link: keep the first, bounded
    detail = (str(exc).splitlines() or [""])[0][:300]
    return f"{kind}: {detail}" if detail not in ("", "None") else kind


class TavilySearchServer(SchemaBasedToolServer):
    """Tavily search server with caching and async support.
    
    Provides two main tools:
    - web_search: AI-powered web search with filtering options
    - extract: Content extraction from URLs with better success than traditional scraping
    """
    
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """Initialize Tavily search server.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration (api_key, cache_ttl, etc.)
        """
        super().__init__(name, system_config, server_config)
        
        # Get API key from config or environment
        self.api_key = getattr(server_config, 'api_key', None) or os.environ.get('TAVILY_API_KEY', '')
        if not self.api_key:
            # Without a key the server stays loadable but offers NO tools (see
            # get_template_vars): an agent must not see a tool whose every call
            # can only fail, or it burns a step finding that out.
            logger.warning("Tavily API key not configured -- %s offers no tools. "
                           "Set TAVILY_API_KEY or api_key in plugins.yaml", name)
        
        # Initialize cache (30 minutes default for search results)
        cache_ttl = getattr(server_config, 'cache_ttl', 1800)
        self.cache = PluginCache(plugin_name="tavily_search", default_ttl=cache_ttl)
        self.cache_enabled = getattr(server_config, 'cache_enabled', True)
        
        # Default settings
        self.default_max_results = getattr(server_config, 'default_max_results', 5)
        self.default_search_depth = getattr(server_config, 'default_search_depth', 'basic')
        self.max_content_chars = _content_cap(getattr(server_config, 'max_content_chars', None))
        
        # Lazy-loaded client
        self._client: Any = None
    
    def get_template_vars(self) -> dict[str, Any]:
        """schema.yaml renders its tools only when a key is configured."""
        vars = super().get_template_vars()
        vars["api_key_configured"] = bool(self.api_key)
        return vars

    async def _get_client(self) -> Any:
        """Get or create async Tavily client."""
        if self._client is None:
            try:
                from tavily import AsyncTavilyClient
            except ImportError as e:
                raise RuntimeError(
                    "Install 'tavily-python' for tavily_search plugin: pip install tavily-python"
                ) from e
            
            if not self.api_key:
                raise RuntimeError(
                    "Tavily API key not configured. Set TAVILY_API_KEY env var or api_key in plugins.yaml"
                )
            
            self._client = AsyncTavilyClient(api_key=self.api_key)
        
        return self._client
    
    def _create_search_cache_key(self, query: str, params: dict[str, Any]) -> str:
        """Create cache key from search parameters."""
        import json
        cache_data = {
            "query": query.strip().lower(),
            "max_results": params.get("max_results", self.default_max_results),
            "search_depth": params.get("search_depth", self.default_search_depth),
            "topic": params.get("topic", "general"),
            "time_range": params.get("time_range"),
            "include_domains": sorted(params.get("include_domains") or []),
            "exclude_domains": sorted(params.get("exclude_domains") or []),
            "include_raw_content": params.get("include_raw_content", False),
            "include_answer": params.get("include_answer", False),
        }
        return f"search:{json.dumps(cache_data, sort_keys=True, separators=(',', ':'))}"
    
    def _create_extract_cache_key(self, urls: list[str], params: dict[str, Any]) -> str:
        """Create cache key from extract parameters."""
        import json
        cache_data = {
            "urls": sorted(urls),
            "extract_depth": params.get("extract_depth", "basic"),
            "format": params.get("format", "markdown"),
            "include_images": params.get("include_images", False),
        }
        return f"extract:{json.dumps(cache_data, sort_keys=True, separators=(',', ':'))}"
    
    async def web_search(self, params: dict[str, Any]) -> dict[str, Any]:
        """Perform AI-powered web search using Tavily.
        
        Returns search results with titles, URLs, content snippets, and optionally
        full page content and AI-generated answers.
        """
        query = params.get("query", "").strip()
        status = params["_status"]
        cancellation_token = params.get("_cancellation_token")
        ignore_cache = params.get("ignore_cache", False)
        
        if not query:
            return {
                "error": "Empty query provided",
                "query": query,
                "results": [],
            }
        
        # Build search parameters
        search_params = {
            "max_results": params.get("max_results", self.default_max_results),
            "search_depth": params.get("search_depth", self.default_search_depth),
            "topic": params.get("topic", "general"),
            "include_raw_content": params.get("include_raw_content", False),
            "include_answer": params.get("include_answer", False),
        }
        
        # Optional filters
        if params.get("time_range"):
            search_params["time_range"] = params["time_range"]
        if params.get("include_domains"):
            search_params["include_domains"] = params["include_domains"]
        if params.get("exclude_domains"):
            search_params["exclude_domains"] = params["exclude_domains"]
        
        # Check cache
        cache_key = self._create_search_cache_key(query, search_params)
        if self.cache_enabled and not ignore_cache:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                # Subject and count, like the fresh path below -- the bare
                # "Retrieved from cache" replaced the progress line that had
                # the query, so a cache hit said nothing at all.
                hits = cached.get("result_count", len(cached.get("results", [])))
                await status.end(
                    f"{hits} results (cached) -- {query[:60]}",
                    meta={"cache_hit": True, "results": hits})
                logger.debug(f"Cache hit for Tavily search: {query[:50]}...")
                return _cut_pages(cached, self.max_content_chars)
        
        # Check cancellation
        if cancellation_token and cancellation_token.is_cancelled:
            return {"error": "Search cancelled", "query": query, "results": [], "cancelled": True}
        
        await status.progress(f"🔍 Searching: {query}")
        
        try:
            client = await self._get_client()
            
            # Execute search
            response = await client.search(query=query, **search_params)
            
            # Format results
            results = []
            for r in response.get("results", []):
                result = {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "score": r.get("score", 0.0),
                }
                if r.get("raw_content"):
                    result["raw_content"] = r["raw_content"]
                if r.get("published_date"):
                    result["published_date"] = r["published_date"]
                results.append(result)
            
            result_data = {
                "query": query,
                "results": results,
                "result_count": len(results),
            }
            
            # Include answer if requested
            if response.get("answer"):
                result_data["answer"] = response["answer"]
            
            # Cache successful results
            if self.cache_enabled:
                await self.cache.set(cache_key, result_data)
                logger.debug(f"Cached Tavily search results: {query[:50]}...")
            
            await status.end(
                f"{len(results)} results -- {query[:60]}",
                meta={"results": len(results)}
            )
            
            return _cut_pages(result_data, self.max_content_chars)
            
        except Exception as e:
            error_msg = _error_text(e)
            logger.warning(f"Tavily search failed for '{query}': {error_msg}")
            await status.error(f"Search failed: {error_msg}")
            
            return {
                "error": error_msg,
                "query": query,
                "results": [],
            }
    
    async def extract(self, params: dict[str, Any]) -> dict[str, Any]:
        """Extract content from URLs using Tavily's AI-powered extraction.
        
        Returns clean, structured content from web pages with better success
        rate than traditional scraping.
        """
        urls = params.get("urls", [])
        if isinstance(urls, str):
            # One URL sent as a string: len() and sorted() would count and
            # sort its characters ("Too many URLs (34)" for a single page).
            urls = [urls]
        status = params["_status"]
        cancellation_token = params.get("_cancellation_token")
        ignore_cache = params.get("ignore_cache", False)
        
        if not urls:
            return {
                "error": "No URLs provided",
                "results": [],
                "failed_results": [],
            }
        
        # Validate URL count
        if len(urls) > 20:
            return {
                "error": f"Too many URLs ({len(urls)}). Maximum is 20 per request.",
                "results": [],
                "failed_results": [],
            }
        
        # Build extract parameters
        extract_params = {
            "extract_depth": params.get("extract_depth", "basic"),
            "format": params.get("format", "markdown"),
            "include_images": params.get("include_images", False),
        }
        
        # Check cache
        cache_key = self._create_extract_cache_key(urls, extract_params)
        if self.cache_enabled and not ignore_cache:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                # What was EXTRACTED, not what was requested -- the cached
                # payload carries its own counts, and partial failures are
                # cached too, so `len(urls)` reported three pages for a call
                # that had returned one.
                got = cached.get("success_count", len(cached.get("results", [])))
                missed = cached.get("failed_count", 0)
                await status.end(
                    f"{got} page(s) (cached)" + (f", {missed} failed" if missed else ""),
                    meta={"cache_hit": True, "success": got, "failed": missed})
                logger.debug(f"Cache hit for Tavily extract: {len(urls)} URLs")
                return _cut_pages(cached, self.max_content_chars)
        
        # Check cancellation
        if cancellation_token and cancellation_token.is_cancelled:
            return {"error": "Extraction cancelled", "results": [], "failed_results": [], "cancelled": True}
        
        await status.progress(f"📄 Extracting content from {len(urls)} URL(s)")
        
        try:
            client = await self._get_client()
            
            # Execute extraction
            response = await client.extract(urls=urls, **extract_params)
            
            # Format results
            results = []
            for r in response.get("results", []):
                result = {
                    "url": r.get("url", ""),
                    "raw_content": r.get("raw_content", ""),
                }
                if r.get("images"):
                    result["images"] = r["images"]
                results.append(result)
            
            failed_results = []
            for f in response.get("failed_results", []):
                failed_results.append({
                    "url": f.get("url", ""),
                    "error": f.get("error", "Unknown error"),
                })
            
            result_data = {
                "results": results,
                "failed_results": failed_results,
                "success_count": len(results),
                "failed_count": len(failed_results),
            }
            
            # Cache successful results
            if self.cache_enabled and results:
                await self.cache.set(cache_key, result_data)
                logger.debug(f"Cached Tavily extract results: {len(urls)} URLs")
            
            status_msg = f"Extracted {len(results)} page(s)"
            if failed_results:
                status_msg += f", {len(failed_results)} failed"
            
            await status.end(status_msg, meta={"success": len(results), "failed": len(failed_results)})
            
            return _cut_pages(result_data, self.max_content_chars)
            
        except Exception as e:
            error_msg = _error_text(e)
            logger.warning(f"Tavily extract failed: {error_msg}")
            await status.error(f"Extraction failed: {error_msg}")
            
            return {
                "error": error_msg,
                "results": [],
                "failed_results": [{"url": url, "error": error_msg} for url in urls],
            }
