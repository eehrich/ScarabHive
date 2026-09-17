"""What a tool server is: its tool definitions, the base class plugins inherit, the registry.

A tool server is a plain Python object with ``call(tool, params)``. It speaks no
protocol -- the Model Context Protocol lives in the ``mcp_client`` plugin, which
talks to foreign servers through the official SDK.
"""

from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


class ToolServerCapability(Enum):
    """What a tool server offers."""
    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"


@dataclass
class ToolDef:
    """One tool: the name the model calls, what it does, and its JSON Schema."""
    name: str
    description: str
    input_schema: Dict[str, Any]  # JSON Schema for input validation


#: One status row's worth of text. The WebUI renders it on a single line
#: (`.progress-message` is nowrap + ellipsis), so anything past this is
#: invisible there and pure weight everywhere else.
#:
#: Deliberately the SAME number as `MAX_LINE` in
#: tests/plugins/test_status_end_lines.py, which holds every plugin's own end
#: line to it: an error and a success land in the same row, so two budgets for
#: one surface would just be a number nobody could justify. (No import across
#: that boundary -- production must not depend on a test module.)
_STATUS_MESSAGE_LIMIT = 140


def _error_result_message(result: Any) -> str | None:
    """The error text of a failed tool result, or None if it is not one.

    THREE conventions live side by side in the plugin fleet, counted
    2026-09-02 over ``src/plugins/**`` (tests excluded):

    * ``{"status": "error", ...}`` -- 342 sites in 23 plugins
    * ``{"error": ...}`` with no status at all -- 130 sites, debate_forum and
      comfyui alone account for 53
    * ``{"success": False, "error": ...}`` -- 35 sites in 5 plugins

    The bare-``error`` rule is the one ``tool_script`` already applies when it
    decides whether a scripted tool call failed (``server.py``, "Tools signal
    failure by RETURN VALUE, in two shapes"): an ``error`` counts only when
    there is no ``status`` beside it, so a result that says
    ``{"status": "failed", "error": [...]}`` about the THING it queried is not
    mistaken for a failed call.

    ``success: False`` requires an ``error`` key for the same reason: on its
    own the flag also carries legitimate negative ANSWERS, where the call did
    its job and the answer is "no" -- ``mcp_client.disconnect`` returns
    ``{"success": False, "message": "was not connected"}`` for a server that
    was not connected.
    """
    if not isinstance(result, dict):
        return None
    # A user cancellation is not a failure this net reports. The fleet is
    # split on how it says so -- 11 sites return {"error": "...cancelled by
    # user", "cancelled": True}, 20 return {"status": "cancelled"} -- and the
    # second shape is invisible here. Flagging only the first would render the
    # SAME user action red in one plugin and green in the next; before this
    # net they all closed alike. Plugins that want a cancellation to show red
    # already do it themselves (terminal, basic_operations,
    # sub_agent_manager). Unifying the two shapes is a decision about what a
    # cancelled call should look like, not a status-line repair.
    if result.get("cancelled") is True:
        return None
    status = result.get("status")
    error = result.get("error")
    if status == "error":
        message = error or result.get("message") or "failed"
    elif error and status is None and result.get("success") is not True:
        message = error
    elif result.get("success") is False and error:
        message = error
    else:
        return None
    # Capped: the value is a tool's, and a structured `error` (a stack trace,
    # a list of node messages) would otherwise land whole in the status
    # stream. The status row is one line -- the full payload is in the result
    # the caller already has. This caps only what THIS net publishes; a plugin
    # that calls status.error itself is responsible for its own length.
    text = str(message)
    return text if len(text) <= _STATUS_MESSAGE_LIMIT else text[:_STATUS_MESSAGE_LIMIT - 3] + "..."


