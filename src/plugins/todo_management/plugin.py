"""
TODO Management Plugin Factory

Hybrid MCP+Hook+Web plugin:
- MCP tools via TodoManagementServer
- Hooks via TodoManagementServer.on_pre_llm_call
- Web UI via TodoWebFactory

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from .server import TodoManagementServer
from .web_endpoints import TodoWebFactory

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig
    from agent_system.hooks import HookContext, HookResult


class TodoManagementHybridPlugin:
    """
    Hybrid plugin combining MCP tools, hooks, and web interface.
    
    - Delegates MCP tools to TodoManagementServer
    - Delegates hooks to server.on_pre_llm_call
    - Provides web router via TodoWebFactory
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
        self.server = TodoManagementServer(name, system_config, mcp_config)
        
        # Create web factory (provides REST API + HTML)
        self.web_factory = TodoWebFactory(self.server)
    
    # =========================================================================
    # MCP Interface (delegate to server)
    # =========================================================================
    
    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs) -> Any:
        """
        Legacy MCP call interface (delegate to server).
        
        Args:
            tool: Tool name
            params: Tool parameters
            
        Returns:
            Tool execution result
        """
        if tool is None and params is None:
            # Status call
            return {
                "status": "ok",
                "name": self.name,
                "description": "TODO Management Plugin",
                "active": True
            }
        
        # Delegate to server's call_with_status
        return await self.server.call_with_status(tool, params or {})
    
    def get_tools(self) -> list[dict[str, Any]]:
        """Expose MCP tools from server"""
        return self.server.get_tools()
    
    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Delegate tool execution to server"""
        return await self.server.call_tool(tool_name, arguments)
    
    # =========================================================================
    # Hook Interface (delegate to server)
    # =========================================================================
    
    async def on_pre_llm_call(
        self,
        context: "HookContext"
    ) -> "HookResult":
        """Delegate hook execution to server"""
        return await self.server.on_pre_llm_call(context)
    
    # =========================================================================
    # Schema Interface (delegate to server)
    # =========================================================================
    
    def get_schema_data(self) -> dict[str, Any]:
        """Delegate schema loading to MCP server (SchemaBasedMCPServer)"""
        return self.server.get_schema_data()
    
    # =========================================================================
    # Web Interface
    # =========================================================================
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router for web UI"""
        return self.web_factory.get_web_router()


def PLUGIN_FACTORY(
    name: str,
    system_config: "AgentSystemConfig",
    mcp_config: "MCPConfig"
) -> TodoManagementHybridPlugin:
    """
    Factory function for creating TodoManagementHybridPlugin instances.
    
    Args:
        name: Plugin name
        system_config: System-level configuration
        mcp_config: MCP client configuration
        
    Returns:
        TodoManagementHybridPlugin instance (MCP+Hook+Web)
    """
    return TodoManagementHybridPlugin(name, system_config, mcp_config)
