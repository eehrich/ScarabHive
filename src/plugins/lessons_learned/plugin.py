"""
Lessons Learned Plugin Factory

Hybrid MCP+Hook+Web plugin:
- tools via LessonsLearnedServer
- Hooks via LessonsLearnedServer.on_pre_llm_call / on_session_end
- Web UI via LessonsWebFactory

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from .server import LessonsLearnedServer
from .web_endpoints import LessonsWebFactory

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig
    from agent_system.hooks import HookContext, HookResult


class LessonsLearnedHybridPlugin:
    """
    Hybrid plugin combining tools, hooks, and web interface.

    - Delegates tools to LessonsLearnedServer
    - Delegates hooks to server.on_pre_llm_call / on_session_end
    - Provides web router via LessonsWebFactory
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        server_config: "ToolServerConfig",
    ):
        self.name = name
        self.system_config = system_config
        self.server_config = server_config

        # Create tool server instance (provides tools + hooks)
        self.server = LessonsLearnedServer(name, system_config, server_config)

        # Create web factory (provides REST API + HTML)
        self.web_factory = LessonsWebFactory(self.server)

    # =========================================================================
    # Tool interface (delegate to server)
    # =========================================================================

    async def call(self, tool: str | None = None, params: dict | None = None, *args: Any, **kwargs: Any) -> Any:
        """Legacy tool call interface (delegate to server)."""
        if tool is None and params is None:
            return {
                "status": "ok",
                "name": self.name,
                "description": "Lessons Learned Plugin",
                "active": True,
            }
        return await self.server.call_with_status(tool, params or {})

    def get_tools(self) -> list[dict[str, Any]]:
        """Expose tools from server."""
        return self.server.get_tools()

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Delegate tool execution to server."""
        return await self.server.call_tool(tool_name, arguments)

    # =========================================================================
    # Hook Interface (delegate to server)
    # =========================================================================

    async def on_pre_llm_call(self, context: "HookContext") -> "HookResult":
        """Inject active lessons into system prompt."""
        return await self.server.on_pre_llm_call(context)

    async def on_session_end(self, context: "HookContext") -> "HookResult":
        """Extract lessons from conversation at session end."""
        return await self.server.on_session_end(context)

    # =========================================================================
    # Schema Interface (delegate to server)
    # =========================================================================

    def get_schema_data(self) -> dict[str, Any]:
        """Delegate schema loading to tool server (SchemaBasedToolServer)."""
        return self.server.get_schema_data()

    # =========================================================================
    # Web Interface
    # =========================================================================

    def get_web_router(self) -> APIRouter:
        """Return FastAPI router for web UI."""
        return self.web_factory.get_web_router()

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"


def PLUGIN_FACTORY(
    name: str,
    system_config: "AgentSystemConfig",
    server_config: "ToolServerConfig",
) -> LessonsLearnedHybridPlugin:
    """
    Factory function for creating LessonsLearnedHybridPlugin instances.

    Args:
        name: Plugin name
        system_config: System-level configuration
        server_config: MCP client configuration

    Returns:
        LessonsLearnedHybridPlugin instance (MCP+Hook+Web)
    """
    return LessonsLearnedHybridPlugin(name, system_config, server_config)
