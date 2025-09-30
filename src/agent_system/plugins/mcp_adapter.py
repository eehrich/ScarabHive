"""
Plugin to MCP Server Adapter

Adapts existing AgentSystem plugins to be MCP-compatible servers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import json
from pathlib import Path

from ..mcp.core import MCPServer, MCPTool, MCPCapability
from .web_adapter import PluginWebInterface, plugin_web_registry

if TYPE_CHECKING:
    from agent_system.config.models import AgentConfig

logger = logging.getLogger(__name__)


class PluginMCPAdapter(MCPServer):
    """Adapts an AgentSystem plugin to be an MCP server"""

    def __init__(self, plugin_name: str, plugin_server, plugin_schema: Optional[Dict[str, Any]] = None):
        super().__init__(plugin_name, f"AgentSystem {plugin_name} plugin")
        self.plugin_server = plugin_server
        self.plugin_schema = plugin_schema or {}
        self.capabilities = [MCPCapability.TOOLS]  # Most plugins expose tools
        self._tools_cache: Optional[List[MCPTool]] = None

    async def list_tools(self) -> List[MCPTool]:
        """List tools available from the plugin - converts legacy interfaces to MCPTool objects"""
        if self._tools_cache is not None:
            return self._tools_cache

        tools = []

        # If the plugin_server is already an MCPServer with list_tools(), delegate to it
        if hasattr(self.plugin_server, 'list_tools') and hasattr(self.plugin_server.__class__, '__bases__'):
            # Check if it inherits from MCPServer (not just has the method)
            from ..mcp.base import MCPServer
            if any(issubclass(base, MCPServer) for base in self.plugin_server.__class__.__bases__):
                try:
                    tools = await self.plugin_server.list_tools()
                    self._tools_cache = tools
                    return tools
                except (NotImplementedError, AttributeError):
                    pass

        # Handle legacy plugins with get_tools() method (OpenAI function format)
        if hasattr(self.plugin_server, 'get_tools'):
            try:
                tool_schemas = self.plugin_server.get_tools()
                for tool_schema in tool_schemas:
                    if isinstance(tool_schema, dict) and 'function' in tool_schema:
                        func_def = tool_schema['function']
                        tool = MCPTool(
                            name=func_def['name'],
                            description=func_def.get('description', f'Tool {func_def["name"]}'),
                            input_schema=func_def.get('parameters', {})
                        )
                        tools.append(tool)
            except (NotImplementedError, AttributeError):
                pass

        # Handle legacy plugins with get_schema() method  
        if not tools and hasattr(self.plugin_server, 'get_schema'):
            try:
                schema = self.plugin_server.get_schema()
                if isinstance(schema, dict) and 'functions' in schema:
                    # Multi-function format
                    for func_def in schema['functions']:
                        tool = MCPTool(
                            name=func_def['name'],
                            description=func_def.get('description', f'Tool {func_def["name"]}'),
                            input_schema=func_def.get('parameters', {})
                        )
                        tools.append(tool)
                elif isinstance(schema, dict) and 'function' in schema:
                    # Single function format
                    func_def = schema['function']
                    tool = MCPTool(
                        name=func_def['name'],
                        description=func_def.get('description', f'Tool {func_def["name"]}'),
                        input_schema=func_def.get('parameters', {})
                    )
                    tools.append(tool)
            except (NotImplementedError, AttributeError):
                pass

        # Use external schema file if available
        if not tools and self.plugin_schema:
            if 'functions' in self.plugin_schema:
                for func_def in self.plugin_schema['functions']:
                    tool = MCPTool(
                        name=func_def['name'],
                        description=func_def.get('description', f'Tool {func_def["name"]}'),
                        input_schema=func_def.get('parameters', {})
                    )
                    tools.append(tool)

        # Final fallback: Create tool from default action
        if not tools and hasattr(self.plugin_server, 'get_default_action'):
            try:
                default_action = self.plugin_server.get_default_action()
                tool = MCPTool(
                    name=default_action,
                    description=f"Default action for {self.name}",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True
                    }
                )
                tools.append(tool)
            except (NotImplementedError, AttributeError):
                pass

        # If still no tools, create a generic one
        if not tools:
            tool = MCPTool(
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
            if hasattr(self.plugin_server, 'call_with_status'):
                result = await self.plugin_server.call_with_status(name, arguments)
                return result
            elif hasattr(self.plugin_server, 'call'):
                # Fallback for plugins that haven't been updated yet
                result = await self.plugin_server.call(name, arguments)
                return result
            else:
                raise Exception(f"Plugin {self.name} does not support tool calls")
        except Exception as e:
            logger.error(f"Tool call failed on plugin {self.name}: {e}")
            raise


class PluginMCPRegistry:
    """Registry for plugin-based MCP servers"""

    def __init__(self):
        self.plugin_servers: Dict[str, PluginMCPAdapter] = {}
        self.plugin_factories: Dict[str, Any] = {}

    def discover_plugins(self, plugin_dirs: List[str]) -> None:
        """Discover plugins from directories"""
        # Import here to avoid circular dependency
        from .discovery import discover_plugins
        
        for plugin_dir in plugin_dirs:
            path = Path(plugin_dir)
            if path.exists() and path.is_dir():
                factories = discover_plugins(path)
                self.plugin_factories.update(factories)
                logger.info(f"Discovered {len(factories)} plugins from {plugin_dir}")

    async def register_plugin(self, name: str, config: Optional[Dict[str, Any]] = None, parent_config: Optional[Dict[str, Any]] = None) -> None:
        """Register a plugin as an MCP server"""
        if name not in self.plugin_factories:
            raise Exception(f"Unknown plugin: {name}")

        # Check if already registered and log for debugging  
        if name in self.plugin_servers:
            logger.warning(f"Plugin {name} already registered in MCP registry. Re-registering with new config: {config}")
            # Continue to re-register instead of skipping

        # Create plugin instance with parent config injection
        factory = self.plugin_factories[name]
        plugin_config = dict(config or {})  # copy to avoid mutating original
        # Remove reserved keys coming from mcp.yaml server definitions (e.g., 'type')
        if 'type' in plugin_config:
            plugin_config.pop('type', None)
        
        # Inject parent_llm configuration if available and not already present
        if parent_config and isinstance(plugin_config, dict) and 'parent_llm' not in plugin_config:
            parent_llm = {}
            # Include LLM system configuration
            if 'llm_system' in parent_config:
                parent_llm['llm_system'] = parent_config['llm_system']
            if 'agent_llm_profiles' in parent_config:
                parent_llm['agent_llm_profiles'] = parent_config['agent_llm_profiles']
            # Include minimal LLM config for backwards compatibility
            parent_llm['llm'] = {}
            if parent_llm:
                plugin_config['parent_llm'] = parent_llm
                logger.debug(f"Injected parent_llm configuration into plugin {name}")
        
        # Ensure generic Agent plugin factories receive a full AgentConfig via parent_agent_config.
        # bootstrap() already injects this, but the CLI MCP adapter path builds its own instances.
        try:  # pragma: no cover - defensive
            if 'parent_agent_config' not in plugin_config and parent_config is not None:
                try:
                    from agent_system.config.models import AgentConfig  # local import
                    if isinstance(parent_config, AgentConfig):
                        plugin_config['parent_agent_config'] = parent_config
                    elif isinstance(parent_config, dict):
                        # Attempt to reconstruct AgentConfig from dict (expects llm_system key)
                        if 'llm_system' in parent_config:
                            plugin_config['parent_agent_config'] = AgentConfig.model_validate(parent_config)  # type: ignore[arg-type]
                except Exception:
                    logger.debug(f"Could not reconstruct AgentConfig for plugin {name} parent injection")
        except Exception:
            pass

        try:
            logger.info(f"MCP registry creating plugin {name} with config: {plugin_config}")
            # TODO: This is the OLD register_plugin method - should use register_plugin_simple instead
            # For now, fallback to old behavior but without ssl_verify if factory expects AgentConfig
            if 'parent_agent_config' in plugin_config:
                plugin_server = factory(name, plugin_config['parent_agent_config'])
            else:
                # Fallback for legacy factories that still expect dict + ssl_verify
                plugin_server = factory(name, plugin_config, ssl_verify=True)
        except Exception as e:
            logger.error(f"Failed to create plugin {name}: {e}")
            raise

        # Load schema if available
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
            # Try to find schema file
            for plugin_dir in ["src/plugins", "plugins"]:
                # Standard schema.yaml
                schema_path = Path(plugin_dir) / name / "schema.yaml"
                if schema_path.exists():
                    schema_file = schema_path
                    break

                # Standard JSON schema
                schema_path = Path(plugin_dir) / name / "schema.json"
                if schema_path.exists():
                    schema_file = schema_path
                    break

            if schema_file:
                try:
                    import yaml
                    with open(schema_file, 'r', encoding='utf-8') as f:
                        if schema_file.suffix == '.json':
                            schema = json.load(f)
                        else:
                            schema = yaml.safe_load(f)
                    logger.debug(f"Loaded schema for plugin {name} from file (no template support)")
                except Exception as e:
                    logger.warning(f"Failed to load schema for plugin {name}: {e}")

        # Create MCP adapter
        mcp_adapter = PluginMCPAdapter(name, plugin_server, schema)
        self.plugin_servers[name] = mcp_adapter

        # Register web capabilities if plugin supports them
        plugin_metadata = {'name': name, 'description': getattr(plugin_server, 'description', '')}
        if schema_file:
            plugin_metadata['schema_path'] = str(schema_file)
            
        if isinstance(plugin_server, PluginWebInterface):
            plugin_web_registry.register_web_plugin(name, plugin_server, plugin_metadata)
            logger.debug(f"Registered web capabilities for plugin {name}")
        elif hasattr(plugin_server, 'get_web_router'):
            # Handle hybrid plugins that implement web methods but don't inherit PluginWebInterface
            plugin_web_registry.register_web_plugin(name, plugin_server, plugin_metadata)
            logger.debug(f"Registered hybrid web capabilities for plugin {name}")

        logger.info(f"Registered plugin {name} as MCP server")

    async def unregister_plugin(self, name: str) -> None:
        """Unregister a plugin MCP server"""
        if name in self.plugin_servers:
            # Also unregister web capabilities
            plugin_web_registry.unregister_web_plugin(name)
            del self.plugin_servers[name]
            logger.info(f"Unregistered plugin {name}")

    def get_server(self, name: str) -> Optional[PluginMCPAdapter]:
        """Get a plugin MCP server by name"""
        return self.plugin_servers.get(name)

    def list_servers(self) -> List[str]:
        """List all registered plugin MCP servers"""
        return list(self.plugin_servers.keys())

    def list_available_plugins(self) -> List[str]:
        """List all available plugins (not necessarily registered)"""
        return list(self.plugin_factories.keys())

    async def register_from_config(self, enabled_servers: List[str], servers_config: Dict[str, Any], parent_config: AgentConfig) -> None:
        """Register plugins from configuration"""
        logger.debug(f"MCP register_from_config - servers_config keys: {list(servers_config.keys())}")
        
        for server_name in enabled_servers:
            if server_name in self.plugin_factories:
                server_overrides = servers_config.get(server_name, {})
                logger.debug(f"MCP register_from_config - plugin {server_name} overrides: {server_overrides}")
                
                if server_overrides:
                    # MODERN: Use Pydantic's model_copy with update for type-safe overrides
                    # This automatically handles all fields without hardcoding keys
                    try:
                        final_config = parent_config.model_copy(update=server_overrides, deep=True)
                        logger.debug(f"Applied server overrides for {server_name} using model_copy")
                    except Exception as e:
                        logger.warning(f"Could not apply server overrides for {server_name}: {e}. Using original config.")
                        final_config = parent_config
                else:
                    # No overrides, use original config
                    final_config = parent_config
                
                try:
                    await self.register_plugin_simple(server_name, final_config)
                except Exception as e:
                    logger.error(f"Failed to register plugin {server_name}: {e}")

    async def register_plugin_simple(self, name: str, config: AgentConfig) -> None:
        """SIMPLIFIED: Register a plugin with a complete AgentConfig (no complex dict handling)"""
        
        if name not in self.plugin_factories:
            raise Exception(f"Unknown plugin: {name}")

        # Check if already registered
        if name in self.plugin_servers:
            logger.warning(f"Plugin {name} already registered in MCP registry. Re-registering.")

        # Create plugin instance with simplified factory call
        factory = self.plugin_factories[name]
        
        try:
            logger.info(f"MCP registry creating plugin {name} with AgentConfig")
            plugin_server = factory(name, config)  # Modern call - clean interface
        except Exception as e:
            logger.error(f"Failed to create plugin instance {name}: {e}")
            raise

        # Load schema if available
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
            # Try to find schema file in standard locations
            for plugin_dir in ["src/plugins", "plugins"]:
                # Standard schema.yaml
                schema_path = Path(plugin_dir) / name / "schema.yaml"
                if schema_path.exists():
                    schema_file = schema_path
                    break

                # Standard JSON schema
                schema_path = Path(plugin_dir) / name / "schema.json"
                if schema_path.exists():
                    schema_file = schema_path
                    break

            if schema_file:
                try:
                    import yaml
                    with open(schema_file, 'r', encoding='utf-8') as f:
                        if schema_file.suffix == '.json':
                            schema = json.load(f)
                        else:
                            schema = yaml.safe_load(f)
                    logger.debug(f"Loaded schema for plugin {name} from file (no template support)")
                except Exception as e:
                    logger.warning(f"Failed to load schema for plugin {name}: {e}")

        # Create MCP adapter
        mcp_adapter = PluginMCPAdapter(name, plugin_server, schema)
        self.plugin_servers[name] = mcp_adapter

        # Register web capabilities if plugin supports them
        plugin_metadata = {'name': name, 'description': getattr(plugin_server, 'description', '')}
        if schema_file:
            plugin_metadata['schema_path'] = str(schema_file)
            
        if isinstance(plugin_server, PluginWebInterface):
            plugin_web_registry.register_web_plugin(name, plugin_server, plugin_metadata)
            logger.debug(f"Registered web capabilities for plugin {name}")
        elif hasattr(plugin_server, 'get_web_router'):
            plugin_web_registry.register_web_plugin(name, plugin_server, plugin_metadata)
            logger.debug(f"Registered hybrid web capabilities for plugin {name}")

        logger.info(f"Registered plugin {name} as MCP server")

    async def get_all_tools(self) -> Dict[str, List[MCPTool]]:
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
plugin_mcp_registry = PluginMCPRegistry()