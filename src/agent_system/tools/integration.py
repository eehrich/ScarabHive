"""
Tool integration Module

Wires the tool servers into the AgentSystem:
- Builds a tool server per configured plugin
- Connects to external MCP servers as clients
- Provides one interface for listing and calling tools
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI

from ..plugins import capabilities
from ..plugins.tool_adapter import plugin_tool_registry
from .tool_cache import ToolCache
from ..config.models import AgentSystemConfig, RemoteMCPConfig

logger = logging.getLogger(__name__)


class ToolServerIntegration:
    """Boots the plugins and finds whoever federates external tools."""

    def __init__(self, app: Optional[FastAPI] = None, config: AgentSystemConfig = None):
        if config is None:
            raise ValueError("AgentSystemConfig is required for ToolServerIntegration initialization")

        # Store full config for network settings access
        self.config = config

        self.plugin_registry = plugin_tool_registry
        self.initialized = False
        self.servers_bootstrapped = False  # Track if bootstrap_servers() was called

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

        # A plugin that connects a new external server tells us through this
        # callback; without it a freshly connected server would stay invisible
        # for a whole cache TTL (an hour, with the shipped config).
        capabilities.on_tool_catalog_changed(self._tool_cache.invalidate)

    # ------------------------------------------------------------------ external
    #
    # Foreign MCP servers are no longer this class's business -- the mcp_client
    # plugin owns the connections and the protocol. What is left here is the
    # lookup, so the callers that ask an "tool integration" about external
    # servers keep working while the ownership sits where it belongs.

    @property
    def external_provider(self) -> Optional[Any]:
        """The plugin currently federating external tools, if any."""
        return capabilities.get_provider(capabilities.EXTERNAL_TOOLS)

    @property
    def configured_external_servers(self) -> Dict[str, RemoteMCPConfig]:
        """Enabled external servers, as known by the client plugin."""
        provider = self.external_provider
        if provider is None:
            return {}
        return getattr(getattr(provider, "pool", None), "configured_servers", {}) or {}

    async def initialize(self, config: AgentSystemConfig) -> None:
        """Initialize tool integration from configuration."""
        if self.initialized:
            return

        await self._bootstrap_servers(config)  # Bootstrap agents BEFORE plugin discovery
        await self._discover_and_register_plugins(config)
        await self._register_plugin_hooks(config)
        # Sweep: the synchronous bootstrap path cannot await a lifecycle hook,
        # so anything it registered gets started here. Idempotent.
        await self.plugin_registry.start_all()

        self.initialized = True
        logger.info("tool integration initialized successfully")

    async def _bootstrap_servers(self, config: AgentSystemConfig) -> None:
        """Bootstrap tool servers and agents using bootstrap_servers().
        
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
        
        from ..tools.base import ToolServerRegistry
        from ..servers.bootstrap import bootstrap_servers
        
        # Create temporary registry for bootstrap (agents will be auto-registered in plugin_registry)
        temp_registry = ToolServerRegistry()
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
        
        logger.debug(f"tool integration - enabled servers: {enabled_servers}")
        logger.debug(
            f"tool integration - already registered servers: "
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
        from ..plugins.discovery import register_plugin_hooks, warn_unknown_hook_overrides
        from ..hooks import load_hooks_config
        from ..config.settings import get_tool_server_config
        
        # From the config this integration runs on, not from a file path
        hooks_config = load_hooks_config(config)

        for server_name in self.plugin_registry.list_servers():
            server = self.plugin_registry.get_server(server_name)
            
            if not server or not hasattr(server, 'plugin_schema') or not server.plugin_schema:
                continue
            
            plugin_schema = server.plugin_schema
            if 'hooks' not in plugin_schema:
                continue
            
            # Get the actual plugin instance (unwrap PluginToolAdapter)
            plugin_instance = server.plugin_server if hasattr(server, 'plugin_server') else server
            
            # For hybrid plugins, get the hooks_plugin attribute
            if hasattr(plugin_instance, 'hooks_plugin'):
                plugin_instance = plugin_instance.hooks_plugin
            
            # The instance's own hook default lives in its MERGED server
            # config (raw and merged differ; agents get the merged form).
            # Guarded: one server whose merge does not validate must not
            # take down startup -- it just registers on schema defaults.
            try:
                server_cfg = get_tool_server_config(server_name, config)
                instance_hook_config = getattr(server_cfg, 'hook_config', None) if server_cfg else None
            except Exception:
                logger.debug("No merged config for '%s'", server_name, exc_info=True)
                instance_hook_config = None

            try:
                registered_hooks = await register_plugin_hooks(
                    plugin_name=server_name,
                    plugin_instance=plugin_instance,
                    metadata=plugin_schema,
                    hooks_config=hooks_config,
                    instance_hook_config=instance_hook_config,
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

        warn_unknown_hook_overrides(config)

    async def shutdown(self) -> None:
        """Shutdown tool integration.

        Stops every registered plugin, which is what closes the external
        connections now that the client plugin owns them.
        """
        logging.getLogger(__name__).debug("ToolServerIntegration.shutdown() called")
        await self.plugin_registry.shutdown_all()
        # A shut-down integration must not claim to be initialized -- the
        # agent-side setup uses this flag to decide whether a (re-)initialize
        # is needed.
        self.initialized = False
        logging.getLogger(__name__).debug("ToolServerIntegration.shutdown() completed")
        logger.info("tool integration shut down")

    def list_external_clients(self) -> List[str]:
        """Names of the currently connected external servers."""
        provider = self.external_provider
        pool = getattr(provider, "pool", None) if provider else None
        return pool.list_connected() if pool else []

    async def retry_connect_server(self, server_name: str) -> bool:
        """Connect an external server that was unavailable earlier.

        Returns True if it is connected afterwards, False otherwise.
        """
        provider = self.external_provider
        pool = getattr(provider, "pool", None) if provider else None
        if pool is None:
            logger.warning("No external MCP client plugin is active; cannot connect '%s'", server_name)
            return False

        if server_name in pool.list_connected():
            logger.debug(f"Server {server_name} already has an active client")
            return True
        if server_name not in pool.configured_servers:
            logger.warning(f"Server {server_name} not found in configured external servers")
            return False

        try:
            await pool.connect(server_name)
            logger.info(f"Successfully reconnected to external MCP server: {server_name}")
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

        # Get tools from external servers. The client plugin already marks
        # blocked tools (marked, not removed -- the permission layer above
        # decides), so this half is a straight hand-through now.
        provider = self.external_provider
        if provider is not None:
            try:
                result["external_servers"] = await provider.list_external_tools()
            except Exception as e:
                logger.warning(f"Could not list external tools: {e}")

        # Store in cache
        await self._tool_cache.set("all_tools", result, config_hash)
        logger.debug("Tools list cached")

        return result

    async def invalidate_tools_cache(self) -> None:
        """Invalidate the tools cache when connections change"""
        await self._tool_cache.invalidate()
        provider = self.external_provider
        pool = getattr(provider, "pool", None) if provider else None
        if pool is not None:
            pool.invalidate_cache()
        logger.debug("Tools cache invalidated (integration and external client)")

    async def get_cache_statistics(self) -> Dict[str, Any]:
        """Get tool cache statistics for monitoring"""
        return await self._tool_cache.get_statistics()

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any], server_type: str = "auto") -> Any:
        """Call a tool on a server (plugin or external)"""
        if server_type == "auto":
            # Try plugin first, then external
            if server_name in self.plugin_registry.list_servers():
                server_type = "plugin"
            elif server_name in self.list_external_clients():
                server_type = "external"
            else:
                raise Exception(f"Unknown server: {server_name}")

        if server_type == "plugin":
            return await self.plugin_registry.call_plugin_tool(server_name, tool_name, arguments)
        elif server_type == "external":
            provider = self.external_provider
            if provider is None:
                raise Exception(
                    f"Cannot call '{tool_name}' on '{server_name}': no external MCP client plugin is active"
                )
            # The blocked-tool check lives with the connection pool now, so it
            # applies to every path into an external server, not just this one.
            return await provider.call_external_tool(server_name, tool_name, arguments)
        else:
            raise Exception(f"Invalid server type: {server_type}")

    # NOTE: the former register_plugin()/unregister_plugin() wrappers were
    # removed: no caller repo-wide, and the register wrapper handed a
    # Pydantic model to the registry's dict-typed parent_config ("key in
    # model" is always False), silently dropping the parent LLM config.
    # The live path is register_plugin_simple() / plugin_registry directly.

    def _require_pool(self) -> Any:
        provider = self.external_provider
        pool = getattr(provider, "pool", None) if provider else None
        if pool is None:
            raise RuntimeError("No external MCP client plugin is active")
        return pool

    async def add_external_server(self, name: str, config: RemoteMCPConfig) -> None:
        """Add an external MCP server at runtime and connect it."""
        pool = self._require_pool()
        pool.configured_servers[name] = config
        try:
            await pool.connect(name)
        finally:
            await self.invalidate_tools_cache()

    async def remove_external_server(self, name: str) -> None:
        """Disconnect and forget an external MCP server."""
        pool = self._require_pool()
        await pool.disconnect(name)
        pool.configured_servers.pop(name, None)
        await self.invalidate_tools_cache()


