"""
Tool Service

Centralized tool management and filtering service.
Lists an external MCP server's tools and its blocked list.
The filter is configuration: it is edited in mcp_servers.yaml, not here.
"""
from __future__ import annotations

import logging
from typing import Any

from agent_system.tools.integration import ToolServerIntegration
from agent_system.config.models import AgentSystemConfig


logger = logging.getLogger(__name__)


class ToolService:
    """Centralized tool management service.
    
    This service provides unified tool management including:
    - Tool listing and filtering
    """

    def __init__(self, tool_integration: ToolServerIntegration, config: AgentSystemConfig):
        """Initialize the ToolService.
        
        Args:
            tool_integration: ToolServerIntegration instance.
            config: AgentSystemConfig instance.
        """
        self._mcp = tool_integration
        self._config = config
        logger.info("ToolService initialized")

    async def list_tools(
        self,
        server_name: str,
        include_filtering: bool = True
    ) -> dict[str, Any]:
        """List all available tools for a server with filtering information.
        
        Args:
            server_name: Name of the tool server.
            include_filtering: If True, include filtering configuration.
        
        Returns:
            Dictionary with keys:
            - server: Server name
            - available_tools: List of all available tool names
            - filtering: {"blocked_tools": [...]} (if include_filtering=True)
            - effective_tools: Tools not blocked -- what mcp_client will call
            - error: Error message if failed
        """
        # Check server exists
        if server_name not in self._mcp.configured_external_servers:
            return {"error": f"Server {server_name} not found in configuration"}
        
        server_config = self._mcp.configured_external_servers[server_name]
        
        # Only `blocked` is filtering for an external server: it is what
        # mcp_client's call_tool refuses. Its `tools.allowed` is enforced
        # nowhere -- which tools an agent may call is the agent's allowlist.
        blocked_tools: list[str] = (server_config.tools.blocked or []) if server_config.tools else []
        
        # Get available tools from server
        try:
            client = await self._get_client_safe(server_name)
            client_created = False
            
            if not client and server_config.enabled:
                # Connect just long enough to read the tool list.
                #
                # This used to hand add_client a plain dict where a
                # RemoteMCPConfig was expected -- the connection factory reads
                # config.transport/config.url as attributes, so the temporary
                # client could never come up. Connecting the configured server
                # through the pool uses the config object that is already there.
                await self._mcp.retry_connect_server(server_name)
                client = await self._get_client_safe(server_name)
                client_created = client is not None
            
            available_tools = []
            if client:
                try:
                    tools = await client.list_tools()
                    available_tools = [tool.name for tool in tools] if tools else []
                except Exception as e:
                    logger.error(f"Failed to list tools for {server_name}: {e}")
                    return {"error": f"Failed to list tools: {str(e)}"}
            
            effective_tools = [t for t in available_tools if t not in blocked_tools]
            
            result: dict[str, Any] = {
                "server": server_name,
                "available_tools": available_tools,
                "effective_tools": effective_tools
            }
            
            if include_filtering:
                result["filtering"] = {"blocked_tools": blocked_tools}
            
            # Clean up temporary client
            if client_created:
                try:
                    await self._mcp.remove_external_server(server_name)
                except Exception as cleanup_error:
                    logger.debug(f"Error cleaning up tool list client {server_name}: {cleanup_error}")
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to list tools for {server_name}: {e}")
            return {"error": f"Failed to list tools: {str(e)}"}

    async def _get_client_safe(self, server_name: str):
        """The live connection to *server_name*, or None.

        Tolerates a coroutine and a missing provider: callers here must degrade
        to "not connected", never raise.
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


