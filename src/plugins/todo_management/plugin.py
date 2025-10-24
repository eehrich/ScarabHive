"""
TODO Management Plugin Factory

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .server import TodoManagementServer

if TYPE_CHECKING:
    from agent_system.mcp.client import MCPConfig


def PLUGIN_FACTORY(
    name: str,
    system_config: dict,
    mcp_config: "MCPConfig"
) -> TodoManagementServer:
    """
    Factory function for creating TodoManagementServer instances.
    
    Args:
        name: Plugin name
        system_config: System-level configuration
        mcp_config: MCP client configuration
        
    Returns:
        TodoManagementServer instance
    """
    return TodoManagementServer(name, system_config, mcp_config)
