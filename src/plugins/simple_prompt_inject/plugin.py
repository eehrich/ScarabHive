"""Simple Prompt Inject Plugin - Factory and exports.

Schema-based hooks-only plugin that injects configurable prompt text
into conversations before LLM calls.
Hook implementation is in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .hooks import SimplePromptInjectPlugin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


def PLUGIN_FACTORY(
    name: str = None,
    system_config: "AgentSystemConfig" = None,
    server_config: "ToolServerConfig" = None,
) -> SimplePromptInjectPlugin:
    """Factory function to create plugin instance.

    Args:
        name: Server instance name; marks the injected messages
        system_config: System configuration (ignored, for compatibility)
        server_config: tool server configuration (contains config from plugins.yaml)

    Returns:
        SimplePromptInjectPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return SimplePromptInjectPlugin(plugin_dir, server_config, name)
