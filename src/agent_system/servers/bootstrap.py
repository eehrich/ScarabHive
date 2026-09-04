"""Server bootstrap for MCP plugin system.

Thin facade over ``agent_system.runtime``: discovering, declaring and building
the configured servers lives there now, in one place instead of in every
entry point. This function keeps its signature and its behaviour -- callers
(tests, the writer tools, InitializationService) do not need to know.
"""
from __future__ import annotations

import logging

from ..config.models import AgentSystemConfig
from ..mcp.base import MCPRegistry
from ..runtime import Runtime, configure_process_singletons

logger = logging.getLogger(__name__)


def bootstrap_servers(config: AgentSystemConfig, registry: MCPRegistry) -> None:
    """Discover and register all configured MCP servers into *registry*.

    Everything enabled is built here (eager), as before.
    """
    configure_process_singletons(config)
    Runtime(config, registry=registry).start()