class ToolServer(ABC):
    """Base class for tool servers -- what every plugin inherits.

    - system_config: Complete system configuration (network, logging, LLM, etc.)
    - server_config: this instance's own entry (enabled, type, agent_config overrides)
    """
    name: str

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        self.name = name
        self.system_config = system_config
        self.server_config = server_config
        # Cache for list_tools() to avoid creating new ToolDef objects on every call
        self._list_tools_cache: List[ToolDef] | None = None

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
        for compatibility with different callers.
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
            # try/except around the whole net: it inspects a value the TOOL
            # produced, so a dict subclass with a throwing ``get`` or an
            # object with a throwing ``__bool__`` could turn a call that
            # returned cleanly into a raise. Reporting must never break the
            # operation it reports on -- the same contract every method on
            # StatusScope already keeps.
            try:
                if not status.ended:
                    message = _error_result_message(result)
                    if message is not None:
                        error_type = result.get("error_type")
                        await status.error(
                            message,
                            meta={"error_type": error_type} if error_type else None)
            except Exception:  # pragma: no cover - reporting must not throw
                logging.getLogger(__name__).debug(
                    "Could not report the error result of %s", action, exc_info=True)

            return result

    async def list_tools(self) -> List[ToolDef]:
        """List tools available from this tool server.
        
        Plugins must implement this method directly or override get_tools() 
        to return a list of tool schemas in OpenAI function calling format.
        
        This method applies custom self_tool_descriptions from server_config if configured.
        Results are cached to avoid creating new ToolDef objects on every call.
        """
        # Return cached tools if available
        if self._list_tools_cache is not None:
            return self._list_tools_cache

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
                        tool = ToolDef(
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
        """Apply custom self tool descriptions from tool server configuration.
        
        Allows instances to override tool descriptions without modifying the base plugin code.
        For example, a sysadmin_agent based on basic_agent can customize the description
        of sysadmin_agent_execute_task to better reflect its SSH capabilities.
        
        Args:
            tools_schema: List of tool schemas to modify in-place
        """
        logger = logging.getLogger(__name__)

        if not self.server_config or not hasattr(self.server_config, 'self_tool_descriptions'):
            return
        
        self_tool_descriptions = getattr(self.server_config, 'self_tool_descriptions', None)
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
                    f"Tool server '{self.name}': self_tool_descriptions contains non-existent tool '{tool_name}'. "
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
                            f"Tool server '{self.name}': Overriding self tool description for '{tool_name}': "
                            f"'{old_desc[:50]}...' -> '{new_desc[:50]}...'"
                        )
                        break
        
        if applied_count > 0:
            logger.info(
                f"Tool server '{self.name}': Applied {applied_count} custom self tool description(s)"
            )




class ToolServerRegistry:
    """The built servers, and -- when bound to a Runtime -- what is declared.

    Unbound it is the plain dict it always was; every test that builds one by
    hand keeps its behaviour to the letter. Bound, it can answer questions
    about servers that are DECLARED but not built, which is what lets the
    per-request walkers stop constructing everything they look at.
    """

    def __init__(self) -> None:
        self._servers: dict[str, ToolServer] = {}
        self._runtime: Any = None

    def bind(self, runtime: Any) -> None:
        """Called by Runtime.__init__ for the registry it owns."""
        self._runtime = runtime

    def register(self, name: str, server: ToolServer) -> None:
        self._servers[name] = server

    def get(self, name: str) -> ToolServer:
        # Deliberately NOT materializing yet: the lazy build belongs to the
        # stage that stops building at start (B5). Until then every declared
        # server is built, and `resolve_longest_prefix` probes names that do
        # not exist and relies on the KeyError.
        return self._servers[name]

    def describe(self, name: str) -> Any:
        """``ServerView`` for a walker, or None when unbound/unknown/unsafe.

        None is the honest answer for an unbound registry: it knows nothing
        beyond what it holds, and a caller must fall back to the instance.
        ``Runtime.view`` returns None for the same reason whenever a
        declaration cannot answer for its instance.
        """
        if self._runtime is None:
            return None
        return self._runtime.view(name)

    def list(self) -> list[str]:
        """The BUILT servers -- unchanged, bound or not.

        It is tempting to add the declared-but-unbuilt names here, and the
        lazy start will need exactly that. It cannot come alone: SIX places
        ask ``if name in registry.list()`` and then ``registry.get(name)``, so
        a name that lists but does not get turns a clean "Unknown tool" into a
        KeyError out of a tool call. ``list()`` and ``get()`` move together,
        in the stage that stops building at start -- and all six move with
        them. They are, in ``src/agent_system/``:

            app.py (twice), agent_cli.py,
            servers/agent/components/tool_execution.py (three times)

        No line numbers on purpose, they rot. The whole set is
        ``grep -rn "in .*registry\\.list()" src/agent_system/``, minus the
        plain ``for name in registry.list()`` walks, which are safe.
        """
        return list(self._servers.keys())
