"""
Plugin to tool-source adapter

Wraps an AgentSystem plugin so that callers can list and call its tools the same
way they use an external server of the mcp_client plugin.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from pathlib import Path

from ..tools.base import ToolDef, ToolServerCapability, tool_def_from_function, tool_defs_from_openai
from . import capabilities
from .web_adapter import PluginWebInterface, plugin_web_registry
from .schema_loader import load_schema_from_dir

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class PluginToolAdapter:
    """Presents an AgentSystem plugin as a source of tools: list_tools() and call_tool().

    Deliberately no base class. It used to inherit an abstract JSON-RPC server
    that carried resources and prompts nobody ever implemented or called; what
    remained of it were these three attributes.
    """

    def __init__(self, plugin_name: str, plugin_server, plugin_schema: Optional[Dict[str, Any]] = None):
        self.name = plugin_name
        self.description = f"AgentSystem {plugin_name} plugin"
        self.plugin_server = plugin_server
        self.plugin_schema = plugin_schema or {}
        self.capabilities = [ToolServerCapability.TOOLS]  # Most plugins expose tools
        self._tools_cache: Optional[List[ToolDef]] = None

    async def list_tools(self) -> List[ToolDef]:
        """List tools available from the plugin - converts plugin interfaces to ToolDef objects"""
        if self._tools_cache is not None:
            return self._tools_cache

        tools = []

        # If the plugin_server offers list_tools(), delegate to it. Duck-typed
        # on purpose: the old check additionally required ToolServer among the
        # DIRECT bases, which a hybrid wrapper (plain class delegating
        # list_tools to its inner server, e.g. SubAgentManagerHybridPlugin)
        # never satisfies -- the adapter then fell through all fallbacks and
        # FABRICATED a single tool named like the server. TypeError joins the
        # excused set so a sync list_tools() drops through to get_tools()
        # instead of erroring out.
        if hasattr(self.plugin_server, 'list_tools'):
            try:
                tools = await self.plugin_server.list_tools()
                self._tools_cache = tools
                return tools
            except (NotImplementedError, AttributeError, TypeError):
                pass

        # Handle plugins with get_tools() method (OpenAI function format)
        if hasattr(self.plugin_server, 'get_tools'):
            try:
                tools = tool_defs_from_openai(self.plugin_server.get_tools())
            except (NotImplementedError, AttributeError):
                pass

        # Use external schema file if available
        if not tools and self.plugin_schema:
            if 'functions' in self.plugin_schema:
                tools = [tool_def_from_function(func_def)
                         for func_def in self.plugin_schema['functions']]

        if not tools and (hasattr(self.plugin_server, 'call')
                          or hasattr(self.plugin_server, 'call_with_status')):
            # Legacy generic-action plugin: callable, but lists no tools.
            # Only fabricate the catch-all tool when call_tool() can actually
            # dispatch it -- for hook-/web-only plugins (no call path) the
            # fabricated tool existed only to fail on every invocation; they
            # legitimately have zero tools.
            tool = ToolDef(
                name=self.name,
                description=f"Generic action for {self.name}",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True
                }
            )
            tools.append(tool)

        self._tools_cache = tools
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the underlying plugin"""
        try:
            # Prefer call_with_status() for automatic status scope management
            if hasattr(self.plugin_server, 'call_with_status'):
                result = await self.plugin_server.call_with_status(name, arguments)
                return result
            elif hasattr(self.plugin_server, 'call'):
                # Fallback to call() for plugins without status support
                result = await self.plugin_server.call(name, arguments)
                return result
            else:
                raise Exception(f"Plugin {self.name} does not support tool calls")
        except Exception as e:
            logger.error(f"Tool call failed on plugin {self.name}: {e}")
            raise


def _plugin_dir(factory: Any) -> Optional[Path]:
    """The directory of the module that defines *factory* (the plugin's folder)."""
    module = sys.modules.get(getattr(factory, "__module__", None) or "")
    module_file = getattr(module, "__file__", None)
    return Path(module_file).resolve().parent if module_file else None


