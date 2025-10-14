"""Request Logger Plugin - Factory and exports.

Schema-based hooks-only plugin that logs agent requests and responses.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .hooks import RequestLoggerPlugin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


def PLUGIN_FACTORY(name: str = None, system_config: "AgentSystemConfig" = None, mcp_config: "MCPConfig" = None) -> RequestLoggerPlugin:
    """Factory function to create plugin instance.
    
    Args:
        name: Plugin name (ignored, for compatibility)
        system_config: System configuration (ignored, for compatibility)
        mcp_config: MCP configuration (ignored, for compatibility)
    
    Returns:
        RequestLoggerPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return RequestLoggerPlugin(plugin_dir)
