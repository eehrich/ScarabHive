"""Sub-Agent Manager Plugin Factory.

Hybrid MCP+Hook+Web plugin:
- MCP tools via SubAgentManagerServer
- Hooks via SubAgentManagerServer.on_pre_llm_call
- Web UI via SubAgentManagerWebFactory

This module provides the PLUGIN_FACTORY function required by the plugin system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig
    from agent_system.hooks import HookContext, HookResult

from plugins.sub_agent_manager.server import SubAgentManagerServer
from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory


class SubAgentManagerHybridPlugin:
    """
    Hybrid plugin combining MCP tools, hooks, and web interface.
    
    - Delegates MCP tools to SubAgentManagerServer
    - Delegates hooks to server.on_pre_llm_call
    - Provides web router via SubAgentManagerWebFactory
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
        self.server = SubAgentManagerServer(name, system_config, mcp_config)
        
        # Create web factory (provides REST API + HTML)
        self.web_factory = SubAgentManagerWebFactory(self.server)
    
    # =========================================================================
    # MCP Interface (delegate to server)
    # =========================================================================
    
    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs) -> Any:
        """Legacy MCP call interface (delegate to server)."""
        return await self.server.call(tool, params, *args, **kwargs)
    
    async def call_with_status(self, tool: str, params: dict[str, Any]) -> Any:
        """Call with status context (delegate to server)."""
        return await self.server.call_with_status(tool, params)
    
    def list_tools(self) -> list[dict]:
        """List available tools (delegate to server)."""
        return self.server.list_tools()
    
    # =========================================================================
    # Hook Interface (delegate to server)
    # =========================================================================
    
    async def on_pre_llm_call(self, context: "HookContext") -> "HookResult":
        """Pre-LLM hook to inject sub-agent context (delegate to server)."""
        return await self.server.on_pre_llm_call(context)
    
    # =========================================================================
    # Web Interface (delegate to web_factory)
    # =========================================================================
    
    def get_web_router(self) -> APIRouter | None:
        """Get FastAPI router for web endpoints."""
        return self.web_factory.get_web_router()


def PLUGIN_FACTORY(name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig") -> SubAgentManagerHybridPlugin:
    """Factory function for creating SubAgentManagerHybridPlugin instances.
    
    Args:
        name: Plugin instance name
        system_config: System-wide configuration
        mcp_config: Plugin-specific MCP configuration
        
    Returns:
        SubAgentManagerHybridPlugin: Configured hybrid plugin instance
    """
    return SubAgentManagerHybridPlugin(name, system_config, mcp_config)

