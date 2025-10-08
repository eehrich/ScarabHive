"""
MCP Service

Centralized MCP (Model Context Protocol) server management. This service
provides a unified interface for MCP operations used by both CLI and API.
"""
from __future__ import annotations

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
    - Server connection/disconnection
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
            
            server_info = {
                "name": name,
                "enabled": server_config.enabled,
                "connected": is_connected,
                "type": "external",
                "description": server_config.description or name.replace('_', ' ').title(),
                "url": server_config.url if hasattr(server_config, 'url') else ""
            }
            
            # Add tool information if requested
            if include_tools and is_connected:
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

    async def connect_server(self, server_name: str) -> dict[str, Any]:
        """Connect to an MCP server.
        
        Args:
            server_name: Name of the server to connect.
        
        Returns:
            Result dictionary with keys:
            - success: Boolean indicating success
            - message: Status message
            - error: Error message (if failed)
        """
        # Check if server exists
        if server_name not in self._mcp.configured_external_servers:
            return {
                "success": False,
                "error": f"Server '{server_name}' not found in configuration"
            }
        
        server_config = self._mcp.configured_external_servers[server_name]
        
        # Check if already connected
        try:
            client = await self._get_client_safe(server_name)
            if client:
                return {
                    "success": True,
                    "message": f"Server '{server_name}' is already connected"
                }
        except Exception:
            pass  # Not connected, proceed
        
        # Attempt connection
        try:
            # Use MCPIntegration's connect method if available
            if hasattr(self._mcp, 'connect_external_server'):
                await self._mcp.connect_external_server(server_name, server_config)
            else:
                # Fallback: try to get client (may trigger auto-connect)
                client = await self._get_client_safe(server_name)
                if not client:
                    raise RuntimeError("Failed to establish connection")
            
            return {
                "success": True,
                "message": f"Successfully connected to '{server_name}'"
            }
        except Exception as e:
            logger.error(f"Failed to connect to {server_name}: {e}")
            return {
                "success": False,
                "error": f"Connection failed: {str(e)}"
            }

    async def disconnect_server(self, server_name: str) -> dict[str, Any]:
        """Disconnect from an MCP server.
        
        Args:
            server_name: Name of the server to disconnect.
        
        Returns:
            Result dictionary with keys:
            - success: Boolean indicating success
            - message: Status message
            - error: Error message (if failed)
        """
        try:
            # Check if client exists
            client = await self._get_client_safe(server_name)
            if not client:
                return {
                    "success": True,
                    "message": f"Server '{server_name}' is not connected"
                }
            
            # Disconnect
            if hasattr(self._mcp, 'disconnect_external_server'):
                await self._mcp.disconnect_external_server(server_name)
            elif hasattr(self._mcp.client_manager, 'remove_client'):
                await self._mcp.client_manager.remove_client(server_name)
            else:
                raise RuntimeError("Disconnect method not available")
            
            return {
                "success": True,
                "message": f"Successfully disconnected from '{server_name}'"
            }
        except Exception as e:
            logger.error(f"Failed to disconnect from {server_name}: {e}")
            return {
                "success": False,
                "error": f"Disconnection failed: {str(e)}"
            }

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
        
        Returns:
            Dictionary mapping server names to tool lists.
            Each tool dict contains: name, description, parameters, blocked.
        """
        try:
            all_tools = await self._mcp.list_all_tools()
            
            # Filter by server if specified
            if server_name:
                external_tools = all_tools.get("external_servers", {})
                plugin_tools = all_tools.get("plugin_servers", {})
                
                result = {}
                if server_name in external_tools:
                    result[server_name] = external_tools[server_name]
                elif server_name in plugin_tools:
                    result[server_name] = plugin_tools[server_name]
                
                return result
            
            # Return all tools
            result = {}
            external = all_tools.get("external_servers", {})
            plugins = all_tools.get("plugin_servers", {})
            
            result.update(external)
            result.update(plugins)
            
            return result
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return {}

    async def _get_client_safe(self, server_name: str):
        """Safely get client, handling both sync and async patterns."""
        try:
            # Try to get client from client manager
            if hasattr(self._mcp, 'client_manager'):
                client = self._mcp.client_manager.get_client(server_name)
                # Handle potential coroutine
                if hasattr(client, '__await__'):
                    client = await client
                return client
            return None
        except Exception as e:
            logger.debug(f"Exception getting client for {server_name}: {e}")
            return None
