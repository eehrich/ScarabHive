"""
MCP Service

Centralized MCP (Model Context Protocol) server management. This service
provides a unified interface for MCP operations used by both CLI and API.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from agent_system.mcp.integration import MCPIntegration
from agent_system.config.models import AgentSystemConfig


logger = logging.getLogger(__name__)


class MCPService:
    """Centralized MCP server management service.

    This service eliminates duplicated MCP management code between
    CLI and API interfaces, providing a clean abstraction for:
    - Server listing and status
    - Tool listing
    - Server testing and health checks
    """

    def __init__(self, mcp_integration: MCPIntegration, config: AgentSystemConfig):
        """Initialize the MCPService.

        Args:
            mcp_integration: MCPIntegration instance for server management.
            config: AgentSystemConfig with MCP server configurations.
        """
        self._mcp = mcp_integration
        self._config = config

    async def list_servers(
        self,
        enabled_only: bool = False,
        include_tools: bool = False
    ) -> list[dict[str, Any]]:
        """List all configured MCP servers.

        Args:
            enabled_only: If True, return only enabled servers.
            include_tools: If True, include tool counts and names.

        Returns:
            List of server info dictionaries with keys:
            - name: Server name
            - enabled: Whether server is enabled
            - connected: Whether server is connected
            - type: Server type (plugin/external)
            - description: Server description
            - url: Server URL (for external servers)
            - tools: List of tool names (if include_tools=True)
            - tool_count: Number of tools (if include_tools=True)
        """
        servers = []

        # Get configured external servers
        configured = self._mcp.configured_external_servers or {}

        for name, server_config in configured.items():
            # Skip disabled servers if filtering
            if enabled_only and not server_config.enabled:
                continue

            # Check connection status
            client = None
            try:
                # Use get_client with await since it might need async initialization
                client = await self._get_client_safe(name)
            except Exception as e:
                logger.debug(f"Could not get client for {name}: {e}")

            is_connected = client is not None

            # Compute status string for CLI display
            if not server_config.enabled:
                status = "disabled"
            elif is_connected:
                status = "connected"
            else:
                status = "disconnected"

            server_info = {
                "name": name,
                "enabled": server_config.enabled,
                "connected": is_connected,
                "status": status,
                "type": "external",
                "description": server_config.description or name.replace('_', ' ').title(),
                "url": server_config.url if hasattr(server_config, 'url') else "",
                "address": server_config.url if hasattr(server_config, 'url') else ""
            }

            # Add tool information if requested
            if include_tools and is_connected and client:
                try:
                    tools = await client.list_tools()
                    tool_list = [tool.name for tool in tools] if tools else []
                    server_info["tools"] = tool_list
                    server_info["tool_count"] = len(tool_list)
                except Exception as e:
                    logger.debug(f"Could not list tools for {name}: {e}")
                    server_info["tools"] = []
                    server_info["tool_count"] = 0

            servers.append(server_info)

        return servers

    async def get_server_status(
        self,
        server_name: str,
        include_tools: bool = True
    ) -> Optional[dict[str, Any]]:
        """Get detailed status for a specific MCP server.

        Args:
            server_name: Name of the server.
            include_tools: If True, include full tool list.

        Returns:
            Server status dictionary with keys:
            - name: Server name
            - connected: Connection status
            - enabled: Whether server is enabled
            - type: Server type
            - description: Server description
            - url: Server URL (for external)
            - tools: List of tool names (if include_tools=True)
            - tool_count: Number of tools
            - error: Error message if connection failed

            None if server not found.
        """
        # Check if server exists in configuration
        if server_name not in self._mcp.configured_external_servers:
            logger.warning(f"Server '{server_name}' not found in configuration")
            return None

        server_config = self._mcp.configured_external_servers[server_name]

        # Try to get client
        client = None
        error = None
        try:
            client = await self._get_client_safe(server_name)
        except Exception as e:
            error = str(e)
            logger.debug(f"Failed to get client for {server_name}: {e}")

        is_connected = client is not None

        status = {
            "name": server_name,
            "connected": is_connected,
            "enabled": server_config.enabled,
            "type": "external",
            "description": server_config.description or server_name.replace('_', ' ').title(),
            "url": server_config.url if hasattr(server_config, 'url') else ""
        }

        if error:
            status["error"] = error

        # Get tools if connected and requested
        if include_tools:
            if is_connected and client:
                try:
                    tools = await client.list_tools()
                    tool_list = [tool.name for tool in tools] if tools else []
                    status["tools"] = tool_list
                    status["tool_count"] = len(tool_list)
                except Exception as e:
                    logger.debug(f"Failed to list tools for {server_name}: {e}")
                    status["tools"] = []
                    status["tool_count"] = 0
                    status["tools_error"] = str(e)
            else:
                status["tools"] = []
                status["tool_count"] = 0

        return status

    async def test_server(self, server_name: str) -> dict[str, Any]:
        """Test connectivity and basic functionality of an MCP server.

        Args:
            server_name: Name of the server to test.

        Returns:
            Test result dictionary with keys:
            - success: Whether test passed
            - connected: Connection status
            - tools_available: Number of tools available
            - response_time_ms: Response time in milliseconds
            - error: Error message (if failed)
        """
        import time

        # Check if server exists
        if server_name not in self._mcp.configured_external_servers:
            return {
                "success": False,
                "error": f"Server '{server_name}' not found in configuration"
            }

        server_config = self._mcp.configured_external_servers[server_name]

        if not server_config.enabled:
            return {
                "success": False,
                "error": f"Server '{server_name}' is disabled in configuration"
            }

        # Test connection
        start_time = time.time()
        try:
            client = await self._get_client_safe(server_name)
            if not client:
                return {
                    "success": False,
                    "connected": False,
                    "error": "Failed to establish connection"
                }

            # Try to list tools
            tools = await client.list_tools()
            response_time = (time.time() - start_time) * 1000  # ms

            return {
                "success": True,
                "connected": True,
                "tools_available": len(tools) if tools else 0,
                "response_time_ms": round(response_time, 2)
            }
        except Exception as e:
            response_time = (time.time() - start_time) * 1000  # ms
            logger.error(f"Server test failed for {server_name}: {e}")
            return {
                "success": False,
                "connected": False,
                "response_time_ms": round(response_time, 2),
                "error": str(e)
            }

    async def list_all_tools(
        self,
        server_name: Optional[str] = None,
        include_blocked: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        """List all available tools across MCP servers.

        Args:
            server_name: If specified, list tools only from this server.
            include_blocked: If True, include blocked tools (marked as such).
                            If False, filter out tools where blocked=True.

        Returns:
            Dictionary mapping server names to tool lists.
            Each tool dict contains: name, description, input_schema, blocked.
        """
        try:
            all_tools = await self._mcp.list_all_tools()

            # Filter by server if specified
            if server_name:
                external_tools = all_tools.get("external_servers", {})
                plugin_tools = all_tools.get("plugins", {})

                result = {}
                if server_name in external_tools:
                    tools = external_tools[server_name]
                    # Filter blocked tools if requested
                    if not include_blocked:
                        tools = [t for t in tools if not t.get("blocked", False)]
                    result[server_name] = tools
                elif server_name in plugin_tools:
                    tools = plugin_tools[server_name]
                    # Filter blocked tools if requested
                    if not include_blocked:
                        tools = [t for t in tools if not t.get("blocked", False)]
                    result[server_name] = tools

                return result

            # Return all tools
            result = {}
            external = all_tools.get("external_servers", {})
            plugins = all_tools.get("plugins", {})

            # Combine and filter blocked tools if requested
            all_servers = {}
            all_servers.update(external)
            all_servers.update(plugins)

            if not include_blocked:
                # Filter out blocked tools from each server
                for server, tools in all_servers.items():
                    result[server] = [t for t in tools if not t.get("blocked", False)]
            else:
                result = all_servers

            return result
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return {}

    async def _get_client_safe(self, server_name: str):
        """The live connection to *server_name*, or None.

        Tolerates a coroutine and a missing provider: this is called on status
        paths that must degrade to "not connected" rather than raise.
        """
        try:
            provider = getattr(self._mcp, "external_provider", None)
            pool = getattr(provider, "pool", None) if provider else None
            if pool is None:
                return None
            client = pool.get(server_name)
            if hasattr(client, '__await__'):
                client = await client
            return client
        except Exception as e:
            logger.debug(f"Exception getting client for {server_name}: {e}")
            return None

    def _check_plugin_connectivity(self, server_id: str, registry) -> bool:
        """Check if a plugin server is connected (present in registry).

        Args:
            server_id: Plugin server ID
            registry: MCPRegistry instance

        Returns:
            True if server exists in registry, False otherwise
        """
        if not registry or not hasattr(registry, "_servers"):
            return False
        return server_id in registry._servers

    async def _check_external_connectivity(self, server_name: str, url: str) -> bool:
        """Check if an external server is reachable via socket connection.

        Args:
            server_name: Server name for logging
            url: Server URL to check

        Returns:
            True if server is reachable, False otherwise
        """
        if not url:
            return False

        try:
            import socket
            from urllib.parse import urlparse

            parsed = urlparse(url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port

            if not port:
                # Default ports based on scheme
                if parsed.scheme == "https":
                    port = 443
                else:
                    port = 80

            # Try socket connection with short timeout. Off-loop on purpose:
            # a synchronous connect_ex (plus its DNS resolution, which
            # settimeout does not even cover) froze the whole event loop for
            # up to 2s per unreachable server on every status call.
            def _probe() -> bool:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)  # 2 second timeout
                try:
                    return sock.connect_ex((host, port)) == 0
                finally:
                    sock.close()

            return await asyncio.to_thread(_probe)
        except Exception as e:
            logger.debug(f"Connectivity check failed for {server_name}: {e}")
            return False

    async def get_comprehensive_status(
        self,
        registry=None,
        check_connectivity: bool = True
    ) -> dict[str, Any]:
        """Get comprehensive MCP server status including plugins and external servers.

        This method aggregates status from multiple sources:
        - Plugin servers from registry
        - External servers from MCP integration
        - Tool lists with filtering information
        - Connection status with optional real-time checks

        Args:
            registry: Optional MCPRegistry for plugin server discovery
            check_connectivity: If True, perform real-time connectivity checks

        Returns:
            Dictionary with keys:
            - plugins: Dict of plugin servers {id: {name, connected, tools, ...}}
            - external_servers: Dict of external servers
            - servers: Combined list (for backward compatibility)
            - total_servers: Total server count
            - total_tools: Total tool count across all servers
        """
        servers = []

        # Track processed servers to avoid duplicates
        processed_server_ids = set()

        # Add plugin servers from registry
        if registry and hasattr(registry, "_servers"):
            for server_id, server_obj in registry._servers.items():
                try:
                    # Skip if already processed or private
                    if server_id in processed_server_ids:
                        continue
                    if getattr(server_obj, '_mcp_public', True) is False:
                        continue

                    # Get tools.
                    #
                    # The registry holds the RAW plugin server. Only MCPServer
                    # subclasses carry list_tools(); a hybrid plugin (Web+MCP)
                    # is a plain class and exposes get_tools() -- or nothing at
                    # all, with its tools declared in a schema file next to it.
                    # PluginMCPAdapter is the piece that knows all three shapes,
                    # so ask it whenever the raw object cannot answer. Without
                    # this fallback 18 public plugins report tool_count 0
                    # (ssh_control, writer_player, log_viewer, ... = 22 tools).
                    #
                    # The _mcp_public check above deliberately stays on the RAW
                    # object: the adapter does not carry that flag, and reading
                    # it there would leak the 138 private agent servers into the
                    # admin UI again.
                    tools = []
                    detailed_tools = []
                    tool_source = server_obj
                    if not hasattr(tool_source, 'list_tools'):
                        plugin_reg = getattr(self._mcp, 'plugin_registry', None) if self._mcp else None
                        adapter = plugin_reg.get_server(server_id) if plugin_reg else None
                        if adapter is not None:
                            tool_source = adapter
                    if hasattr(tool_source, 'list_tools'):
                        try:
                            mcp_tools = await tool_source.list_tools()
                            if mcp_tools:
                                for mcp_tool in mcp_tools:
                                    tools.append(mcp_tool.name)
                                    detailed_tools.append({
                                        'name': mcp_tool.name,
                                        'description': mcp_tool.description,
                                        'parameters': mcp_tool.input_schema
                                    })
                        except Exception as e:
                            logger.debug(f"list_tools() failed for {server_id}: {e}")

                    # Check connection status
                    if check_connectivity:
                        # For plugins: check if in registry
                        connected = self._check_plugin_connectivity(server_id, registry)
                    else:
                        # Fast path: assume connected if in registry
                        connected = True  # In registry = connected

                    servers.append({
                        "id": server_id,
                        "name": server_id.replace('_', ' ').title(),
                        "type": "plugin",
                        "connected": connected,
                        "tools": tools,
                        "detailed_tools": detailed_tools,
                        "tool_count": len(tools)
                    })

                    processed_server_ids.add(server_id)

                except Exception as e:
                    logger.debug(f"Failed to get info for registry server {server_id}: {e}")

        # Add external servers from MCP integration
        if self._mcp and self._mcp.initialized:
            try:
                # Get connected servers from client manager
                connected_servers = self._mcp.list_external_clients()

                # Get configured external servers (filter out disabled ones)
                configured_servers = getattr(self._mcp, 'configured_external_servers', {})
                try:
                    configured_servers = {
                        name: cfg
                        for name, cfg in (configured_servers or {}).items()
                        if getattr(cfg, 'enabled', True)
                    }
                except Exception as e:
                    logger.warning(f"Failed to filter enabled servers: {e}", exc_info=True)

                # Get all tools from external servers
                all_tools = await self._mcp.list_all_tools()
                servers_with_tools = all_tools.get("external_servers", {})

                # Combine all external server names
                all_external_servers = set(connected_servers) | set(configured_servers.keys())

                for server_name in all_external_servers:
                    # Get server config info
                    description = server_name.replace('_', ' ').title()
                    url = ""

                    try:
                        server_config = configured_servers.get(server_name)
                        if server_config:
                            if hasattr(server_config, 'description') and server_config.description:
                                description = server_config.description
                            if hasattr(server_config, 'url') and server_config.url:
                                url = server_config.url
                    except Exception as e:
                        logger.debug(f"Failed to get config for {server_name}: {e}")

                    # Check if server is configured but not connected - try to reconnect
                    if check_connectivity and url and server_name not in connected_servers:
                        logger.debug(f"Server {server_name} is configured but has no active client, checking connectivity...")
                        reachable = await self._check_external_connectivity(server_name, url)
                        if reachable:
                            logger.info(f"Server {server_name} is reachable but not connected, attempting to connect...")
                            try:
                                success = await self._mcp.retry_connect_server(server_name)
                                if success:
                                    logger.info(f"Successfully connected to {server_name}")
                                    # Refresh connected servers and tools after successful connection
                                    connected_servers = self._mcp.list_external_clients()
                                    all_tools = await self._mcp.list_all_tools()
                                    servers_with_tools = all_tools.get("external_servers", {})
                            except Exception as e:
                                logger.warning(f"Failed to reconnect to {server_name}: {e}")

                    # Get tools
                    tools = servers_with_tools.get(server_name, [])
                    tool_names = [tool["name"] for tool in tools]
                    detailed_tools = [{
                        'name': tool.get("name", "unknown"),
                        'description': tool.get("description", f"Tool from {description}"),
                        # External tool dicts carry "input_schema" (provider
                        # contract); "parameters" never existed there and the
                        # .get default silently emptied every schema.
                        'parameters': tool.get("input_schema") or tool.get("parameters", {}),
                        'blocked': tool.get("blocked", False)
                    } for tool in tools]

                    # Check connection status
                    if check_connectivity and url:
                        # Perform real-time connectivity check (strict mode for refresh)
                        reachable = await self._check_external_connectivity(server_name, url)
                        connected = reachable  # Trust only the actual connectivity check
                    else:
                        # Fast path: check if has active client with tools
                        connected = server_name in connected_servers and len(tool_names) > 0

                    servers.append({
                        "id": server_name,
                        "name": description,
                        "type": "external",
                        "connected": connected,
                        "tools": tool_names,
                        "detailed_tools": detailed_tools,
                        "tool_count": len(tool_names),
                        "url": url
                    })

            except Exception as e:
                logger.error(f"Failed to get external servers: {e}")

        # Separate plugins and external servers
        plugins = {}
        external_servers = {}

        for server in servers:
            server_data = {
                "id": server["id"],
                "name": server["name"],
                "connected": server["connected"],
                "tools": server["tools"],
                "detailed_tools": server["detailed_tools"],
                "tool_count": server["tool_count"]
            }
            if "url" in server:
                server_data["url"] = server["url"]
            if "error" in server:
                server_data["error"] = server["error"]

            if server["type"] == "plugin":
                plugins[server["id"]] = server_data
            else:
                external_servers[server["id"]] = server_data

        return {
            "plugins": plugins,
            "external_servers": external_servers,
            "servers": servers,  # Keep for backward compatibility
            "total_servers": len(servers),
            "total_tools": sum(s["tool_count"] for s in servers)
        }
