"""Log Viewer Plugin Entry Point"""

from __future__ import annotations
from typing import TYPE_CHECKING

from .mcp_server import LogViewerMCPServer
from .endpoints import LogViewerWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class LogViewerHybridPlugin:
    """Hybrid plugin that provides both MCP and web capabilities"""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.ssl_verify = getattr(system_config.network, 'ssl_verify', True) if hasattr(system_config, 'network') and system_config.network else True
        
        # Expose configuration properties for compatibility
        default_log_files = [
            'logs/agent-cli.log',
            'logs/api.log',
            'logs/cli.log',
            'logs/http_server.log',
            'logs/llm_router.log'
        ]
        self.log_files = getattr(mcp_config, 'log_files', default_log_files)
        self.max_lines = getattr(mcp_config, 'max_lines', 100)
        self.refresh_interval = getattr(mcp_config, 'refresh_interval', 1.0)
        
        # Initialize both components with new signature
        self.mcp_server = LogViewerMCPServer(name, system_config, mcp_config)
        self.web_endpoints = LogViewerWebEndpoints(name, system_config, mcp_config, self.mcp_server)
    
    # MCP Server interface methods
    async def call(self, tool: str = None, params: dict = None, *args, **kwargs):
        """Delegate to MCP server"""
        if tool is None and params is None:
            # Legacy call for status - return basic info
            return {
                "status": "ok", 
                "name": self.name,
                "log_files": self.log_files,
                "active": True
            }
        return await self.mcp_server.call_with_status(tool, params or {})
    
    def get_tools(self):
        """Delegate to MCP server"""
        return self.mcp_server.get_tools()
    
    def get_schema_data(self):
        """Delegate to MCP server"""
        return self.mcp_server.get_schema_data()
        
    async def call_tool(self, tool_name: str, arguments: dict):
        """Delegate to MCP server - compatibility method"""
        return await self.mcp_server.call_with_status(tool_name, arguments)
    
    # Web Interface methods
    def get_web_router(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_web_router()


PLUGIN_FACTORY = LogViewerHybridPlugin