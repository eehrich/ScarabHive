"""Log Viewer Plugin Entry Point"""

from __future__ import annotations

from .mcp_server import LogViewerMCPServer
from .endpoints import LogViewerWebEndpoints


class LogViewerHybridPlugin:
    """Hybrid plugin that provides both MCP and web capabilities"""
    
    def __init__(self, name: str, config: dict, ssl_verify: bool = True):
        self.name = name
        self.config = config
        self.ssl_verify = ssl_verify
        
        # Expose configuration properties for compatibility
        self.log_files = config.get('log_files', ['logs/agent.log', 'logs/api.log'])
        self.max_lines = config.get('max_lines', 100)
        self.refresh_interval = config.get('refresh_interval', 1.0)
        
        # Initialize both components
        self.mcp_server = LogViewerMCPServer(name, config, ssl_verify)
        self.web_endpoints = LogViewerWebEndpoints(name, config)
    
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
    
    def get_default_action(self):
        """Delegate to MCP server"""
        return self.mcp_server.get_default_action()
    
    async def call_tool(self, tool_name: str, arguments: dict):
        """Delegate to MCP server - compatibility method"""
        return await self.mcp_server.call_with_status(tool_name, arguments)
    
    # Web Interface methods
    def get_web_router(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_web_router()
    
    def get_static_assets(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_static_assets()
    
    def get_panels(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_panels()
    
    def get_security_config(self):
        """Delegate to web endpoints"""
        return self.web_endpoints.get_security_config()


# Plugin factory for discovery system
PLUGIN_FACTORY = LogViewerHybridPlugin