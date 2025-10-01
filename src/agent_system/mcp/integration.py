"""
MCP Integration Module

Integrates MCP functionality with the AgentSystem:
- Exposes plugins as MCP servers
- Connects to external MCP servers as clients
- Provides unified interface for MCP operations
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI

from .client import MCPClientManager
from ..plugins.mcp_adapter import plugin_mcp_registry
from .http_server import MCPHTTPServer
from .security import configure_security
from ..config.models import AgentSystemConfig, RemoteMCPConfig, MCPSystemConfig

logger = logging.getLogger(__name__)


class MCPIntegration:
    """Main integration class for MCP functionality"""

    def __init__(self, app: Optional[FastAPI] = None, config: AgentSystemConfig = None):
        if config is None:
            raise ValueError("AgentSystemConfig is required for MCPIntegration initialization")

        # Store MCP system config
        self.mcp_system_config: MCPSystemConfig = config.mcp_system if config and config.mcp_system else MCPSystemConfig()

        # Configure security with provided config
        configure_security(config)

        self.client_manager = MCPClientManager()
        self.plugin_registry = plugin_mcp_registry
        self.http_server = MCPHTTPServer(app)
        self.initialized = False
        self.configured_external_servers: Dict[str, RemoteMCPConfig] = {}  # Type-safe config storage

        # Tool list caching to reduce external server queries
        self._tools_cache: Optional[Dict[str, Dict[str, List[Any]]]] = None
        self._tools_cache_time = 0.0
        self._tools_cache_ttl = 30.0  # Cache for 30 seconds


    async def initialize(self, config: AgentSystemConfig) -> None:
        """Initialize MCP integration from configuration"""
        if self.initialized:
            return

        # Access MCP system config
        mcp_config = config.mcp_system if config.mcp_system else MCPSystemConfig()

        # Update cache TTL from configuration
        if mcp_config.external_servers and mcp_config.external_servers.cache:
            self._tools_cache_ttl = mcp_config.external_servers.cache.tool_list_ttl
        logger.debug(f"MCP tools cache TTL set to {self._tools_cache_ttl}s")

        # Set cache TTL on client manager as well
        self.client_manager.set_cache_ttl(self._tools_cache_ttl)

        # Discover and register plugins
        plugin_dirs = ['src/plugins']  # Default plugin directory
        self.plugin_registry.discover_plugins(plugin_dirs)

        # Register enabled plugins as MCP servers
        # Use servers from mcp_config.servers (Dict[str, MCPConfig])
        servers_config = mcp_config.servers if mcp_config.servers else {}
        enabled_servers = [name for name, server_cfg in servers_config.items() if server_cfg.enabled]
        
        logger.debug(f"MCP integration - enabled servers: {enabled_servers}")
        logger.debug(f"MCP integration - servers_config type: {type(servers_config)}")

        # Pass complete AgentSystemConfig for plugin registration
        await self.plugin_registry.register_from_config(enabled_servers, servers_config, config)

        # Register plugin servers with HTTP server
        for server_name in self.plugin_registry.list_servers():
            server = self.plugin_registry.get_server(server_name)
            if server:
                self.http_server.register_server(server_name, server)

        # Connect to external MCP servers from new config format
        if mcp_config.external_servers and mcp_config.external_servers.remote_servers:
            remote_servers = mcp_config.external_servers.remote_servers
            
            # Only store enabled servers
            self.configured_external_servers = {
                name: server_config
                for name, server_config in remote_servers.items()
                if server_config.enabled
            }
            
            # Connect to enabled servers
            for server_name, server_config in remote_servers.items():
                if not server_config.enabled:
                    logger.debug(f"Skipping disabled external MCP server: {server_name}")
                    continue
                    
                try:
                    await self.client_manager.add_client(server_name, server_config)
                    logger.info(f"Connected to external MCP server: {server_name}")
                except Exception as e:
                    logger.debug(f"Failed to connect to external MCP server {server_name}: {e}")

        self.initialized = True
        logger.info("MCP integration initialized successfully")

    async def shutdown(self) -> None:
        """Shutdown MCP integration"""
        logging.getLogger(__name__).debug("MCPIntegration.shutdown() called")
        await self.client_manager.close_all()
        logging.getLogger(__name__).debug("MCPIntegration.shutdown() completed")
        logger.info("MCP integration shut down")

    def get_app(self) -> FastAPI:
        """Get the FastAPI app with MCP endpoints"""
        return self.http_server.app

    async def list_all_tools(self) -> Dict[str, Dict[str, List[Any]]]:
        """List all available tools from plugins and external servers"""
        # Check cache validity
        now = time.time()
        if (self._tools_cache is not None and
            (now - self._tools_cache_time) < self._tools_cache_ttl):
            logger.debug("Returning cached tools list (age: %.1fs)", now - self._tools_cache_time)
            return self._tools_cache

        logger.debug("Refreshing tools list cache...")

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
                    "input_schema": tool.input_schema,
                    "blocked": False  # Plugins don't have blocking yet
                }
                for tool in tools
            ]

        # Get tools from external servers
        external_tools = await self.client_manager.list_all_tools()
        for server_name, tools in external_tools.items():
            # Get server configuration to check blocked tools
            server_config = self.configured_external_servers.get(server_name)
            blocked_tools = []
            if server_config and server_config.tools:
                blocked_tools = server_config.tools.blocked or []
                logger.debug(f"Found server config for {server_name}: blocked_tools={blocked_tools}")

            filtered_tools = []
            for tool in tools:
                is_blocked = tool.name in blocked_tools
                logger.debug(f"Tool {tool.name}: blocked={is_blocked} (blocked_tools={blocked_tools})")
                filtered_tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "blocked": is_blocked
                })

            result["external_servers"][server_name] = filtered_tools

        # Update cache
        self._tools_cache = result
        self._tools_cache_time = now
        logger.debug("Tools list cache updated")

        return result

    def invalidate_tools_cache(self) -> None:
        """Invalidate the tools cache when connections change"""
        self._tools_cache = None
        self._tools_cache_time = 0.0
        logger.debug("Tools cache invalidated")

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

        # Check if tool is blocked before calling
        if server_type == "external":
            # Get server configuration to check blocked tools
            server_config = self.configured_external_servers.get(server_name)
            blocked_tools = []
            if server_config and server_config.tools:
                blocked_tools = server_config.tools.blocked or []
                logger.debug(f"Found server config for {server_name}: blocked_tools={blocked_tools}")

            if tool_name in blocked_tools:
                error_msg = f"Tool '{tool_name}' is blocked on server '{server_name}'"
                logger.warning(error_msg)
                raise Exception(error_msg)

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

    async def register_plugin(self, name: str, config: Optional[AgentSystemConfig] = None) -> None:
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

    async def add_external_server(self, name: str, config: RemoteMCPConfig) -> None:
        """Add an external MCP server"""
        await self.client_manager.add_client(name, config)
        # Update local config storage
        self.configured_external_servers[name] = config
        # Invalidate tools cache
        self.invalidate_tools_cache()

    async def remove_external_server(self, name: str) -> None:
        """Remove an external MCP server"""
        await self.client_manager.remove_client(name)
        # Remove from local config storage
        self.configured_external_servers.pop(name, None)
        # Invalidate tools cache
        self.invalidate_tools_cache()


# Global MCP integration instance
mcp_integration: Optional[MCPIntegration] = None


def get_mcp_integration(app: Optional[FastAPI] = None, config: Optional[AgentSystemConfig] = None) -> MCPIntegration:
    """Get or create the global MCP integration instance"""
    global mcp_integration
    # First check if the API has an initialized instance and prefer it
    try:
        from agent_system.agent.interface_api import _mcp_integration as api_integration
        if api_integration is not None and api_integration.initialized:
            return api_integration
    except (ImportError, AttributeError):
        pass  # API module not available or not initialized

    # Return existing global instance if available
    if mcp_integration is not None:
        return mcp_integration

    # If no config provided and no existing instance, we need config to create one
    if config is None:
        raise ValueError("AgentSystemConfig is required when creating new MCPIntegration instance")

    # If an app is provided, create a fresh app-bound integration so tests
    # that build an ASGI app get a dedicated integration instance and do not
    # accidentally reuse a previously initialized global instance.
    if app is not None:
        fresh_integration = MCPIntegration(app, config)
        return fresh_integration

    # Fall back to module-level global instance (create if needed)
    mcp_integration = MCPIntegration(app, config)
    return mcp_integration


async def initialize_mcp(config: AgentSystemConfig, app: Optional[FastAPI] = None) -> MCPIntegration:
    """Initialize MCP integration with configuration"""
    integration = get_mcp_integration(app, config)
    await integration.initialize(config)
    return integration


async def shutdown_mcp() -> None:
    """Shutdown MCP integration"""
    global mcp_integration
    if mcp_integration:
        await mcp_integration.shutdown()
        mcp_integration = None