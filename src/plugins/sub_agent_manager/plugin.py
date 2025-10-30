"""Sub-Agent Manager Plugin Factory.

This module provides the PLUGIN_FACTORY function required by the plugin system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

from plugins.sub_agent_manager.server import SubAgentManagerServer


def PLUGIN_FACTORY(name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> SubAgentManagerServer:
    """Factory function for creating SubAgentManagerServer instances.
    
    Args:
        name: Plugin instance name
        system_config: System-wide configuration
        mcp_config: Plugin-specific MCP configuration
        
    Returns:
        SubAgentManagerServer: Configured server instance
    """
    return SubAgentManagerServer(name, system_config, mcp_config)
