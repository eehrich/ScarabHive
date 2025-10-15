"""LLM Message Validator Plugin - Factory and exports.

Schema-based hook plugin for message validation before LLM calls.
Hook implementations are in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .hooks import MessageValidatorPlugin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


def PLUGIN_FACTORY(name: str = None, system_config: "AgentSystemConfig" = None, mcp_config: "MCPConfig" = None) -> MessageValidatorPlugin:
    """Factory function to create plugin instance.
    
    Args:
        name: Plugin instance name (ignored)
        system_config: System configuration (ignored)
        mcp_config: MCP configuration (ignored)
    
    Returns:
        MessageValidatorPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return MessageValidatorPlugin(plugin_dir)
