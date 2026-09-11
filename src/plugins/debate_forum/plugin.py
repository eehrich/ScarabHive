"""Debate Forum Plugin - Factory and exports.

Hybrid MCP+Web plugin:
- MCP tools via DebateForumServer (create_channel, post_message, get_thread, conclude, list_channels)
- Web UI via DebateForumWebFactory (Discord-style panel with channels and chat messages)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

from .database import DebateForumDB
from .server import DebateForumServer
from .hooks import DebateForumHooks
from .web_endpoints import DebateForumWebFactory

logger = logging.getLogger(__name__)


class DebateForumHybridPlugin:
    """Hybrid plugin providing MCP tools + Web UI for debate forums.

    - Delegates MCP tool calls to DebateForumServer
    - Provides web router via DebateForumWebFactory
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig",
    ):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config

        # Resolve DB path from plugin config
        config: dict[str, Any] = {}
        if mcp_config and hasattr(mcp_config, "config") and mcp_config.config:
            config = mcp_config.config

        db_path = config.get("db_path")
        if not db_path:
            db_path = str(Path("data") / "debate_forum" / "forum.db")

        # Create shared database
        self._db = DebateForumDB(db_path)

        # Create MCP server (provides tools)
        self.server = DebateForumServer(
            name, system_config, mcp_config, db=self._db,
            min_message_length=config.get("min_message_length", 50),
        )

        # Create web factory (provides REST API + HTML panel)
        self.web_factory = DebateForumWebFactory(
            db=self._db, name=name, server=self.server
        )

        # Create hooks plugin (schema-based), pass plugin config so plugins.yaml values take effect
        plugin_dir = Path(__file__).parent
        self.hooks_plugin = DebateForumHooks(plugin_dir, self._db, plugin_config=config,
                                             tool_prefix=name)

        logger.info("DebateForumHybridPlugin initialized: db=%s", db_path)

    # ── MCP Interface (delegate to server) ────────────────────

    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs) -> Any:
        return await self.server.call(tool, params, *args, **kwargs)

    async def call_with_status(self, tool: str, params: dict[str, Any]) -> Any:
        return await self.server.call_with_status(tool, params)

    async def list_tools(self) -> list[dict]:
        return await self.server.list_tools()

    def get_schema_data(self) -> dict[str, Any]:
        return self.server.get_schema_data()

    # ── Web Interface (delegate to web_factory) ───────────────

    def get_web_router(self) -> APIRouter | None:
        return self.web_factory.get_web_router()

    # ── Hook Interface (delegate to hooks_plugin) ─────────────

    def get_hooks(self):
        """Get hooks from the hooks plugin."""
        return self.hooks_plugin.get_hooks()

    async def execute_hook(self, hook_type, context):
        """Execute hook via the hooks plugin."""
        return await self.hooks_plugin.execute_hook(hook_type, context)


def PLUGIN_FACTORY(
    name: str,
    system_config: "AgentSystemConfig",
    mcp_config: "MCPConfig",
) -> DebateForumHybridPlugin:
    return DebateForumHybridPlugin(name, system_config, mcp_config)
