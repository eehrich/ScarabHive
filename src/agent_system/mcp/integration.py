"""
MCP Integration Module

Integrates MCP functionality with the AgentSystem:
- Exposes plugins as MCP servers
- Connects to external MCP servers as clients
- Provides unified interface for MCP operations
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI

from .client import MCPClientManager
from .plugin_adapter import plugin_mcp_registry
from .http_server import MCPHTTPServer
from .config import MCPConfigManager
from .security import configure_security

logger = logging.getLogger(__name__)


class MCPIntegration:
    """Main integration class for MCP functionality"""

    def __init__(self, app: Optional[FastAPI] = None, config: Optional[Dict[str, Any]] = None):
        # Load configuration
        self.config_manager = MCPConfigManager()
        self.mcp_config = self.config_manager.load_config(config)

        # Configure security if config provided
        if config:
            configure_security(config)

        self.client_manager = MCPClientManager()
        self.plugin_registry = plugin_mcp_registry
        self.http_server = MCPHTTPServer(app)
        self.initialized = False

    async def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize MCP integration from configuration"""
        if self.initialized:
            return

        mcp_config = config.get('mcp', {})

        # Load external servers from new configuration format
        await self._setup_external_servers_from_config()

        # Discover and register plugins
        plugin_dirs = mcp_config.get('plugin_dirs', ['src/plugins'])
        self.plugin_registry.discover_plugins(plugin_dirs)

        # Register enabled plugins as MCP servers
        enabled_servers = mcp_config.get('enabled_servers', [])
        servers_config = mcp_config.get('servers', {})

        await self.plugin_registry.register_from_config(enabled_servers, servers_config)

        # Register plugin servers with HTTP server
        for server_name in self.plugin_registry.list_servers():
            server = self.plugin_registry.get_server(server_name)
            if server:
                self.http_server.register_server(server_name, server)

        # Connect to external MCP servers
        external_servers = mcp_config.get('external_servers', {})
        for server_name, server_config in external_servers.items():
            try:
                await self.client_manager.add_client(server_name, server_config)
                logger.info(f"Connected to external MCP server: {server_name}")
            except Exception as e:
                logger.error(f"Failed to connect to external MCP server {server_name}: {e}")

        self.initialized = True
        logger.info("MCP integration initialized successfully")

    async def shutdown(self) -> None:
        """Shutdown MCP integration"""
        logging.getLogger(__name__).debug("MCPIntegration.shutdown() called")
        await self.client_manager.close_all()
        logging.getLogger(__name__).debug("MCPIntegration.shutdown() completed")
        logger.info("MCP integration shut down")

    async def _setup_external_servers_from_config(self) -> None:
        """Setup external servers from new configuration format"""
        for server_name, server_config in self.mcp_config.servers.items():
            if not server_config.enabled:
                logger.debug(f"Skipping disabled MCP server: {server_name}")
                continue

            try:
                # Create client config for the server
                client_config = {
                    "transport": server_config.transport_type,
                    "url": server_config.url,
                    "client_name": f"AgentSystem-{server_name}",
                    "timeout": server_config.timeout,
                    "ssl_verify": server_config.ssl_verify
                }

                # Add initialization options if present
                if server_config.initialization_options:
                    client_config["initialization_options"] = server_config.initialization_options

                await self.client_manager.add_client(server_name, client_config)
                logger.info(f"Connected to external MCP server: {server_name} at {server_config.url}")
            except Exception as e:
                logger.error(f"Failed to connect to MCP server {server_name}: {e}")

    def get_app(self) -> FastAPI:
        """Get the FastAPI app with MCP endpoints"""
        return self.http_server.app

    async def list_all_tools(self) -> Dict[str, Dict[str, List[Any]]]:
        """List all available tools from plugins and external servers"""
        result: Dict[str, Dict[str, List[Any]]] = {
            "plugins": {},
            "external_servers": {}
        }

        # Get tools from plugins
        plugin_tools = await self.plugin_registry.get_all_tools()
        for plugin_name, tools in plugin_tools.items():
            result["plugins"][plugin_name] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema
                }
                for tool in tools
            ]

        # Get tools from external servers
        external_tools = await self.client_manager.list_all_tools()
        for server_name, tools in external_tools.items():
            result["external_servers"][server_name] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema
                }
                for tool in tools
            ]

        return result

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any], server_type: str = "auto") -> Any:
        """Call a tool on a server (plugin or external)"""
        if server_type == "auto":
            # Try plugin first, then external
            if server_name in self.plugin_registry.list_servers():
                server_type = "plugin"
            elif server_name in self.client_manager.list_clients():
                server_type = "external"
            else:
                raise Exception(f"Unknown server: {server_name}")

        if server_type == "plugin":
            return await self.plugin_registry.call_plugin_tool(server_name, tool_name, arguments)
        elif server_type == "external":
            return await self.client_manager.call_tool(server_name, tool_name, arguments)
        else:
            raise Exception(f"Invalid server type: {server_type}")

    def get_server_info(self) -> Dict[str, Any]:
        """Get information about available servers"""
        return {
            "plugins": {
                "available": self.plugin_registry.list_available_plugins(),
                "registered": self.plugin_registry.list_servers()
            },
            "external_servers": self.client_manager.list_clients(),
            "http_server": {
                "endpoints": ["/mcp", "/mcp/servers", "/mcp/servers/{server_name}/tools"]
            }
        }

    async def register_plugin(self, name: str, config: Optional[Dict[str, Any]] = None) -> None:
        """Register a plugin as an MCP server"""
        await self.plugin_registry.register_plugin(name, config)

        # Add to HTTP server
        server = self.plugin_registry.get_server(name)
        if server:
            self.http_server.register_server(name, server)

    async def unregister_plugin(self, name: str) -> None:
        """Unregister a plugin MCP server"""
        await self.plugin_registry.unregister_plugin(name)
        self.http_server.unregister_server(name)

    async def add_external_server(self, name: str, config: Dict[str, Any]) -> None:
        """Add an external MCP server"""
        await self.client_manager.add_client(name, config)

    async def remove_external_server(self, name: str) -> None:
        """Remove an external MCP server"""
        await self.client_manager.remove_client(name)


# Global MCP integration instance
mcp_integration: Optional[MCPIntegration] = None


def get_mcp_integration(app: Optional[FastAPI] = None) -> MCPIntegration:
    """Get or create the global MCP integration instance"""
    global mcp_integration
    if mcp_integration is None:
        mcp_integration = MCPIntegration(app)
    return mcp_integration


async def initialize_mcp(config: Dict[str, Any], app: Optional[FastAPI] = None) -> MCPIntegration:
    """Initialize MCP integration with configuration"""
    integration = get_mcp_integration(app)
    await integration.initialize(config)
    return integration


async def shutdown_mcp() -> None:
    """Shutdown MCP integration"""
    global mcp_integration
    if mcp_integration:
        await mcp_integration.shutdown()
        mcp_integration = None