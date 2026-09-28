"""project_instructions plugin: factory.

Hooks-only plugin that puts the project's AGENTS.md in front of the model,
read once per session. Hook in hooks.py, configuration in schema.yaml.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .hooks import ProjectInstructionsPlugin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


def PLUGIN_FACTORY(
    name: str = None,
    system_config: "AgentSystemConfig" = None,
    server_config: "ToolServerConfig" = None,
) -> ProjectInstructionsPlugin:
    """Called as factory(name, system_config, server_config); only the server config is read."""
    return ProjectInstructionsPlugin(Path(__file__).parent, server_config)
