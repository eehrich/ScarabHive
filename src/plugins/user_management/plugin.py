"""User Management Plugin Entry Point"""

from __future__ import annotations
from typing import TYPE_CHECKING

from .endpoints import UserManagementWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class UserManagementPlugin:
    """Web-only plugin for user management UI"""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Check if auth is enabled
        self.auth_enabled = getattr(system_config.auth, 'enabled', False) if hasattr(system_config, 'auth') and system_config.auth else False
        
        # Configuration from plugin.yaml or mcp_config
        self.items_per_page = getattr(mcp_config, 'items_per_page', 20)
        self.allow_self_delete = getattr(mcp_config, 'allow_self_delete', False)
        self.show_api_keys = getattr(mcp_config, 'show_api_keys', True)
        
        # Initialize web endpoints
        self.web_endpoints = UserManagementWebEndpoints(name, system_config, mcp_config)
    
    # MCP Server interface methods (no-op since this is web-only)
    async def call(self, tool: str = None, params: dict = None, *args, **kwargs):
        """No MCP tools - web UI only"""
        return {
            "status": "ok", 
            "name": self.name,
            "type": "web_ui_only",
            "auth_enabled": self.auth_enabled,
            "active": self.auth_enabled
        }
    
    def get_tools(self):
        """No MCP tools - web UI only"""
        return []
    
    def get_default_action(self):
        """No default action"""
        return None
    
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


PLUGIN_FACTORY = UserManagementPlugin
