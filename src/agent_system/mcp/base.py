from __future__ import annotations

from abc import ABC
from typing import Any, List, TYPE_CHECKING
from .core import MCPTool

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


def _error_result_message(result: Any) -> str | None:
    """The error text of a failed tool result, or None if it is not one.

    Two conventions live side by side in the plugin fleet, counted 2026-09-02
    over ``src/plugins/**`` (tests excluded): ``{"status": "error"}`` at 342
    sites in 23 plugins, ``{"success": False}`` at 35 sites in 5. Both are
    recognised here so the safety net in ``call_with_status`` does not depend
    on which one a plugin happens to use.

    ``success: False`` additionally requires an ``error`` key: on its own the
    flag also carries legitimate negative ANSWERS, where the call did its job
    and the answer is "no" -- ``mcp_client.disconnect`` returns
    ``{"success": False, "message": "was not connected"}`` for a server that
    was not connected. ``status: "error"`` is unambiguous by its own name and
    needs no such qualifier.
    """
    if not isinstance(result, dict):
        return None
    if result.get("status") == "error":
        return str(result.get("error") or result.get("message") or "failed")
    if result.get("success") is False and result.get("error"):
        return str(result["error"])
    return None


class MCPServer(ABC):
    """Base class for MCP servers.
    
    Modern interface: receives both system-wide config and MCP-specific config.
    - system_config: Complete system configuration (network, logging, LLM, etc.)
    - mcp_config: MCP-specific configuration (enabled, type, agent_config overrides)
    """
    name: str

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        # Cache for list_tools() to avoid creating new MCPTool objects on every call
        self._list_tools_cache: List[MCPTool] | None = None

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Generic tool dispatcher that routes to tool methods by name.
        
        Automatically calls the method with the same name as the tool.
        Derived classes just need to implement methods matching their tool names.
        
        Example:
            If get_tools() returns a tool named "search_tweets",
            this will call self.search_tweets(params)
        """
        # Check if the tool method exists
        if not hasattr(self, tool):
            raise ValueError(f"Tool '{tool}' not found in {self.name}. Available tools: {self._get_available_tool_names()}")
        
        method = getattr(self, tool)
        
        # Verify it's callable
        if not callable(method):
            raise ValueError(f"Tool '{tool}' exists but is not callable in {self.name}")
        
        # Call the tool method
        # Support both sync and async methods
        import asyncio
        if asyncio.iscoroutinefunction(method):
            return await method(params)
        else:
            return method(params)
    
    def _get_available_tool_names(self) -> list[str]:
        """Helper to get list of available tool names for error messages."""
        try:
            tools = self.get_tools()
            return [t['function']['name'] for t in tools if isinstance(t, dict) and 'function' in t]
        except Exception:
            return []

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

        # Extract method name from action (remove plugin prefix if present)
        # Example: "writer_path_validate" -> "validate()"
        method_name = action.replace(f"{self.name}_", "") if action.startswith(f"{self.name}_") else action
        scope_name = f"{self.name}.{method_name}()"

        async with status_scope(status_bus, scope_name, request_id=request_id) as status:
            # Create a copy to avoid mutating the original params
            params_with_status = params.copy()
            
            # Inject status object for the plugin to use
            params_with_status["_status"] = status

            # Inject request_id into status for plugins to check cancellation
            if request_id:
                params_with_status["_request_id"] = request_id
            
            # Inject session_id if provided by agent (for session-aware plugins)
            if "_session_id" in params:
                params_with_status["_session_id"] = params["_session_id"]

            result = await self.call(action, params_with_status)

            # Safety net for the whole plugin fleet: a handler that RETURNS an
            # error result without reporting it leaves the scope to close with
            # its default END "completed" -- the failure then reads as a
            # success in CLI and WebUI. Audited 2026-09-02: 19 of 45 plugins
            # had at least one such path. Only fires when the handler said
            # nothing itself, so a plugin's own status.error/end always wins.
            if not status.ended:
                message = _error_result_message(result)
                if message is not None:
                    meta = {"error_type": result.get("error_type")} if result.get("error_type") else None
                    await status.error(message, meta=meta)

            return result

    async def list_tools(self) -> List[MCPTool]:
        """List tools available from this MCP server.
        
        Plugins must implement this method directly or override get_tools() 
        to return a list of tool schemas in OpenAI function calling format.
        
        This method applies custom self_tool_descriptions from mcp_config if configured.
        Results are cached to avoid creating new MCPTool objects on every call.
        """
        # Return cached tools if available
        if self._list_tools_cache is not None:
            return self._list_tools_cache

        from .core import MCPTool

        # Try get_tools() method
        if hasattr(self, 'get_tools'):
            try:
                tool_schemas = self.get_tools()
                
                # Apply custom self_tool_descriptions if configured
                self._apply_custom_tool_descriptions(tool_schemas)
                
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
                
                # Cache the result
                self._list_tools_cache = tools
                return tools
            except NotImplementedError:
                pass

        raise NotImplementedError(
            f"Plugin {self.name} must implement list_tools() or get_tools() to provide tool schemas"
        )

    def _apply_custom_tool_descriptions(self, tools_schema: List[dict]) -> None:
        """Apply custom self tool descriptions from MCP configuration.
        
        Allows instances to override tool descriptions without modifying the base plugin code.
        For example, a sysadmin_agent based on basic_agent can customize the description
        of sysadmin_agent_execute_task to better reflect its SSH capabilities.
        
        Args:
            tools_schema: List of tool schemas to modify in-place
        """
        import logging
        logger = logging.getLogger(__name__)
        
        if not self.mcp_config or not hasattr(self.mcp_config, 'self_tool_descriptions'):
            return
        
        self_tool_descriptions = getattr(self.mcp_config, 'self_tool_descriptions', None)
        if not self_tool_descriptions:
            return
        
        # Build set of available tool names for validation
        available_tool_names = set()
        for tool_schema in tools_schema:
            if tool_schema.get("type") == "function" and "function" in tool_schema:
                tool_name = tool_schema["function"].get("name")
                if tool_name:
                    available_tool_names.add(tool_name)
        
        # Apply custom descriptions and warn about non-existent tools
        applied_count = 0
        for tool_name, new_desc in self_tool_descriptions.items():
            if tool_name not in available_tool_names:
                logger.warning(
                    f"MCP Server '{self.name}': self_tool_descriptions contains non-existent tool '{tool_name}'. "
                    f"Available tools: {sorted(available_tool_names)}"
                )
                continue
            
            # Find and update the tool schema
            for tool_schema in tools_schema:
                if tool_schema.get("type") == "function" and "function" in tool_schema:
                    if tool_schema["function"].get("name") == tool_name:
                        old_desc = tool_schema["function"].get("description", "")
                        tool_schema["function"]["description"] = new_desc
                        applied_count += 1
                        logger.debug(
                            f"MCP Server '{self.name}': Overriding self tool description for '{tool_name}': "
                            f"'{old_desc[:50]}...' -> '{new_desc[:50]}...'"
                        )
                        break
        
        if applied_count > 0:
            logger.info(
                f"MCP Server '{self.name}': Applied {applied_count} custom self tool description(s)"
            )




class MCPRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServer] = {}

    def register(self, name: str, server: MCPServer) -> None:
        self._servers[name] = server

    def get(self, name: str) -> MCPServer:
        return self._servers[name]

    def list(self) -> list[str]:
        return list(self._servers.keys())
