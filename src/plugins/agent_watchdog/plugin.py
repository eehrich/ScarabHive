"""Agent watchdog plugin — factory. See hooks.py."""

from pathlib import Path
from typing import Any

from .hooks import AgentWatchdogPlugin


def PLUGIN_FACTORY(name: str, system_config: Any, server_config: Any) -> AgentWatchdogPlugin:
    """Factory function for plugin discovery."""
    project_root = getattr(system_config, "project_root", None)
    return AgentWatchdogPlugin(Path(__file__).parent, server_config,
                               project_root=Path(project_root) if project_root else None)