# Global tool integration instance
tool_integration: Optional[ToolServerIntegration] = None


def get_tool_integration(app: Optional[FastAPI] = None, config: Optional[AgentSystemConfig] = None) -> ToolServerIntegration:
    """Get or create the global tool integration instance"""
    global tool_integration
    # First check if the API has an initialized instance and prefer it
    try:
        from agent_system.app import _tool_integration as api_integration
        if api_integration is not None and api_integration.initialized:
            return api_integration
    except (ImportError, AttributeError):
        pass  # API module not available or not initialized

    # Return existing global instance if available
    if tool_integration is not None:
        return tool_integration

    # If no config provided and no existing instance, we need config to create one
    if config is None:
        raise ValueError("AgentSystemConfig is required when creating new ToolServerIntegration instance")

    # If an app is provided, create a fresh app-bound integration so tests
    # that build an ASGI app get a dedicated integration instance and do not
    # accidentally reuse a previously initialized global instance.
    if app is not None:
        fresh_integration = ToolServerIntegration(app, config)
        return fresh_integration

    # Fall back to module-level global instance (create if needed)
    tool_integration = ToolServerIntegration(app, config)
    return tool_integration


async def initialize_tools(config: AgentSystemConfig, app: Optional[FastAPI] = None) -> ToolServerIntegration:
    """Initialize tool integration with configuration"""
    integration = get_tool_integration(app, config)
    await integration.initialize(config)
    return integration


async def shutdown_tools() -> None:
    """Shutdown tool integration"""
    global tool_integration
    if tool_integration:
        await tool_integration.shutdown()
        tool_integration = None