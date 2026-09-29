"""SSH Control plugin factory."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from .tool_server import SSHControlToolServer
from .web_endpoints import SSHControlWebEndpoints

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


class SSHControlHybridPlugin:
    """Hybrid plugin that provides both MCP and web capabilities."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.server_config = server_config
        
        # Shared command history for MCP and web UI
        self.command_history = deque(maxlen=1000)
        
        # Initialize tool server component
        self.tool_server = SSHControlToolServer(
            name, 
            system_config, 
            server_config,
            command_history=self.command_history
        )
        
        # Web endpoints component: the panel, on the tool server's connections and history
        self.web_endpoints = SSHControlWebEndpoints(self.tool_server)
    
    # Tool server interface methods
    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs):
        """Delegate to tool server."""
        if tool is None and params is None:
            # Legacy call for status - return basic info
            return {
                "status": "ok",
                "name": self.name,
                "machines": len(self.tool_server.connection_manager.machines),
                "active": True
            }
        return await self.tool_server.call_with_status(tool, params or {})
    
    def get_tools(self):
        """Delegate to tool server."""
        return self.tool_server.get_tools()
    
    def get_schema_data(self):
        """Delegate to tool server."""
        return self.tool_server.get_schema_data()
    
    async def call_tool(self, tool_name: str, arguments: dict):
        """Delegate to tool server - compatibility method."""
        return await self.tool_server.call_with_status(tool_name, arguments)
    
    # Web Interface methods
    def get_web_router(self):
        """Delegate to web endpoints."""
        return self.web_endpoints.get_web_router()
    
    def get_static_assets(self):
        """Delegate to web endpoints."""
        return self.web_endpoints.get_static_assets()
    
    async def close(self):
        """Clean up resources."""
        await self.tool_server.close()

    async def stop_plugin(self) -> None:
        """The name the framework actually calls at shutdown.

        ``close()`` waits for the background commands and closes the pools, but
        nothing reached it: ``plugins/capabilities.stop_plugin`` is the only
        shutdown hook the adapter knows (tool_adapter.py:361), and it looks for
        THIS name. Remote commands and their SSH channels therefore outlived
        the run that started them.
        """
        await self.close()


PLUGIN_FACTORY = SSHControlHybridPlugin
