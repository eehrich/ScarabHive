"""Plugin factory helpers.

This module centralizes tiny helpers so individual plugin directories can stay
minimal (ideally just: from .server import X; PLUGIN_FACTORY = make_agent_plugin_factory(X)).
"""
from __future__ import annotations
from typing import Callable, Type
import logging

from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent
from agent_system.config.models import AgentConfig

logger = logging.getLogger(__name__)


def make_agent_plugin_factory(agent_cls: Type[Agent]) -> Callable[[str, AgentConfig], Agent]:
    """Return a standard PLUGIN_FACTORY callable for an Agent subclass.

    SIMPLIFIED: Always expects a fully prepared AgentConfig object.
    No more complex type checking or dict parsing.
    SSL verification is read from config.network.ssl_verify.

    Signature produced: (name: str, config: AgentConfig) -> Agent
    """
    def _factory(name: str, config: AgentConfig) -> Agent:
        # MODERN: Clean constructor - no legacy parameters, no ssl_verify
        registry = MCPRegistry()
        
        # Standard modern constructor: Agent(name, config, registry)
        # All configuration (including SSL settings) is in the AgentConfig
        inst = agent_cls(name, config, registry)
        
        logger.debug("Instantiated agent plugin %s via generic factory with server config", name)
        return inst
    return _factory

__all__ = ["make_agent_plugin_factory"]
