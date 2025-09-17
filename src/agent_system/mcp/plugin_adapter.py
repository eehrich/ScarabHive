"""
Plugin to MCP Server Adapter

Adapts existing AgentSystem plugins to be MCP-compatible servers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import json
from pathlib import Path

from .core import MCPServer, MCPTool, MCPCapability
from .plugins import discover_plugins

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
        """List tools available from the plugin"""
        if self._tools_cache is not None:
            return self._tools_cache

        tools = []

        # Get schema from plugin if available
        if hasattr(self.plugin_server, 'get_schema'):
            schema = self.plugin_server.get_schema()
            if isinstance(schema, dict) and 'functions' in schema:
                for func_def in schema['functions']:
                    tool = MCPTool(
                        name=func_def['name'],
                        description=func_def.get('description', f'Tool {func_def["name"]}'),
                        input_schema=func_def.get('parameters', {})
                    )
                    tools.append(tool)

        # Fallback: Inspect plugin methods
        if not tools and hasattr(self.plugin_server, 'call'):
            # Try to get default action or discover available tools
            if hasattr(self.plugin_server, 'get_default_action'):
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

        # Use schema file if available
        if not tools and self.plugin_schema:
            if 'functions' in self.plugin_schema:
                for func_def in self.plugin_schema['functions']:
                    tool = MCPTool(
                        name=func_def['name'],
                        description=func_def.get('description', f'Tool {func_def["name"]}'),
                        input_schema=func_def.get('parameters', {})
                    )
                    tools.append(tool)

        self._tools_cache = tools
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the underlying plugin"""
        try:
            if hasattr(self.plugin_server, 'call'):
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
        for plugin_dir in plugin_dirs:
            path = Path(plugin_dir)
            if path.exists() and path.is_dir():
                factories = discover_plugins(path)
                self.plugin_factories.update(factories)
                logger.info(f"Discovered {len(factories)} plugins from {plugin_dir}")

    async def register_plugin(self, name: str, config: Optional[Dict[str, Any]] = None) -> None:
        """Register a plugin as an MCP server"""
        if name not in self.plugin_factories:
            raise Exception(f"Unknown plugin: {name}")

        # Create plugin instance
        factory = self.plugin_factories[name]
        try:
            plugin_server = factory(name, config or {}, ssl_verify=True)
        except Exception as e:
            logger.error(f"Failed to create plugin {name}: {e}")
            raise

        # Load schema if available
        schema = None
        schema_file = None

        # Try to find schema file
        for plugin_dir in ["src/plugins", "plugins"]:
            schema_path = Path(plugin_dir) / name / "mcp_schema.yaml"
            if schema_path.exists():
                schema_file = schema_path
                break

            # Try JSON schema
            schema_path = Path(plugin_dir) / name / "mcp_schema.json"
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
                logger.debug(f"Loaded schema for plugin {name}")
            except Exception as e:
                logger.warning(f"Failed to load schema for plugin {name}: {e}")

        # Create MCP adapter
        mcp_adapter = PluginMCPAdapter(name, plugin_server, schema)
        self.plugin_servers[name] = mcp_adapter

        logger.info(f"Registered plugin {name} as MCP server")

    async def unregister_plugin(self, name: str) -> None:
        """Unregister a plugin MCP server"""
        if name in self.plugin_servers:
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

    async def register_from_config(self, enabled_servers: List[str], servers_config: Dict[str, Any]) -> None:
        """Register plugins from configuration"""
        for server_name in enabled_servers:
            if server_name in self.plugin_factories:
                config = servers_config.get(server_name, {})
                try:
                    await self.register_plugin(server_name, config)
                except Exception as e:
                    logger.error(f"Failed to register plugin {server_name}: {e}")

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