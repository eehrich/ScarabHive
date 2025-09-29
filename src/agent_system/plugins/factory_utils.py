"""Plugin factory helpers.

This module centralizes tiny helpers so individual plugin directories can stay
minimal (ideally just: from .server import X; PLUGIN_FACTORY = make_agent_plugin_factory(X)).
"""
from __future__ import annotations
from typing import Any, Callable, Type
import logging

from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent

logger = logging.getLogger(__name__)


def make_agent_plugin_factory(agent_cls: Type[Agent]) -> Callable[[str, Any | None], Agent]:
    """Return a standard PLUGIN_FACTORY callable for an Agent subclass.

    It expects bootstrap to have already injected a fully prepared AgentConfig
    (as object) either directly as `config` (AgentConfig instance) or inside
    a dict under key `parent_agent_config`.

    Signature produced: (name: str, config: Any | None, ssl_verify: bool = True) -> Agent
    Matching existing discovery expectations.
    """
    def _factory(name: str, config: Any | None = None, ssl_verify: bool = True) -> Agent:
        from agent_system.config.models import AgentConfig  # local import to avoid cycles
        cfg_obj = None
        if isinstance(config, AgentConfig):
            cfg_obj = config
        elif isinstance(config, dict):
            parent = config.get("parent_agent_config")
            if isinstance(parent, AgentConfig):
                cfg_obj = parent
            else:
                raise ValueError(
                    f"Plugin '{name}': expected full AgentConfig (parent_agent_config). Got partial dict; aborting."
                )
        else:
            raise ValueError(
                f"Plugin '{name}': unsupported config type {type(config).__name__}; expected AgentConfig or dict"
            )
        registry = MCPRegistry()
        inst = agent_cls(name, cfg_obj, registry, ssl_verify=ssl_verify)
        logger.debug("Instantiated agent plugin %s via generic factory", name)
        return inst
    return _factory

__all__ = ["make_agent_plugin_factory"]