def _load_plugin_schema(name: str, plugin_server: Any, factory: Any = None,
                        plugin_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The schema of a plugin the registry has just built; None if none is found.

    Asked of the server first, then read from ``schema.yaml`` / ``schema.json``
    in the plugin's directory: the folder of the module that defines
    *factory*, else ``src/plugins/<type>`` / ``plugins/<type>`` below the
    working directory. The folder is the plugin TYPE's, never the instance
    name's -- a config agent named ``slovak_tutor`` of type ``basic_agent``
    has no folder of its own. register_existing_plugin_instance asks the
    instance only.
    """
    schema = None
    schema_file = None

    # Try to get schema from plugin server first (handles templates properly)
    if hasattr(plugin_server, 'get_schema_data'):
        try:
            schema = plugin_server.get_schema_data()
            logger.debug(f"Loaded schema for plugin {name} from server (with template support)")
        except Exception as e:
            logger.warning(f"Failed to load schema from plugin server {name}: {e}")
            schema = None

    # Fallback to file-based loading if server doesn't support schema or failed
    if schema is None:
        candidates = []
        factory_dir = _plugin_dir(factory)
        if factory_dir is not None:
            candidates.append(factory_dir)
        candidates.extend(Path(plugin_dir) / (plugin_type or name)
                          for plugin_dir in ("src/plugins", "plugins"))
        schema_file = next((directory / filename for directory in candidates
                            for filename in ("schema.yaml", "schema.json")
                            if (directory / filename).exists()), None)

        if schema_file:
            try:
                # Use template-aware loader for consistency
                schema_dir = schema_file.parent
                template_vars = {'name': name}
                schema = load_schema_from_dir(schema_dir, template_vars)
                logger.debug(f"Loaded schema for plugin {name} from file with template support")
            except Exception as e:
                logger.warning(f"Failed to load schema for plugin {name}: {e}")
    return schema


def _register_web_capabilities(name: str, plugin_server: Any) -> None:
    """Hand a plugin the registry has just built to the web registry, if it serves web routes."""
    if isinstance(plugin_server, PluginWebInterface):
        plugin_web_registry.register_web_plugin(name, plugin_server)
        logger.debug(f"Registered web capabilities for plugin {name}")
    elif hasattr(plugin_server, 'get_web_router'):
        # Handle hybrid plugins that implement web methods but don't inherit PluginWebInterface
        plugin_web_registry.register_web_plugin(name, plugin_server)
        logger.debug(f"Registered hybrid web capabilities for plugin {name}")


class PluginToolRegistry:
    """Registry for the plugins built as tool servers"""

    def __init__(self):
        self.plugin_servers: Dict[str, PluginToolAdapter] = {}
        self.plugin_factories: Dict[str, Any] = {}
        #: Plugins whose start_plugin() hook has run, so it runs exactly once.
        self._started: set[str] = set()

    def discover_plugins(self, plugin_dirs: List[str]) -> None:
        """Discover plugins from directories"""
        # Import here to avoid circular dependency
        from .discovery import _add_plugins, _ModuleNames

        module_names = _ModuleNames()
        for plugin_dir in plugin_dirs:
            path = Path(plugin_dir)
            if path.exists() and path.is_dir():
                factories = module_names.discover(path)
                # The Runtime's rule: the first source of a type wins. With
                # update() here, a server this path builds (one the Runtime
                # did not) would get the other plugin of the same name.
                _add_plugins(self.plugin_factories, factories, str(path))
                logger.info(f"Discovered {len(factories)} plugins from {plugin_dir}")

    def register_existing_plugin_instance(self, name: str, plugin_instance, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """Register an already-instantiated plugin instance (to prevent duplicate creation).
        
        Used when bootstrap has already created the plugin and we want to register it
        in the plugin_tool_registry without creating a second instance.
        """
        if name in self.plugin_servers:
            logger.debug(f"Plugin {name} already registered, skipping duplicate registration")
            return
            
        # Load schema if available
        schema = None
        
        # Try to get schema from plugin server
        if hasattr(plugin_instance, 'get_schema_data'):
            try:
                schema = plugin_instance.get_schema_data()
                logger.debug(f"Loaded schema for plugin {name} from existing instance")
            except Exception as e:
                logger.warning(f"Failed to load schema from plugin instance {name}: {e}")
        
        # Create tool adapter
        tool_adapter = PluginToolAdapter(name, plugin_instance, schema)
        # The instance's hook default travels with it: hook registration looks it up in the calling agent's config,
        # and an agent built on another config (a test's, a CLI's) does not know this server -- without this its
        # hooks would register on schema defaults, i.e. on for every agent, whatever the instance said.
        tool_adapter.instance_hook_config = getattr(server_config, "hook_config", None)
        self.plugin_servers[name] = tool_adapter
        
        # Register web capabilities if supported
        if hasattr(plugin_instance, 'get_web_router'):
            plugin_web_registry.register_web_plugin(name, plugin_instance)
            logger.debug(f"Registered web capabilities for existing plugin {name}")
        
        logger.info(f"Registered existing plugin instance {name} in plugin_tool_registry")
    
    async def start_plugin(self, name: str) -> None:
        """Run a plugin's optional ``start_plugin()`` hook, at most once.

        Seam: until this existed the registry created the plugin object and
        then did nothing, so anything with a real lifecycle -- the external MCP
        client, with its connections -- could not be a plugin at all.

        Idempotent on purpose. Plugins arrive through two different paths
        (register_plugin_simple, and the synchronous
        register_existing_plugin_instance used by bootstrap), and start_all()
        sweeps up afterwards; hooking each path separately would either miss
        one or start a plugin twice.
        """
        if name in self._started:
            return
        adapter = self.plugin_servers.get(name)
        if adapter is None:
            return
        self._started.add(name)
        await capabilities.start_plugin(getattr(adapter, "plugin_server", adapter))

    async def start_all(self) -> None:
        """Start every registered plugin that has not been started yet.

        Called once after discovery, which is what covers the synchronous
        bootstrap path -- it cannot await a hook itself.
        """
        for name in list(self.plugin_servers):
            await self.start_plugin(name)

    async def unregister_plugin(self, name: str) -> None:
        """Unregister a plugin tool server"""
        if name in self.plugin_servers:
            adapter = self.plugin_servers[name]
            # Counterpart to start_plugin: give the plugin the chance to close
            # sockets and tasks before its last reference goes away.
            await capabilities.stop_plugin(getattr(adapter, "plugin_server", adapter))
            self._started.discard(name)
            # Also unregister web capabilities
            plugin_web_registry.unregister_web_plugin(name)
            del self.plugin_servers[name]
            logger.info(f"Unregistered plugin {name}")

    async def shutdown_all(self) -> None:
        """Stop every started plugin, keeping them registered.

        Used at application shutdown, where the goal is to release resources,
        not to tear down the registry. Plugins stay startable afterwards.
        """
        for name in list(self._started):
            adapter = self.plugin_servers.get(name)
            if adapter is None:
                continue
            await capabilities.stop_plugin(getattr(adapter, "plugin_server", adapter))
            self._started.discard(name)
            logger.debug("Stopped plugin %s", name)

    def get_server(self, name: str) -> Optional[PluginToolAdapter]:
        """Get a plugin tool server by name"""
        return self.plugin_servers.get(name)

    def list_servers(self) -> List[str]:
        """List all registered plugin tool servers"""
        return list(self.plugin_servers.keys())

    def list_available_plugins(self) -> List[str]:
        """List all available plugins (not necessarily registered)"""
        return list(self.plugin_factories.keys())

    async def register_from_config(self, enabled_servers: List[str], servers_config: Dict[str, Any], system_config: AgentSystemConfig) -> None:
        """Register plugins from configuration.
        
        Args:
            enabled_servers: List of plugin names to register
            servers_config: Dict[str, ToolServerConfig] OR Dict[str, dict] with plugin configurations
            system_config: Complete AgentSystemConfig (not AgentConfig!)
        """
        logger.debug(f"register_from_config - servers_config keys: {list(servers_config.keys())}")

        for server_name in enabled_servers:
            # Get ToolServerConfig for this server FIRST — the factory is resolved via
            # the server's TYPE, not its key. Config agents use arbitrary keys
            # with e.g. type=basic_agent (same rule as servers/bootstrap.py);
            # the old key-based lookup errored for every such server whenever
            # this fallback path ran (broken startup ordering flooded the log
            # with one error per config agent).
            server_config = servers_config.get(server_name)

            # Import ToolServerConfig here to avoid circular dependency
            from agent_system.config.models import ToolServerConfig as ToolServerConfigClass, AgentConfig

            # Convert dict to ToolServerConfig if necessary
            if isinstance(server_config, dict):
                # Build ToolServerConfig from dict
                config_dict = dict(server_config)
                config_type = config_dict.pop('type', server_name)
                enabled = config_dict.pop('enabled', True)
                agent_config = AgentConfig()  # Default agent config

                # Create ToolServerConfig with extra fields allowed
                resolved_config = ToolServerConfigClass(
                    type=config_type,
                    enabled=enabled,
                    agent_config=agent_config,
                    **config_dict  # Pass remaining fields as extra
                )
            elif isinstance(server_config, ToolServerConfigClass):
                resolved_config = server_config
            else:
                logger.error(f"Plugin {server_name} config is invalid type: {type(server_config)}")
                continue

            plugin_type = resolved_config.type or server_name
            if plugin_type not in self.plugin_factories:
                logger.error(
                    f"Plugin type '{plugin_type}' (server '{server_name}') is enabled "
                    f"in config but not found in discovered plugins. "
                    f"Available plugins: {list(self.plugin_factories.keys())}. "
                    f"Check plugin.toml [plugin] 'name' matches the config 'type'."
                )
                continue

            logger.debug(f"register_from_config - plugin {server_name} type: {resolved_config.type}")

            try:
                await self.register_plugin_simple(server_name, system_config, resolved_config)
            except Exception as e:
                logger.error(f"Failed to register plugin {server_name}: {e}")

    async def register_plugin_simple(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """SIMPLIFIED: Register a plugin with system_config and server_config (modern signature).
        
        Args:
            name: Server key (instance name; may differ from the plugin type)
            system_config: Complete AgentSystemConfig with LLM, network, context, etc.
            server_config: ToolServerConfig with agent_config, enabled, type, etc.
        """
        # Factory is keyed by plugin TYPE (server_config.type); the server key is
        # the instance name. For classic plugins key == type; config agents
        # (e.g. key 'slovak_tutor' with type 'basic_agent') differ — same
        # resolution rule as servers/bootstrap.py.
        plugin_type = getattr(server_config, "type", None) or name
        if plugin_type not in self.plugin_factories:
            raise Exception(f"Unknown plugin type: {plugin_type} (server '{name}')")

        # Check if already registered (by bootstrap_servers)
        if name in self.plugin_servers:
            logger.debug(f"Plugin {name} already registered, skipping duplicate instantiation")
            return  # CRITICAL: Don't re-instantiate! Bootstrap already created it.

        # Create plugin instance with modern factory signature: (name, system_config, server_config)
        factory = self.plugin_factories[plugin_type]

        try:
            logger.info(f"tool registry creating plugin {name} (type={plugin_type}) with AgentSystemConfig and ToolServerConfig")
            plugin_server = factory(name, system_config, server_config)  # Modern call - clean interface
        except Exception as e:
            logger.error(f"Failed to create plugin instance {name}: {e}")
            raise

        # Visible degraded mode instead of silent drift: agents created via
        # this FALLBACK path (instead of via bootstrap_servers)
        # get no shared ToolServerRegistry injected — tool access then runs
        # only through the plugin registry. The primary path stays bootstrap.
        if plugin_type != name:
            try:
                from agent_system.servers.agent.server import Agent as _Agent
                if isinstance(plugin_server, _Agent):
                    logger.warning(
                        f"Config agent '{name}' (type={plugin_type}) registered via "
                        f"Tool-integration fallback WITHOUT shared agent registry — "
                        f"normally bootstrap_servers registers it first. Check startup order."
                    )
            except Exception:
                pass

        # Load schema if available
        schema = _load_plugin_schema(name, plugin_server, factory, plugin_type)

        # Create tool adapter
        tool_adapter = PluginToolAdapter(name, plugin_server, schema)
        # The instance's hook default travels with it, as in register_existing_plugin_instance.
        tool_adapter.instance_hook_config = getattr(server_config, "hook_config", None)
        self.plugin_servers[name] = tool_adapter

        # Register web capabilities if plugin supports them
        _register_web_capabilities(name, plugin_server)

        await self.start_plugin(name)

        logger.info(f"Registered plugin {name} as tool server")

    async def get_all_tools(self) -> Dict[str, List[ToolDef]]:
        """Get all tools from all registered plugins"""
        all_tools = {}
        for name, server in self.plugin_servers.items():
            try:
                tools = await server.list_tools()
                all_tools[name] = tools
            except Exception as e:
                logger.error(f"Failed to list tools from plugin {name}: {e}")
                all_tools[name] = []
        return all_tools

    async def call_plugin_tool(self, plugin_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on a specific plugin"""
        server = self.get_server(plugin_name)
        if not server:
            raise Exception(f"Plugin {plugin_name} not registered")

        return await server.call_tool(tool_name, arguments)


# Global plugin registry instance
plugin_tool_registry = PluginToolRegistry()