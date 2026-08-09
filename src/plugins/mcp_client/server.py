"""The external MCP client, as a plugin.

Talking to foreign MCP servers used to be welded into ``MCPIntegration`` in the
core, next to the plugin bootstrap that has nothing to do with it. This plugin
takes over that half: it owns the connections, speaks the current protocol
through the official SDK, and offers the foreign tools back to the agent core
through the ``external_tools`` capability.

Two roles in one object:

* an ordinary plugin, with management tools (list / connect / disconnect /
  tools), so the connections can be inspected and steered like anything else;
* an :class:`~agent_system.plugins.capabilities.ExternalToolProvider`, which is
  how the agent core gets at the foreign tools without knowing this plugin
  exists.

The agent core keeps its ``server.tool`` naming for foreign tools. That dot is
not cosmetic -- it is what the tool-permission layer matches on, and what the
runtime uses to decide where a call goes. Federating these tools under flat
names would silently widen what an agent may call, so the plugin hands out the
same shape the core has always seen.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.plugins import capabilities

from .connection import MCPConnectionError
from .manager import ExternalServerPool

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class MCPClientServer(SchemaBasedMCPServer):
    """Connects to external MCP servers and federates their tools."""

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        super().__init__(name, system_config, mcp_config)

        network = getattr(system_config, "network", None)
        external = getattr(system_config, "external_servers", None)
        connection_cfg = getattr(external, "connection", None) if external else None
        cache_cfg = getattr(external, "cache", None) if external else None

        self.pool = ExternalServerPool(
            ssl_verify=getattr(network, "ssl_verify", True) if network else True,
            timeout=getattr(connection_cfg, "timeout", 30.0) if connection_cfg else 30.0,
            cache_ttl=getattr(cache_cfg, "tool_list_ttl", 300.0) if cache_cfg else 300.0,
        )
        if external is not None:
            self.pool.configure(getattr(external, "remote_servers", None) or {})

    # --------------------------------------------------------------- lifecycle

    async def start_plugin(self) -> None:
        """Announce the capability, then connect the enabled servers.

        Registration comes first on purpose: the core must be able to find the
        provider even when every server is unreachable, otherwise an outage
        would look like "no external tools are configured".
        """
        capabilities.register_provider(capabilities.EXTERNAL_TOOLS, self)

        if not self.pool.configured_servers:
            logger.info("No external MCP servers enabled")
            return

        results = await self.pool.connect_all()
        failed = {name: error for name, error in results.items() if error}
        logger.info(
            "External MCP servers: %d connected, %d failed",
            len(results) - len(failed), len(failed),
        )
        capabilities.notify_tool_catalog_changed()

    async def stop_plugin(self) -> None:
        """Close every connection and withdraw the capability."""
        capabilities.unregister_provider(capabilities.EXTERNAL_TOOLS, self)
        await self.pool.close_all()
        capabilities.notify_tool_catalog_changed()

    # ------------------------------------------------- ExternalToolProvider role

    async def list_external_tools(self, *, force_refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """``{server: [{name, description, input_schema, blocked}]}`` for the core."""
        return await self.pool.list_tools_by_server(force_refresh=force_refresh)

    async def call_external_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> Any:
        """Route one call to one external server."""
        return await self.pool.call_tool(server, tool, arguments)

    # -------------------------------------------------------- management tools

    async def list_servers(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: show the configured external MCP servers and their state."""
        status = self.pool.status()
        return {
            "servers": list(status.values()),
            "connected": self.pool.list_connected(),
            "total": len(status),
        }

    async def connect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: connect (or reconnect) one configured server."""
        name = params.get("server")
        if not name:
            return {"success": False, "error": "Parameter 'server' is required"}
        try:
            connection = await self.pool.connect(name)
        except (MCPConnectionError, Exception) as e:  # noqa: B014 - report, never raise
            return {"success": False, "server": name, "error": str(e)}
        capabilities.notify_tool_catalog_changed()
        return {
            "success": True,
            "server": name,
            "server_info": connection.server_info,
            "protocol_version": connection.protocol_version,
        }

    async def disconnect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: close one server's connection."""
        name = params.get("server")
        if not name:
            return {"success": False, "error": "Parameter 'server' is required"}
        closed = await self.pool.disconnect(name)
        capabilities.notify_tool_catalog_changed()
        return {"success": closed, "server": name,
                "message": "disconnected" if closed else "was not connected"}

    async def tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Tool: list the tools of the connected servers."""
        by_server = await self.pool.list_tools_by_server(
            force_refresh=bool(params.get("force_refresh"))
        )
        server = params.get("server")
        if server:
            by_server = {server: by_server.get(server, [])}
        return {
            "servers": by_server,
            "total": sum(len(tools) for tools in by_server.values()),
        }
