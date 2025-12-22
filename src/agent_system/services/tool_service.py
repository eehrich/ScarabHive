"""
Tool Service

Centralized tool management and filtering service.
Handles tool blocking, allowing, and filtering across MCP servers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from pathlib import Path
import yaml

from agent_system.mcp.integration import MCPIntegration
from agent_system.config.models import AgentSystemConfig
from agent_system.utils.io import atomic_write_text


logger = logging.getLogger(__name__)

# Global lock for config file modifications to prevent race conditions
_config_file_locks: dict[Path, asyncio.Lock] = {}


def _get_config_lock(config_path: Path) -> asyncio.Lock:
    """Get or create a lock for a specific config file path.
    
    Args:
        config_path: Path to the config file
        
    Returns:
        asyncio.Lock for that file
    """
    # Resolve to absolute path to avoid duplicates
    resolved_path = config_path.resolve()
    if resolved_path not in _config_file_locks:
        _config_file_locks[resolved_path] = asyncio.Lock()
    return _config_file_locks[resolved_path]


class ToolService:
    """Centralized tool management service.
    
    This service provides unified tool management including:
    - Tool listing and filtering
    - Tool blocking/allowing
    - Tool permission management
    - Configuration file updates
    """

    def __init__(self, mcp_integration: MCPIntegration, config: AgentSystemConfig):
        """Initialize the ToolService.
        
        Args:
            mcp_integration: MCPIntegration instance.
            config: AgentSystemConfig instance.
        """
        self._mcp = mcp_integration
        self._config = config
        logger.info("ToolService initialized")

    async def list_tools(
        self,
        server_name: str,
        include_filtering: bool = True
    ) -> dict[str, Any]:
        """List all available tools for a server with filtering information.
        
        Args:
            server_name: Name of the MCP server.
            include_filtering: If True, include filtering configuration.
        
        Returns:
            Dictionary with keys:
            - server: Server name
            - available_tools: List of all available tool names
            - filtering: Filtering configuration (if include_filtering=True)
            - effective_tools: Tools after filtering applied
            - error: Error message if failed
        """
        # Check server exists
        if server_name not in self._mcp.configured_external_servers:
            return {"error": f"Server {server_name} not found in configuration"}
        
        server_config = self._mcp.configured_external_servers[server_name]
        
        # Get filtering configuration
        allowed_tools = server_config.tools.allowed if server_config.tools else None
        blocked_tools = server_config.tools.blocked if server_config.tools else None
        
        # Get available tools from server
        try:
            client = await self._get_client_safe(server_name)
            client_created = False
            
            if not client and server_config.enabled:
                # Create temporary client to list tools
                client_config = {
                    "transport": server_config.transport,
                    "url": server_config.url
                }
                
                if server_config.initialization_options:
                    client_config["initialization_options"] = server_config.initialization_options
                
                await self._mcp.client_manager.add_client(server_name, client_config)
                client = await self._get_client_safe(server_name)
                client_created = True
            
            available_tools = []
            if client:
                try:
                    tools = await client.list_tools()
                    available_tools = [tool.name for tool in tools] if tools else []
                except Exception as e:
                    logger.error(f"Failed to list tools for {server_name}: {e}")
                    return {"error": f"Failed to list tools: {str(e)}"}
            
            # Calculate effective tools based on filtering
            # Note: Empty allowed_tools list means no filtering (pass through)
            # Non-empty allowed_tools list means whitelist filtering
            if allowed_tools:  # Only filter if allowed list has items
                effective_tools = [t for t in available_tools if t in allowed_tools]
            elif blocked_tools:
                effective_tools = [t for t in available_tools if t not in blocked_tools]
            else:
                effective_tools = available_tools
            
            result = {
                "server": server_name,
                "available_tools": available_tools,
                "effective_tools": effective_tools
            }
            
            if include_filtering:
                result["filtering"] = {
                    "allowed_tools": allowed_tools,
                    "blocked_tools": blocked_tools
                }
            
            # Clean up temporary client
            if client_created:
                try:
                    await self._mcp.client_manager.remove_client(server_name)
                except Exception as cleanup_error:
                    logger.debug(f"Error cleaning up tool list client {server_name}: {cleanup_error}")
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to list tools for {server_name}: {e}")
            return {"error": f"Failed to list tools: {str(e)}"}

    async def block_tool(
        self,
        server_name: str,
        tool_name: str,
        config_path: Optional[Path] = None
    ) -> dict[str, Any]:
        """Block a tool from being used on a server.
        
        Args:
            server_name: Name of the MCP server.
            tool_name: Name of the tool to block.
            config_path: Path to config file (default: config/mcp_servers.yaml).
        
        Returns:
            Dictionary with keys:
            - success: Boolean indicating success
            - message: Status message
            - server: Server name
            - tool: Tool name
            - error: Error message (if failed)
        """
        # Check server exists
        if server_name not in self._mcp.configured_external_servers:
            return {
                "success": False,
                "error": f"Server {server_name} not found in configuration"
            }
        
        cfg_path = config_path or Path("config/mcp_servers.yaml")
        if not cfg_path.exists():
            return {
                "success": False,
                "error": f"Configuration file {cfg_path} not found"
            }
        
        # Acquire lock to prevent race conditions on file operations
        lock = _get_config_lock(cfg_path)
        async with lock:
            try:
                # Load raw YAML (preserve formatting and comments)
                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to read config: {str(e)}"
                }
            
            # Navigate YAML structure (new structure: top-level external_servers key)
            external_servers_block = raw.get("external_servers", {})
            remote_servers = external_servers_block.get("remote_servers", {})
            
            if server_name not in remote_servers:
                return {
                    "success": False,
                    "error": f"Server {server_name} not found in config file"
                }
            
            server_cfg = remote_servers[server_name] or {}
            tools_dict = server_cfg.get("tools", {})
            if not tools_dict:
                tools_dict = {}
                server_cfg["tools"] = tools_dict
            
            allowed = list(tools_dict.get("allowed") or [])
            blocked = list(tools_dict.get("blocked") or [])
            
            # Check if already blocked
            if tool_name in blocked:
                return {
                    "success": True,
                    "message": "Tool already blocked",
                    "server": server_name,
                    "tool": tool_name
                }
            
            # Remove from allowed if present
            if tool_name in allowed:
                allowed.remove(tool_name)
                tools_dict["allowed"] = allowed
            
            # Add to blocked
            blocked.append(tool_name)
            tools_dict["blocked"] = blocked
            
            # Update YAML structure (new structure: top-level external_servers)
            remote_servers[server_name] = server_cfg
            external_servers_block["remote_servers"] = remote_servers
            raw["external_servers"] = external_servers_block
            
            # Write back to file
            try:
                data = yaml.safe_dump(raw, sort_keys=False)
                atomic_write_text(cfg_path, data)
                
                return {
                    "success": True,
                    "message": "Tool blocked successfully",
                    "server": server_name,
                    "tool": tool_name
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to write config: {str(e)}"
                }

    async def allow_tool(
        self,
        server_name: str,
        tool_name: str,
        config_path: Optional[Path] = None
    ) -> dict[str, Any]:
        """Allow a tool to be used on a server (unblock or add to allowed list).
        
        Args:
            server_name: Name of the MCP server.
            tool_name: Name of the tool to allow.
            config_path: Path to config file (default: config/mcp_servers.yaml).
        
        Returns:
            Dictionary with keys:
            - success: Boolean indicating success
            - message: Status message
            - server: Server name
            - tool: Tool name
            - error: Error message (if failed)
        """
        # Check server exists
        if server_name not in self._mcp.configured_external_servers:
            return {
                "success": False,
                "error": f"Server {server_name} not found in configuration"
            }
        
        cfg_path = config_path or Path("config/mcp_servers.yaml")
        if not cfg_path.exists():
            return {
                "success": False,
                "error": f"Configuration file {cfg_path} not found"
            }
        
        # Acquire lock to prevent race conditions on file operations
        lock = _get_config_lock(cfg_path)
        async with lock:
            try:
                # Load raw YAML (preserve formatting and comments)
                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to read config: {str(e)}"
                }
            
            # Navigate YAML structure (new structure: top-level external_servers key)
            external_servers_block = raw.get("external_servers", {})
            remote_servers = external_servers_block.get("remote_servers", {})
            
            if server_name not in remote_servers:
                return {
                    "success": False,
                    "error": f"Server {server_name} not found in config file"
                }
            
            server_cfg = remote_servers[server_name] or {}
            tools_dict = server_cfg.get("tools", {})
            if not tools_dict:
                tools_dict = {}
                server_cfg["tools"] = tools_dict
            
            allowed = list(tools_dict.get("allowed") or [])
            blocked = list(tools_dict.get("blocked") or [])
            
            # Check if already allowed
            if tool_name in allowed:
                return {
                    "success": True,
                    "message": "Tool already allowed",
                    "server": server_name,
                    "tool": tool_name
                }
            
            # Remove from blocked if present
            if tool_name in blocked:
                blocked.remove(tool_name)
                tools_dict["blocked"] = blocked
            
            # Add to allowed
            allowed.append(tool_name)
            tools_dict["allowed"] = allowed
            
            # Update YAML structure (new structure: top-level external_servers)
            remote_servers[server_name] = server_cfg
            external_servers_block["remote_servers"] = remote_servers
            raw["external_servers"] = external_servers_block
            
            # Write back to file
            try:
                data = yaml.safe_dump(raw, sort_keys=False)
                atomic_write_text(cfg_path, data)
                
                return {
                    "success": True,
                    "message": "Tool allowed successfully",
                    "server": server_name,
                    "tool": tool_name
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to write config: {str(e)}"
                }

    async def get_tool_status(
        self,
        server_name: str,
        tool_name: str
    ) -> dict[str, Any]:
        """Get the status of a specific tool (allowed/blocked/neutral).
        
        Args:
            server_name: Name of the MCP server.
            tool_name: Name of the tool.
        
        Returns:
            Dictionary with keys:
            - server: Server name
            - tool: Tool name
            - status: "allowed", "blocked", or "neutral"
            - available: Whether tool exists on server
            - error: Error message (if failed)
        """
        if server_name not in self._mcp.configured_external_servers:
            return {"error": f"Server {server_name} not found in configuration"}
        
        server_config = self._mcp.configured_external_servers[server_name]
        
        # Get filtering configuration
        allowed_tools = server_config.tools.allowed if server_config.tools else None
        blocked_tools = server_config.tools.blocked if server_config.tools else None
        
        # Determine status
        if blocked_tools and tool_name in blocked_tools:
            status = "blocked"
        elif allowed_tools is not None and tool_name in allowed_tools:
            status = "allowed"
        elif allowed_tools is not None:
            # If allowed list exists but tool not in it, it's blocked
            status = "blocked"
        else:
            status = "neutral"
        
        return {
            "server": server_name,
            "tool": tool_name,
            "status": status
        }

    async def _get_client_safe(self, server_name: str):
        """Safely get client, handling both sync and async patterns."""
        try:
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


