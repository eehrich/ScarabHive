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
from ..plugins.mcp_adapter import plugin_mcp_registry
from .security import configure_security
from .tool_cache import ToolCache
from ..config.models import AgentSystemConfig, RemoteMCPConfig

logger = logging.getLogger(__name__)


class MCPIntegration:
    """Main integration class for MCP functionality"""

    def __init__(self, app: Optional[FastAPI] = None, config: AgentSystemConfig = None):
        if config is None:
            raise ValueError("AgentSystemConfig is required for MCPIntegration initialization")

        # Store full config for network settings access
        self.config = config

        # Configure security with provided config
        configure_security(config)

        self.client_manager = MCPClientManager()
        self.plugin_registry = plugin_mcp_registry
        self.initialized = False
        self.servers_bootstrapped = False  # Track if bootstrap_servers() was called
        self.configured_external_servers: Dict[str, RemoteMCPConfig] = {}  # Type-safe config storage

        # Tool caching with config-aware invalidation
        cache_enabled = config.external_servers.cache.enabled if (
            config.external_servers and 
            config.external_servers.cache
        ) else True
        cache_max_size = config.external_servers.cache.max_size if (
            config.external_servers and 
            config.external_servers.cache
        ) else None
        self._tool_cache = ToolCache(enabled=cache_enabled, max_size=cache_max_size)


    async def initialize(self, config: AgentSystemConfig) -> None:
        """Initialize MCP integration from configuration."""
        if self.initialized:
            return

        await self._initialize_cache_settings(config)
        await self._bootstrap_servers(config)  # Bootstrap agents BEFORE plugin discovery
        await self._discover_and_register_plugins(config)
        await self._register_plugin_hooks(config)
        await self._connect_external_servers(config)

        self.initialized = True
        logger.info("MCP integration initialized successfully")

    async def _initialize_cache_settings(self, config: AgentSystemConfig) -> None:
        """Set cache TTL on client manager."""
        if config.external_servers and config.external_servers.cache:
            ttl = config.external_servers.cache.tool_list_ttl
            self.client_manager.set_cache_ttl(ttl)
            logger.debug(f"MCP client manager cache TTL set to {ttl}s")

    async def _bootstrap_servers(self, config: AgentSystemConfig) -> None:
        """Bootstrap MCP servers and agents using bootstrap_servers().
        
        This is called once during initialization. If bootstrap_servers()
        was already called externally (e.g., by InitializationService), skip it.
        """
        if self.servers_bootstrapped:
            logger.debug("Servers already bootstrapped (flag set), skipping")
            return
        
        # Check if plugin_registry already has servers (bootstrapped elsewhere)
        if self.plugin_registry.plugin_servers:
            logger.debug(
                f"Servers already bootstrapped externally "
                f"({len(self.plugin_registry.plugin_servers)} servers in plugin_registry), skipping"
            )
            self.servers_bootstrapped = True
            return
        
        from ..mcp.base import MCPRegistry
        from ..servers.bootstrap import bootstrap_servers
        
        # Create temporary registry for bootstrap (agents will be auto-registered in plugin_registry)
        temp_registry = MCPRegistry()
        bootstrap_servers(config, temp_registry)
        self.servers_bootstrapped = True
        
        logger.debug(
            f"Bootstrap complete - {len(temp_registry.list())} servers in temp registry, "
            f"{len(self.plugin_registry.list_servers())} servers in plugin_registry"
        )

    async def _discover_and_register_plugins(self, config: AgentSystemConfig) -> None:
        """Discover and register enabled plugins."""
        # Use plugin_dirs from config, fallback to default
        plugin_dirs = config.plugins.plugin_dirs if config.plugins and config.plugins.plugin_dirs else ['src/plugins']
        
        # Check if plugins are already discovered (singleton registry)
        if not self.plugin_registry.plugin_factories:
            logger.debug(f"Discovering plugins for the first time from dirs: {plugin_dirs}")
            self.plugin_registry.discover_plugins(plugin_dirs)
        else:
            logger.debug(
                f"Plugins already discovered "
                f"({len(self.plugin_registry.plugin_factories)} factories available)"
            )

        # Get enabled servers from config
        servers_config = config.plugins.servers if config.plugins and config.plugins.servers else {}
        enabled_servers = [
            name for name, server_cfg in servers_config.items() 
            if server_cfg.enabled
        ]
        
        logger.debug(f"MCP integration - enabled servers: {enabled_servers}")
        logger.debug(
            f"MCP integration - already registered servers: "
            f"{list(self.plugin_registry.plugin_servers.keys())}"
        )

        # Only register plugins that are not already registered
        servers_to_register = [
            name for name in enabled_servers 
            if name not in self.plugin_registry.plugin_servers
        ]
        
        if servers_to_register:
            logger.debug(f"Registering new servers: {servers_to_register}")
            await self.plugin_registry.register_from_config(
                servers_to_register, 
                servers_config, 
                config
            )
        else:
            logger.debug("All enabled servers already registered, skipping re-registration")

    async def _register_plugin_hooks(self, config: AgentSystemConfig) -> None:
        """Register hooks from plugins."""
        from ..plugins.discovery import register_plugin_hooks
        from ..hooks import load_hooks_config
        
        hooks_config = load_hooks_config()
        
        for server_name in self.plugin_registry.list_servers():
            server = self.plugin_registry.get_server(server_name)
            
            if not server or not hasattr(server, 'plugin_schema') or not server.plugin_schema:
                continue
            
            plugin_schema = server.plugin_schema
            if 'hooks' not in plugin_schema:
                continue
            
            # Get the actual plugin instance (unwrap PluginMCPAdapter)
            plugin_instance = server.plugin_server if hasattr(server, 'plugin_server') else server
            
            # For hybrid plugins, get the hooks_plugin attribute
            if hasattr(plugin_instance, 'hooks_plugin'):
                plugin_instance = plugin_instance.hooks_plugin
            
            try:
                registered_hooks = await register_plugin_hooks(
                    plugin_name=server_name,
                    plugin_instance=plugin_instance,
                    metadata=plugin_schema,
                    hooks_config=hooks_config
                )
                if registered_hooks:
                    logger.debug(
                        f"Registered {len(registered_hooks)} hook(s) "
                        f"for plugin '{server_name}': {registered_hooks}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to register hooks for plugin '{server_name}': {e}",
                    exc_info=True
                )

    async def _connect_external_servers(self, config: AgentSystemConfig) -> None:
        """Connect to external MCP servers."""
        if not config.external_servers or not config.external_servers.remote_servers:
            return

        remote_servers = config.external_servers.remote_servers
        
        # Store enabled servers
        self.configured_external_servers = {
            name: server_config
            for name, server_config in remote_servers.items()
            if server_config.enabled
        }
        
        # Connect to enabled servers
        ssl_verify = self.config.network.ssl_verify if self.config and self.config.network else True
        timeout = (
            config.external_servers.connection.timeout 
            if config.external_servers and config.external_servers.connection
            else 30.0
        )
        connection_limit = self.config.network.http_connection_limit if self.config and self.config.network else 10
        connection_limit_per_host = self.config.network.http_connection_limit_per_host if self.config and self.config.network else 5
        
        for server_name, server_config in remote_servers.items():
            if not server_config.enabled:
                logger.debug(f"Skipping disabled external MCP server: {server_name}")
                continue
                
            try:
                await self.client_manager.add_client(
                    server_name, 
                    server_config, 
                    ssl_verify=ssl_verify, 
                    timeout=timeout,
                    connection_limit=connection_limit,
                    connection_limit_per_host=connection_limit_per_host
                )
                logger.info(f"Connected to external MCP server: {server_name}")
            except Exception as e:
                logger.debug(f"Failed to connect to external MCP server {server_name}: {e}")

    async def shutdown(self) -> None:
        """Shutdown MCP integration"""
        logging.getLogger(__name__).debug("MCPIntegration.shutdown() called")
        await self.client_manager.close_all()
        logging.getLogger(__name__).debug("MCPIntegration.shutdown() completed")
        logger.info("MCP integration shut down")

    async def retry_connect_server(self, server_name: str) -> bool:
        """
        Retry connecting to an external MCP server.
        Used when a server was unavailable at startup but becomes available later.
        
        Returns True if connection successful, False otherwise.
        """
        # Check if server is already connected
        if server_name in self.client_manager.list_clients():
            logger.debug(f"Server {server_name} already has an active client")
            return True
        
        # Check if server is configured
        server_config = self.configured_external_servers.get(server_name)
        if not server_config:
            logger.warning(f"Server {server_name} not found in configured external servers")
            return False
        
        # Try to connect
        try:
            ssl_verify = self.config.network.ssl_verify if self.config and self.config.network else True
            timeout = self.config.external_servers.connection.timeout if (
                self.config.external_servers and 
                self.config.external_servers.connection
            ) else 30.0
            connection_limit = self.config.network.http_connection_limit if self.config and self.config.network else 10
            connection_limit_per_host = self.config.network.http_connection_limit_per_host if self.config and self.config.network else 5
            
            await self.client_manager.add_client(
                server_name, 
                server_config, 
                ssl_verify=ssl_verify, 
                timeout=timeout,
                connection_limit=connection_limit,
                connection_limit_per_host=connection_limit_per_host
            )
            logger.info(f"Successfully reconnected to external MCP server: {server_name}")
            
            # Invalidate tools cache to pick up new tools
            await self.invalidate_tools_cache()
            
            return True
        except Exception as e:
            logger.debug(f"Failed to reconnect to external MCP server {server_name}: {e}")
            return False

    async def list_all_tools(self) -> Dict[str, Dict[str, List[Any]]]:
        """List all available tools from plugins and external servers"""
        # Compute config hash for cache invalidation
        cache_config = {
            "external_servers": {
                name: {
                    "url": server.url,
                    "blocked_tools": server.tools.blocked if server.tools else []
                }
                for name, server in self.configured_external_servers.items()
            },
            "plugins": self.plugin_registry.list_servers()
        }
        config_hash = self._tool_cache.compute_config_hash(cache_config)
        
        # Try to get from cache
        cached_result = await self._tool_cache.get("all_tools", config_hash)
        if cached_result is not None:
            logger.debug("Returning cached tools list")
            return cached_result

        logger.debug("Fetching fresh tools list...")

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

        # Store in cache
        await self._tool_cache.set("all_tools", result, config_hash)
        logger.debug("Tools list cached")

        return result

    async def invalidate_tools_cache(self) -> None:
        """Invalidate the tools cache when connections change"""
        await self._tool_cache.invalidate()
        # Also invalidate the client manager's cache
        self.client_manager.invalidate_tools_cache()
        logger.debug("Tools cache invalidated (both integration and client manager)")

    async def get_cache_statistics(self) -> Dict[str, Any]:
        """Get tool cache statistics for monitoring"""
        return await self._tool_cache.get_statistics()

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

    async def register_plugin(self, name: str, config: Optional[AgentSystemConfig] = None) -> None:
        """Register a plugin as an MCP server"""
        await self.plugin_registry.register_plugin(name, config)


    async def unregister_plugin(self, name: str) -> None:
        """Unregister a plugin MCP server"""
        await self.plugin_registry.unregister_plugin(name)

    async def add_external_server(self, name: str, config: RemoteMCPConfig) -> None:
        """Add an external MCP server"""
        ssl_verify = self.config.network.ssl_verify if self.config and self.config.network else True
        timeout = self.config.external_servers.connection.timeout if (
            self.config.external_servers and 
            self.config.external_servers.connection
        ) else 30.0
        connection_limit = self.config.network.http_connection_limit if self.config and self.config.network else 10
        connection_limit_per_host = self.config.network.http_connection_limit_per_host if self.config and self.config.network else 5
        
        await self.client_manager.add_client(
            name, 
            config, 
            ssl_verify=ssl_verify, 
            timeout=timeout,
            connection_limit=connection_limit,
            connection_limit_per_host=connection_limit_per_host
        )
        # Update local config storage
        self.configured_external_servers[name] = config
        # Invalidate tools cache
        await self.invalidate_tools_cache()

    async def remove_external_server(self, name: str) -> None:
        """Remove an external MCP server"""
        await self.client_manager.remove_client(name)
        # Remove from local config storage
        self.configured_external_servers.pop(name, None)
        # Invalidate tools cache
        await self.invalidate_tools_cache()


# Global MCP integration instance
mcp_integration: Optional[MCPIntegration] = None


def get_mcp_integration(app: Optional[FastAPI] = None, config: Optional[AgentSystemConfig] = None) -> MCPIntegration:
    """Get or create the global MCP integration instance"""
    global mcp_integration
    # First check if the API has an initialized instance and prefer it
    try:
        from agent_system.app import _mcp_integration as api_integration
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