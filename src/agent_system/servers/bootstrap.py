from __future__ import annotations

from typing import Any

from ..config.models import AgentConfig
from ..mcp.base import MCPRegistry


def bootstrap_servers(config: AgentConfig, registry: MCPRegistry) -> None:
    for key in config.mcp.enabled_servers:
        server_cfg: dict[str, Any] = config.servers.get(key, {})
        typ = server_cfg.get("type", key)
        if typ == "duckduckgo_search":
            from .duckduckgo_search.server import DuckDuckGoSearchServer
            registry.register(key, DuckDuckGoSearchServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "google_search":
            from .google_search.server import GoogleSearchServer
            registry.register(key, GoogleSearchServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "yahoo_finance":
            from .yahoo_finance.server import YahooFinanceServer
            registry.register(key, YahooFinanceServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "twitter_search":
            from .twitter_search.server import TwitterSearchServer
            registry.register(key, TwitterSearchServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "llm_router":
            from .llm_router.server import LLMRouterServer
            registry.register(key, LLMRouterServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "web_scraper":
            from .web_scraper.server import WebScraperServer
            registry.register(key, WebScraperServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        else:
            # ignore unknown for now
            continue
