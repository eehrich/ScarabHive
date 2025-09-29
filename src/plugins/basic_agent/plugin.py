"""Basic Agent plugin factory and configuration.

This module provides the plugin factory function that creates and configures
the basic agent plugin server. It demonstrates proper plugin initialization,
configuration handling, and dependency injection patterns for agent-based plugins.
"""

from __future__ import annotations
import logging
from typing import Any

from .server import BasicAgent

logger = logging.getLogger(__name__)


def PLUGIN_FACTORY(name: str, config: dict[str, Any] | None = None, ssl_verify: bool = True) -> BasicAgent:
    """Create and configure a basic agent plugin server instance.
    
    Args:
        name: The plugin instance name
        config: Plugin configuration dict with parent_llm injected by bootstrap
        ssl_verify: Enable SSL certificate verification (default: True)
    
    Returns:
        Configured BasicAgent instance
        
    Example:
        >>> plugin = PLUGIN_FACTORY("my_agent", {
        ...     "max_steps": 50,
        ...     "enable_debug": True
        ... })
        >>> # Plugin provides basic agent execution capabilities
    """
    logger.debug(f"Creating basic agent plugin instance: {name}")
    
    # Extract server configuration
    server_cfg = config or {}
    parent_llm = server_cfg.get("parent_llm") if isinstance(server_cfg, dict) else None
    
    # Ensure we have LLM configuration
    if not isinstance(parent_llm, dict) or not parent_llm.get('llm_system'):
        raise ValueError(f"BasicAgent '{name}' requires LLM system configuration with parent_llm")
    
    # Create agent configuration using parent LLM system
    from agent_system.config.models import AgentConfig, MCPConfig, LLMSystemConfig
    from agent_system.mcp.base import MCPRegistry
    from agent_system.servers.bootstrap import bootstrap_servers
    
    agent_config = AgentConfig(
        llm_system=LLMSystemConfig(**(parent_llm.get('llm_system', {}))),
        agent_llm_profiles=parent_llm.get('agent_llm_profiles', {}),
        mcp=MCPConfig(enabled_servers=[
            "basic_operations", "datetime", "duckduckgo_search",
            "script_interpreter", "weather", "web_scraper"
        ]),
        servers={
            "basic_operations": {"type": "basic_operations"},
            "datetime": {"type": "datetime"},
            "duckduckgo_search": {"type": "duckduckgo_search"},
            "script_interpreter": {"type": "script_interpreter"},
            "weather": {"type": "weather"},
            "web_scraper": {"type": "web_scraper"}
        },
        max_steps=int(server_cfg.get("max_steps", 30)),
        network={"ssl_verify": ssl_verify}
    )
    
    # Create registry and bootstrap servers
    registry = MCPRegistry()
    bootstrap_servers(agent_config, registry)
    
    # Configure logging if debug is enabled
    if server_cfg.get("enable_debug"):
        logging.getLogger(f"plugins.basic_agent.{name}").setLevel(logging.DEBUG)
        logger.debug(f"Debug logging enabled for {name}")
    
    # Log LLM configuration info
    from agent_system.llm.factory import resolve_llm_config_for_agent
    llm_kwargs = resolve_llm_config_for_agent(agent_config, name)
    logger.info(f"BasicAgent '{name}' using LLM profile: provider={llm_kwargs['provider']}, model={llm_kwargs['model']}")
    
    logger.info(f"Basic agent plugin '{name}' initialized with tools: {registry.list()}")
    
    return BasicAgent(name=name, config=agent_config, registry=registry, ssl_verify=ssl_verify)