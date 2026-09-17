"""Agent Editor plugin entry point: a web-only panel that edits agent entries in their YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

from .endpoints import AgentEditorWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class AgentEditorPlugin(SchemaBasedPluginWebInterface):
    """Web-only plugin: the Agent Editor panel."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        super().__init__(name, system_config, mcp_config)
        auth = getattr(system_config, "auth", None)
        self.auth_enabled = bool(auth and auth.enabled)
        self.web_endpoints = AgentEditorWebEndpoints(self)

    def get_web_router(self):
        return self.web_endpoints.get_web_router()

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"


PLUGIN_FACTORY = AgentEditorPlugin
