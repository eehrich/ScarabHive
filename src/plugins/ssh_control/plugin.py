"""SSH Control plugin factory."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from .mcp_server import SSHControlMCPServer
from .web_endpoints import SSHControlWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class SSHControlHybridPlugin:
    """Hybrid plugin that provides both MCP and web capabilities."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Shared command history for MCP and web UI
        self.command_history = deque(maxlen=1000)
        
        # Initialize MCP server component
        self.mcp_server = SSHControlMCPServer(
            name, 
            system_config, 
            mcp_config,
            command_history=self.command_history
        )
        
        # Initialize web endpoints component
        self.web_endpoints = SSHControlWebEndpoints(
            name,
            system_config,
            mcp_config,
            connection_manager=self.mcp_server.connection_manager,
            command_history=self.command_history
        )
    
    # MCP Server interface methods
    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs):
        """Delegate to MCP server."""
        if tool is None and params is None:
            # Legacy call for status - return basic info
            return {
                "status": "ok",
                "name": self.name,
                "machines": len(self.mcp_server.connection_manager.machines),
                "active": True
            }
        return await self.mcp_server.call_with_status(tool, params or {})
    
    def get_tools(self):
        """Delegate to MCP server."""
        return self.mcp_server.get_tools()
    
    def get_schema_data(self):
        """Delegate to MCP server."""
        return self.mcp_server.get_schema_data()
    
    async def call_tool(self, tool_name: str, arguments: dict):
        """Delegate to MCP server - compatibility method."""
        return await self.mcp_server.call_with_status(tool_name, arguments)
    
    # Web Interface methods
    def get_web_router(self):
        """Delegate to web endpoints."""
        return self.web_endpoints.get_web_router()
    
    def get_static_assets(self):
        """Delegate to web endpoints."""
        return self.web_endpoints.get_static_assets()
    
    def get_panels(self):
        """Delegate to web endpoints."""
        return self.web_endpoints.get_panels()
    
    def get_security_config(self):
        """Delegate to web endpoints."""
        return self.web_endpoints.get_security_config()
    
    async def close(self):
        """Clean up resources."""
        await self.mcp_server.close()


PLUGIN_FACTORY = SSHControlHybridPlugin
