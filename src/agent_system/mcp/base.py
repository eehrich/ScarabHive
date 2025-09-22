from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MCPServer(ABC):
    name: str

    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        self.name = name
        self.config = config or {}
        self.ssl_verify = ssl_verify

    @abstractmethod
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        ...

    async def call_with_status(self, action: str, params: dict[str, Any]):
        """Call tool with automatic StatusScope management
        
        Supports both 'request_id' (Python convention) and 'requestId' (JS convention)
        for compatibility with different MCP clients.
        """
        from .status import get_status_bus, status_scope
        
        status_bus = get_status_bus()
        # Support both snake_case and camelCase request_id for compatibility
        # Prefer request_id (Python convention) but fallback to requestId (JS convention)
        request_id = params.get("request_id") or params.get("requestId")
        
        async with status_scope(status_bus, self.name, request_id=request_id) as status:
            # Inject status object for the plugin to use
            params["_status"] = status
            return await self.call(action, params)

    def get_tools(self) -> list[dict[str, Any]]:
        """Return a list of OpenAI function schemas for this MCP server's tools.
        
        Default implementation returns a single tool from get_schema() for backward compatibility.
        Override this method to provide multiple tools.
        """
        # Check if this class has overridden get_schema (not the base implementation)
        if self.__class__.get_schema is not MCPServer.get_schema:
            # Plugin has implemented get_schema, use it
            return [self.get_schema()]
        else:
            # Neither get_tools nor get_schema is implemented
            raise NotImplementedError("Plugin must implement either get_schema() or get_tools()")

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for this MCP server's tools.
        
        This method is kept for backward compatibility. New plugins should override
        get_tools() instead to provide multiple tools.
        """
        # Check if this class has overridden get_tools (not the base implementation)
        if self.__class__.get_tools is not MCPServer.get_tools:
            # Plugin has implemented get_tools, use it
            tools = self.get_tools()
            if len(tools) == 1:
                return tools[0]
            elif len(tools) == 0:
                raise NotImplementedError("Plugin must implement either get_schema() or get_tools()")
            else:
                # Return the first tool as the "default" schema for backward compatibility
                return tools[0]
        else:
            # Neither get_tools nor get_schema is implemented
            raise NotImplementedError("Plugin must implement either get_schema() or get_tools()")

    def get_default_action(self) -> str:
        """Return the default action name for this MCP server.
        
        For multi-tool plugins, this should return the name of the primary/default tool.
        """
        # Try to extract from the first tool schema
        try:
            tools = self.get_tools()
            if tools and 'function' in tools[0] and 'name' in tools[0]['function']:
                return tools[0]['function']['name']
        except NotImplementedError:
            pass
        
        # Fallback to abstract method for backward compatibility
        return self._get_default_action_impl()
    
    def _get_default_action_impl(self) -> str:
        """Override this method if not implementing get_tools() with proper tool names."""
        raise NotImplementedError("Plugin must implement get_default_action or provide tools with names")


class MCPRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}

    def register(self, name: str, server: MCPServer) -> None:
        self._servers[name] = server

    def get(self, name: str) -> MCPServer:
        return self._servers[name]

    def list(self) -> list[str]:
        return list(self._servers.keys())
