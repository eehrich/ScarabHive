"""Context Engineer Plugin Factory.

Hybrid MCP+Hook+Web plugin:
- MCP tools via ContextEngineerServer
- Hooks via ContextEngineerServer.on_pre_llm_call
- Web UI via ContextEngineerWebFactory

This module provides the PLUGIN_FACTORY function required by the plugin system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig
    from agent_system.hooks import HookContext, HookResult

from plugins.context_engineer.server import ContextEngineerServer
from plugins.context_engineer.web_endpoints import ContextEngineerWebFactory


class ContextEngineerHybridPlugin:
    """Hybrid plugin combining MCP tools, hooks, and web interface.
    
    - Delegates MCP tools to ContextEngineerServer
    - Delegates hooks to server.on_pre_llm_call
    - Provides web router via ContextEngineerWebFactory
    """
    
    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig"
    ):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Create MCP server instance (provides tools + hooks)
        self.server = ContextEngineerServer(name, system_config, mcp_config)
        
        # Create web factory (provides REST API + HTML panel)
        self.web_factory = ContextEngineerWebFactory(
            self.server,
            self.server.stats_history
        )
    
    # =========================================================================
    # MCP Interface (delegate to server)
    # =========================================================================
    
    async def call(
        self,
        tool: str | None = None,
        params: dict | None = None,
        *args,
        **kwargs
    ) -> Any:
        """Legacy MCP call interface (delegate to server)."""
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
        """Pre-LLM hook for automatic context engineering (delegate to server)."""
        return await self.server.on_pre_llm_call(context)
    
    # =========================================================================
    # Web Interface (delegate to web_factory)
    # =========================================================================
    
    def get_web_router(self) -> APIRouter | None:
        """Get FastAPI router for web endpoints."""
        return self.web_factory.get_web_router()


def PLUGIN_FACTORY(
    name: str,
    system_config: "AgentSystemConfig",
    mcp_config: "MCPConfig"
) -> ContextEngineerHybridPlugin:
    """Factory function for creating ContextEngineerHybridPlugin instances.
    
    Args:
        name: Plugin instance name
        system_config: System-wide configuration
        mcp_config: Plugin-specific MCP configuration
        
    Returns:
        ContextEngineerHybridPlugin: Configured hybrid plugin instance
    """
    return ContextEngineerHybridPlugin(name, system_config, mcp_config)
