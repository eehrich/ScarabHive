"""agent_eval plugin entry point: the Evals panel. Runs themselves are the CLI's (python -m plugins.agent_eval)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface

from .web import AgentEvalEndpoints


class AgentEvalPlugin(SchemaBasedPluginWebInterface):
    """Web-only plugin: the Evals panel."""

    def __init__(self, name, system_config, server_config):
        super().__init__(name, system_config, server_config)
        self.web_endpoints = AgentEvalEndpoints(self)

    async def call(self, tool: Optional[str] = None, params: Optional[dict] = None, *args: Any, **kwargs: Any):
        """No tools - web UI only"""
        return {"status": "ok", "name": self.name, "type": "web_ui_only", "active": True}

    def get_web_router(self):
        return self.web_endpoints.get_web_router()

    def get_static_assets(self) -> Path:
        """The panel's script, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"

    def get_security_config(self) -> dict:
        """Admins only, under any instance name: a run's suite checks execute
        commands and its records carry agent answers. config/security.yaml's
        rule matches the instance name ``agent_eval`` alone; a renamed or a
        second instance fell to the default role (PluginWebRegistry.effective_policy)."""
        return {"min_role": "admin"}


PLUGIN_FACTORY = AgentEvalPlugin
