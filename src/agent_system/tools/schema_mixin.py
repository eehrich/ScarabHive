"""Schema-based Tool Mixin for tool servers and agents with tool support.

This mixin extends SchemaBaseMixin to add tool-specific functionality:
- Tool loading from schema.yaml
- Generic tool dispatcher (routes calls to methods automatically)
- Tool validation and error handling

For non-tool components (hooks, web UI), use SchemaBasedHookMixin or SchemaBasedWebMixin.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, TYPE_CHECKING

from agent_system.core.schema_base_mixin import SchemaBaseMixin

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SchemaBasedToolMixin(SchemaBaseMixin):
    """Mixin providing schema-based tool loading and dispatching.

    This mixin can be used by any ToolServer subclass to add automatic
    schema.yaml loading and generic tool dispatching.

    Classes using this mixin should:
    1. Call super().__init__() to initialize caches
    2. Optionally override get_template_vars() for custom template variables
    3. Optionally override _get_method_name() for custom tool → method routing
    4. Implement methods matching their tool names

    Example:
        class MyServer(ToolServer, SchemaBasedToolMixin):
            def __init__(self, ...):
                super().__init__(...)
                self._init_schema_mixin()

            async def my_tool(self, params: dict) -> Any:
                # Tool implementation
                pass
    """

    # Type hints for attributes that will be provided by ToolServer
    name: str

    def _init_schema_mixin(self) -> None:
        """Initialize schema mixin caches.

        Must be called in __init__ of classes using this mixin.
        """
        self._init_schema_base()  # Initialize base schema cache
        self._tools_cache: list[dict[str, Any]] | None = None

    def get_tools(self) -> list[dict[str, Any]]:
        """Load tools from the plugin's schema.yaml file.

        This method automatically loads and parses the schema.yaml file
        from the plugin's directory.

        Returns:
            List of tool definitions in OpenAI function format.

        Raises:
            RuntimeError: If schema is missing, invalid, or doesn't use multi-tool format.
        """
        if self._tools_cache is not None:
            return self._tools_cache

        schema_data = self._load_schema()

        # Only support multi-tool format
        if 'tools' not in schema_data:
            raise RuntimeError(
                f"{self.name} plugin schema must contain 'tools' array. "
                f"Found keys: {list(schema_data.keys())}"
            )

        tools = schema_data['tools']
        if not isinstance(tools, list):
            raise RuntimeError(
                f"{self.name} plugin schema 'tools' must be a list"
            )

        self._tools_cache = tools
        return tools

    def clear_schema_cache(self) -> None:
        """Clear the cached schema and tools data.

        This forces the schema to be reloaded on the next access.
        Useful for development and testing.
        """
        super().clear_schema_cache()  # Clear base schema cache
        self._tools_cache = None
        logger.debug(f"Cleared tools cache for {self.name}")

    def _get_method_name(self, tool_name: str) -> str:
        """Convert tool name to method name with optional prefix stripping.

        This method handles multiple tool naming patterns:

        1. **Prefixed tools**: "server_name_method" → "method"
           Example: "cognitive_stack_push" → "push"

        2. **Exact match**: "server_name" → "execute" (default handler)
           Example: "cognitive_stack" → "execute"
           This allows single-tool servers where tool name = server name

        3. **Unprefixed**: "method" → "method"
           Example: "search" → "search"

        Override this method in subclasses for more complex routing logic.

        Args:
            tool_name: The tool name from the tool call

        Returns:
            The method name to call on self
        """
        # Special case: If tool name exactly matches server name (e.g., "cognitive_stack"),
        # route to default "execute" method. This supports single-tool servers where
        # the tool name is just "{{ name }}" in schema.yaml
        if tool_name == self.name:
            return "execute"

        # Strip the "{name}_" prefix if present
        prefix = f"{self.name}_"
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]

        # Return as-is if no prefix found (unprefixed tool names)
        return tool_name

    def _get_available_tool_names(self) -> list[str]:
        """Helper to get list of available tool names for error messages."""
        try:
            tools = self.get_tools()
            tool_names = []
            for t in tools:
                if isinstance(t, dict):
                    # Support both formats: {"function": {...}} and {"name": ...}
                    if 'function' in t:
                        name = t['function'].get('name')
                    else:
                        name = t.get('name')
                    if name:
                        tool_names.append(name)
            return tool_names
        except Exception:
            return []

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Generic tool dispatcher that routes to tool methods by name.

        Automatically calls the method matching the tool name.
        Derived classes just need to implement methods matching their tool names.

        The method name is determined by _get_method_name(), which can be
        overridden for custom routing (e.g., stripping prefixes).

        Example:
            If get_tools() returns a tool named "search_tweets",
            this will call self.search_tweets(params)

        Args:
            tool: The tool name to execute
            params: Parameters to pass to the tool method

        Returns:
            The result from the tool method

        Raises:
            ValueError: If tool is not found or not callable
        """
        # Special case: If this is an Agent and tool name equals agent name,
        # delegate to Agent.call() which handles run/execute/ask actions
        if tool == self.name and hasattr(super(), 'call'):
            # Check if parent class has a different call() implementation (Agent class)
            # This allows agents to be called by their name as a tool
            from ..servers.agent.server import Agent
            if isinstance(self, Agent):
                # Call Agent.call() directly, skipping SchemaBasedToolMixin
                return await Agent.call(self, tool, params)

        # Convert tool name to method name
        method_name = self._get_method_name(tool)

        # Check if the tool method exists
        if not hasattr(self, method_name):
            available = self._get_available_tool_names()
            raise ValueError(
                f"Tool '{tool}' not found in {self.name}. "
                f"Available tools: {available}. "
                f"Expected method: {method_name}()"
            )

        method = getattr(self, method_name)

        # Verify it's callable
        if not callable(method):
            raise ValueError(
                f"Tool '{tool}' exists but is not callable in {self.name}"
            )

        # An agent's role gate (metadata.min_role) covers every tool it serves,
        # not only the runs Agent.call and run_events guard: `<agent>_list_available_tools`
        # answers what GET /agents/{name}/tools refuses. The calling run's user is asked.
        if getattr(self, "min_role", None) is not None:
            from ..servers.agent.server import Agent
            if isinstance(self, Agent):
                denial = self._tool_call_denial(params)
                if denial:
                    return {"status": "error", "error": denial}

        # Call the tool method; an awaitable result is awaited (async methods, and
        # an object with an async __call__, which no coroutine-function check sees)
        result = method(params)
        if inspect.isawaitable(result):
            result = await result
        return result
