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
        elif typ == "weather":
            from .weather.server import WeatherServer
            registry.register(key, WeatherServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "datetime":
            from .datetime.server import DateTimeServer
            registry.register(key, DateTimeServer(key, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "sub_agent":
            # Sub-agent type requires special handling - needs an agent instance
            # This will be used later for specialized agents
            from ..agent.sub_agent import SubAgent
            from ..agent.core import Agent
            # For now, create a basic sub-agent (will be enhanced in specialized agents)
            sub_agent_config = AgentConfig()  # Use default config for sub-agent
            sub_registry = MCPRegistry()  # Empty registry for sub-agent
            sub_agent_instance = Agent("basic_sub_agent", sub_agent_config, sub_registry)
            registry.register(key, SubAgent(key, sub_agent_instance, server_cfg, ssl_verify=config.network.ssl_verify))
        elif typ == "web_research_agent":
            # Specialized web research agent
            from ..agent.web_research_agent import WebResearchAgent
            registry.register(key, WebResearchAgent(key, server_cfg, ssl_verify=config.network.ssl_verify))
        else:
            # ignore unknown for now
            continue
