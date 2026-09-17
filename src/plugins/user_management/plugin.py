"""User Management Plugin Entry Point"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
from .endpoints import UserManagementWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


class UserManagementPlugin(SchemaBasedPluginWebInterface):
    """Web-only plugin: the Users panel."""

    def __init__(self, name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig"):
        super().__init__(name, system_config, server_config)
        auth = getattr(system_config, "auth", None)
        self.auth_enabled = bool(auth and auth.enabled)
        self.web_endpoints = UserManagementWebEndpoints(self)

    async def call(self, tool: str = None, params: dict = None, *args, **kwargs):
        """No tools - web UI only"""
        return {
            "status": "ok",
            "name": self.name,
            "type": "web_ui_only",
            "auth_enabled": self.auth_enabled,
            "active": self.auth_enabled
        }

    def get_web_router(self):
        return self.web_endpoints.get_web_router()

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"


PLUGIN_FACTORY = UserManagementPlugin
