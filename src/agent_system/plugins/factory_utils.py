"""Plugin factory helpers.

This module centralizes tiny helpers so individual plugin directories can stay
minimal (ideally just: from .server import X; PLUGIN_FACTORY = make_agent_plugin_factory(X)).
"""
from __future__ import annotations
from typing import Callable, Type
import logging

from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent
from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


def make_agent_plugin_factory(agent_cls: Type[Agent]) -> Callable[[str, AgentSystemConfig, MCPConfig], Agent]:
    """Return a standard PLUGIN_FACTORY callable for an Agent subclass.

    MODERN: Clean interface expecting system-wide config and plugin-specific MCP config.
    
    Signature produced: (name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> Agent
    
    Args:
        agent_cls: The Agent subclass to instantiate
        
    Returns:
        Factory function that creates agent instances with proper configuration
        
    Note:
        The factory signature uses AgentSystemConfig and MCPConfig as the modern standard.
        The Agent constructor expects: (name, system_config, mcp_config, registry)
    """
    def _factory(name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> Agent:
        """Create an agent plugin instance.
        
        Args:
            name: Plugin instance name  
            system_config: Complete system configuration (LLM, network, context, etc.)
            mcp_config: Plugin-specific MCP configuration (enabled, type, agent_config)
        """
        # Agent constructor signature: (name, system_config, mcp_config, registry, llm, llm_factory)
        # Note: Registry will be injected by bootstrap.py after all plugins are registered
        # Create temporary empty registry that will be replaced by bootstrap
        registry = MCPRegistry()
        
        # IMPORTANT: Do not call any methods that need registry access during __init__
        # The registry will be populated and replaced by bootstrap.py
        inst = agent_cls(name, system_config, mcp_config, registry)
        
        logger.debug(
            "Instantiated agent plugin %s (type=%s) via generic factory (registry will be injected)",
            name, mcp_config.type
        )
        return inst
    
    return _factory


__all__ = ["make_agent_plugin_factory"]
