from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, TYPE_CHECKING
from .core import MCPTool

if TYPE_CHECKING:
    from agent_system.config.models import AgentConfig


class MCPServer(ABC):
    name: str

    def __init__(self, name: str, agent_config: AgentConfig) -> None:
        self.name = name
        self.agent_config = agent_config

        # Extract SSL setting from AgentConfig
        self.ssl_verify = agent_config.network.ssl_verify if hasattr(agent_config, 'network') and agent_config.network else True

        # SSL verification extracted from agent config
        # Available for plugins that need HTTP client configuration

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

            # Inject request_id into status for plugins to check cancellation
            if request_id:
                params["_request_id"] = request_id

            return await self.call(action, params)

    async def list_tools(self) -> List["MCPTool"]:
        """List tools available from this MCP server.

        This is the unified interface. Plugins can either implement this directly
        or override get_tools() or get_schema() for legacy compatibility.
        """
        from .core import MCPTool

        # Try get_tools() method first (modern multi-tool interface)
        try:
            tool_schemas = self.get_tools()
            tools = []
            for tool_schema in tool_schemas:
                if isinstance(tool_schema, dict) and 'function' in tool_schema:
                    func_def = tool_schema['function']
                    tool = MCPTool(
                        name=func_def['name'],
                        description=func_def.get('description', f'Tool {func_def["name"]}'),
                        input_schema=func_def.get('parameters', {})
                    )
                    tools.append(tool)
            if tools:
                return tools
        except NotImplementedError:
            pass

        # Try get_schema() method (legacy single-tool interface)
        try:
            schema = self.get_schema()
            if isinstance(schema, dict) and 'function' in schema:
                func_def = schema['function']
                tool = MCPTool(
                    name=func_def['name'],
                    description=func_def.get('description', f'Tool {func_def["name"]}'),
                    input_schema=func_def.get('parameters', {})
                )
                return [tool]
        except NotImplementedError:
            pass

        # Final fallback: Use default action
        try:
            default_action = self.get_default_action()
            tool = MCPTool(
                name=default_action,
                description=f"Default action for {self.name}",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True
                }
            )
            return [tool]
        except NotImplementedError:
            pass

        raise NotImplementedError("Plugin must implement list_tools(), get_tools(), get_schema(), or get_default_action()")


    def get_default_action(self) -> str:
        """Return the default action name for this MCP server.

        For multi-tool plugins, this returns the name of the first tool.
        """
        # Default fallback for plugins that don't override this
        return self.name


class MCPRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}

    def register(self, name: str, server: MCPServer) -> None:
        self._servers[name] = server

    def get(self, name: str) -> MCPServer:
        return self._servers[name]

    def list(self) -> list[str]:
        return list(self._servers.keys())
