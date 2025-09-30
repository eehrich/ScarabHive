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
from .config import MCPConfigManager, MCPConfig
from .security import configure_security

logger = logging.getLogger(__name__)


class MCPIntegration:
    """Main integration class for MCP functionality"""

    def __init__(self, app: Optional[FastAPI] = None, config: Optional[Dict[str, Any]] = None):
        # Initialize configuration manager without loading config yet
        self.config_manager = MCPConfigManager()
        self.mcp_config = MCPConfig()  # Use default config initially

        # Configure security if config provided
        if config:
            configure_security(config)

        self.client_manager = MCPClientManager()
        self.plugin_registry = plugin_mcp_registry
        self.http_server = MCPHTTPServer(app)
        self.initialized = False
        self.configured_external_servers: Dict[str, Dict[str, Any]] = {}  # Store original configuration
        
        # Tool list caching to reduce external server queries
        self._tools_cache: Optional[Dict[str, Dict[str, List[Any]]]] = None
        self._tools_cache_time = 0.0
        self._tools_cache_ttl = 30.0  # Cache for 30 seconds
        
        # Reference to main agent for cancellation support


    async def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize MCP integration from configuration"""
        if self.initialized:
            return

        # Load the MCP configuration properly
        self.mcp_config = self.config_manager.load_config(config)

        mcp_config = config.get('mcp', {})
        
        # Update cache TTL from configuration
        cache_config = mcp_config.get('cache', {})
        self._tools_cache_ttl = cache_config.get('tool_list_ttl', 30.0)
        logger.debug(f"MCP tools cache TTL set to {self._tools_cache_ttl}s")
        
        # Set cache TTL on client manager as well
        self.client_manager.set_cache_ttl(self._tools_cache_ttl)

        # Load external servers from new configuration format
        await self._setup_external_servers_from_config()

        # Discover and register plugins
        plugin_dirs = mcp_config.get('plugin_dirs', ['src/plugins'])
        self.plugin_registry.discover_plugins(plugin_dirs)

        # Register enabled plugins as MCP servers
        enabled_servers = mcp_config.get('enabled_servers', [])
        # Read servers config from the servers section of mcp.yaml, not from mcp_config
        # which only contains external_servers config
        servers_config = config.get('servers', {})
        logger.debug(f"MCP integration - config keys: {list(config.keys())}")
        logger.debug(f"MCP integration - servers_config: {servers_config}")
        
        # SIMPLIFIED: Pass complete AgentConfig instead of selective parent_config
        # This eliminates the need to manually copy specific keys
        from ..config.models import AgentConfig
        
        if isinstance(config, dict) and all(key in config for key in ['llm_system', 'agent_llm_profiles']):
            # Convert dict to AgentConfig if needed (for full config access)
            try:
                full_agent_config = AgentConfig.model_validate(config)
                logger.debug("Converted config dict to AgentConfig for MCP plugin registration")
            except Exception as e:
                logger.warning("Could not convert config to AgentConfig: %s. Using dict fallback.", e)
                full_agent_config = config
        else:
            # Assume it's already an AgentConfig or compatible dict
            full_agent_config = config

        await self.plugin_registry.register_from_config(enabled_servers, servers_config, full_agent_config)

        # Register plugin servers with HTTP server
        for server_name in self.plugin_registry.list_servers():
            server = self.plugin_registry.get_server(server_name)
            if server:
                self.http_server.register_server(server_name, server)

        # Connect to external MCP servers (using old format for backward compatibility)
        external_servers = mcp_config.get('external_servers', {})
        # Only store enabled servers for runtime connections and status endpoint
        self.configured_external_servers = {
            name: config for name, config in external_servers.items()
            if config.get('enabled', True)
        }
        # Store all configured servers (enabled + disabled) for CLI management operations
        self.all_configured_external_servers = {
            name: config for name, config in external_servers.items()
        }
        for server_name, server_config in external_servers.items():
            # Only connect to enabled servers
            if not server_config.get('enabled', True):
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

    async def _setup_external_servers_from_config(self) -> None:
        """Setup external servers from new configuration format"""
        enabled_servers = [
            (server_name, server_config)
            for server_name, server_config in self.mcp_config.servers.items()
            if server_config.enabled
        ]

        if not enabled_servers:
            logger.info("No enabled external MCP servers to connect to")
            return

        logger.info(f"Connecting to {len(enabled_servers)} external MCP servers...")

        async def connect_server(server_name: str, server_config) -> None:
            """Connect to a single server"""
            try:
                # Map deprecated transport types for backward compatibility
                transport_type = server_config.transport_type
                if transport_type == "smithery":
                    # Legacy support: map smithery to streaming
                    transport_type = "streaming"
                    logger.warning(f"Transport type 'smithery' is deprecated for server {server_name}. Use 'http' instead.")

                # Create client config for the server
                client_config = {
                    "transport": transport_type,
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
                logger.debug(f"Failed to connect to MCP server {server_name}: {e}")

        # Connect to servers in parallel if enabled
        if self.mcp_config.parallel_connect:
            logger.info("Connecting to MCP servers in parallel...")
            import asyncio
            tasks = [
                connect_server(server_name, server_config)
                for server_name, server_config in enabled_servers
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            # Connect sequentially (original behavior)
            for server_name, server_config in enabled_servers:
                await connect_server(server_name, server_config)

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
            server_config = self.mcp_config.servers.get(server_name)
            blocked_tools = []
            if server_config:
                blocked_tools = server_config.blocked_tools
                logger.debug(f"Found server config for {server_name}: blocked_tools={blocked_tools}")
            else:
                # Fallback: check in configured_external_servers from old format
                logger.debug(f"No server config found for {server_name} in self.mcp_config.servers")
                server_info = self.configured_external_servers.get(server_name, {})
                tools_config = server_info.get("tools", {})
                blocked_tools = tools_config.get("blocked", [])
                logger.debug(f"Using fallback from configured_external_servers: blocked_tools={blocked_tools}")

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
            server_config = self.mcp_config.servers.get(server_name)
            blocked_tools = []
            if server_config:
                blocked_tools = server_config.blocked_tools
                logger.debug(f"Found server config for {server_name}: blocked_tools={blocked_tools}")
            else:
                # Fallback: check in configured_external_servers from old format
                logger.debug(f"No server config found for {server_name} in self.mcp_config.servers")
                server_info = self.configured_external_servers.get(server_name, {})
                tools_config = server_info.get("tools", {})
                blocked_tools = tools_config.get("blocked", [])
                logger.debug(f"Using fallback from configured_external_servers: blocked_tools={blocked_tools}")

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
    # First check if the API has an initialized instance and prefer it
    try:
        from agent_system.agent.interface_api import _mcp_integration as api_integration
        if api_integration is not None and api_integration.initialized:
            return api_integration
    except (ImportError, AttributeError):
        pass  # API module not available or not initialized

    # If an app is provided, create a fresh app-bound integration so tests
    # that build an ASGI app get a dedicated integration instance and do not
    # accidentally reuse a previously initialized global instance.
    if app is not None:
        mcp_integration = MCPIntegration(app)
        return mcp_integration

    # Fall back to module-level global instance (create if needed)
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