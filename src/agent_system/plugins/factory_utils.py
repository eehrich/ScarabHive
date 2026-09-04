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


def make_agent_plugin_factory(agent_cls: Type[Agent]) -> Callable[..., Agent]:
    """Return a standard PLUGIN_FACTORY callable for an Agent subclass.

    MODERN: Clean interface expecting system-wide config and plugin-specific MCP config.

    Signature produced: (name, system_config, mcp_config, registry=None) -> Agent.
    The factory carries ``_accepts_registry = True``; callers that do not know
    about it (mcp_adapter) keep calling it with three arguments.

    Args:
        agent_cls: The Agent subclass to instantiate
        
    Returns:
        Factory function that creates agent instances with proper configuration
        
    Note:
        The factory signature uses AgentSystemConfig and MCPConfig as the modern standard.
        The Agent constructor expects: (name, system_config, mcp_config, registry)
    """
    def _factory(name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig,
                 registry: MCPRegistry | None = None) -> Agent:
        """Create an agent plugin instance.

        Args:
            name: Plugin instance name
            system_config: Complete system configuration (LLM, network, context, etc.)
            mcp_config: Plugin-specific MCP configuration (enabled, type, agent_config)
            registry: The shared MCPRegistry. bootstrap passes it; a caller
                that has none gets a private, empty one.

        The registry has to reach the constructor: Agent.__init__ hands it to
        the ToolExecutionManager, and replacing ``inst.registry`` afterwards
        (what bootstrap did) left that manager holding the throwaway
        registry it was built with -- its fallbacks then resolved tools
        against an empty one.
        """
        inst = agent_cls(name, system_config, mcp_config, registry if registry is not None else MCPRegistry())

        logger.debug(
            "Instantiated agent plugin %s (type=%s) via generic factory",
            name, mcp_config.type
        )
        return inst

    _factory._accepts_registry = True  # bootstrap checks this before passing registry=
    return _factory


__all__ = ["make_agent_plugin_factory"]
