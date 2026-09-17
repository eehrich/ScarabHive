"""Log Viewer Plugin Entry Point"""

from __future__ import annotations
from typing import TYPE_CHECKING

from .tool_server import LogViewerToolServer
from .endpoints import LogViewerWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


class LogViewerHybridPlugin:
    """Hybrid plugin that provides both MCP and web capabilities"""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", server_config: "ToolServerConfig"):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.server_config = server_config
        self.ssl_verify = getattr(system_config.network, 'ssl_verify', True) if hasattr(system_config, 'network') and system_config.network else True
        
        # Initialize both components with new signature
        self.tool_server = LogViewerToolServer(name, system_config, server_config)
        self.log_files = self.tool_server.log_files
        self.web_endpoints = LogViewerWebEndpoints(name, system_config, server_config, self.tool_server)
    
    # Tool server interface methods
    async def call(self, tool: str = None, params: dict = None, *args, **kwargs):
        """Delegate to tool server"""
        if tool is None and params is None:
            # Legacy call for status - return basic info
            return {
                "status": "ok", 
                "name": self.name,
                "log_files": self.log_files,
                "active": True
            }
        return await self.tool_server.call_with_status(tool, params or {})
    
    def get_tools(self):
        """Delegate to tool server"""
        return self.tool_server.get_tools()
    
    def get_schema_data(self):
        """Delegate to tool server"""
        return self.tool_server.get_schema_data()
        
    async def call_tool(self, tool_name: str, arguments: dict):
        """Delegate to tool server - compatibility method"""
        return await self.tool_server.call_with_status(tool_name, arguments)
    
    # Web Interface methods
    def get_web_router(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_web_router()

    def get_static_assets(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_static_assets()


PLUGIN_FACTORY = LogViewerHybridPlugin