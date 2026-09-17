"""Context Summarizer Plugin Factory.

Hybrid MCP+Hook+Web plugin:
- tools via ContextSummarizerServer
- Hooks via ContextSummarizerServer.on_pre_llm_call
- Web UI via ContextSummarizerWebFactory

This module provides the PLUGIN_FACTORY function required by the plugin system.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig
    from agent_system.hooks import HookContext, HookResult

from plugins.context_summarizer.server import ContextSummarizerServer
from plugins.context_summarizer.web_endpoints import ContextSummarizerWebFactory


class ContextSummarizerHybridPlugin:
    """
    Hybrid plugin combining tools, hooks, and web interface.

    - Delegates tools to ContextSummarizerServer
    - Delegates hooks to server.on_pre_llm_call
    - Provides web router via ContextSummarizerWebFactory
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        server_config: "ToolServerConfig"
    ):
        self.name = name
        self.system_config = system_config
        self.server_config = server_config

        # Create tool server instance (provides tools + hooks)
        self.server = ContextSummarizerServer(name, system_config, server_config)

        # Create web factory (provides REST API + HTML)
        self.web_factory = ContextSummarizerWebFactory(
            self.server,
            self.server.summarization_history
        )

    # =========================================================================
    # Tool interface (delegate to server)
    # =========================================================================

    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs) -> Any:
        """Legacy tool call interface (delegate to server)."""
        return await self.server.call(tool, params, *args, **kwargs)

    async def call_with_status(self, tool: str, params: dict[str, Any]) -> Any:
        """Call with status context (delegate to server)."""
        return await self.server.call_with_status(tool, params)

    async def list_tools(self) -> list[dict]:
        """List available tools (delegate to server)."""
        return await self.server.list_tools()

    def get_schema_data(self) -> dict[str, Any]:
        """Get schema data (delegate to server)."""
        return self.server.get_schema_data()

    # =========================================================================
    # Hook Interface (delegate to server)
    # =========================================================================

    async def on_pre_llm_call(self, context: "HookContext") -> "HookResult":
        """Pre-LLM hook for automatic context summarization (delegate to server)."""
        return await self.server.on_pre_llm_call(context)

    # =========================================================================
    # Web Interface (delegate to web_factory)
    # =========================================================================

    def get_web_router(self) -> APIRouter | None:
        """Get FastAPI router for web endpoints."""
        return self.web_factory.get_web_router()

    def get_static_assets(self) -> Path:
        """The panel's script and stylesheet, served under /plugins/<name>/static/."""
        return Path(__file__).parent / "static"


def PLUGIN_FACTORY(name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig") -> ContextSummarizerHybridPlugin:
    """Factory function for creating ContextSummarizerHybridPlugin instances.

    Args:
        name: Plugin instance name
        system_config: System-wide configuration
        server_config: Plugin-specific tool server configuration

    Returns:
        ContextSummarizerHybridPlugin: Configured hybrid plugin instance
    """
    return ContextSummarizerHybridPlugin(name, system_config, server_config)
