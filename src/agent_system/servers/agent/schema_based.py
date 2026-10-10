"""Schema-based Agent - Agent that loads tools from schema.yaml files.

This class extends the base Agent with automatic schema.yaml loading,
following the same pattern as SchemaBasedToolServer.

Most agent plugins should inherit from this class instead of Agent directly,
as it provides the standard tool definition mechanism via schema.yaml.

This class uses SchemaBasedToolMixin for shared functionality with SchemaBasedToolServer.

Features:
- Automatic schema.yaml loading with template variable support
- Generic tool dispatcher (routes calls to methods automatically)
- Automatic tool name prefix stripping ("{name}_tool" → "tool")
- Robust plugin directory resolution
- Full schema caching and access
- Development utilities (cache clearing)
"""
from __future__ import annotations

import logging

from .server import Agent
from ...tools.schema_mixin import SchemaBasedToolMixin

logger = logging.getLogger(__name__)


class SchemaBasedAgent(SchemaBasedToolMixin, Agent):
    """Agent that automatically loads tools from schema.yaml.

    This class eliminates the need for agent plugins to implement
    get_tools() and call() methods. Instead:
    1. Tools are defined in schema.yaml
    2. Methods matching tool names are automatically routed

    For agent tools, the agent name prefix is automatically stripped:
    - Tool: "basic_agent_execute_task"
    - Method: execute_task(params)

    Example schema.yaml:
        tools:
          - function:
              name: "{{name}}_execute_task"
              description: "Execute a task"
              parameters:
                type: object
                properties:
                  task:
                    type: string
                    description: "The task to execute"
                required: ["task"]

    Example implementation:
        class MyAgent(SchemaBasedAgent):
            async def execute_task(self, params: dict) -> dict:
                task = params["task"]
                # Execute task...
                return {"status": "success", "result": "..."}

    The {{name}} template variable is automatically replaced with the agent's name.

    Note: This class uses SchemaBasedToolMixin's call() dispatcher for tool routing,
    which differs from Agent's simplified call() interface that only handles
    "run", "execute", and "ask" actions.
    """

    def __init__(self, *args, **kwargs):
        """Initialize SchemaBasedAgent.

        Accepts the same arguments as Agent. Initializes schema caching.
        """
        super().__init__(*args, **kwargs)
        # Initialize schema mixin
        self._init_schema_mixin()

    def get_template_vars(self) -> dict:
        """Override to provide llm_profiles for schema rendering."""
        vars = super().get_template_vars()

        # Add available LLM profiles if agent_config exists.
        # llm_profiles = union of both chains (selection enum in the schema);
        # has_advanced gates the use_advanced_model description — without
        # llm_profile_advanced (or when advanced == default, i.e. no
        # real upgrade possible) the parameter is a no-op and should
        # not be advertised as an upgrade.
        if hasattr(self, 'agent_config') and self.agent_config:
            ac = self.agent_config
            vars['llm_profiles'] = ac.available_llm_profiles
            vars['has_advanced'] = bool(
                ac.advanced_llm_profile
                and ac.advanced_llm_profile != ac.default_llm_profile)
        else:
            vars['llm_profiles'] = []
            vars['has_advanced'] = False

        return vars

    async def list_tools(self) -> list:
        """Return tools defined in schema.yaml (ToolServer interface).

        Override base Agent.list_tools() to return multiple tools from schema.yaml
        instead of just a single agent tool.

        Returns:
            List[ToolDef] - Tools defined in this agent's schema.yaml
        """
        # Return cached tools to avoid creating new objects on every call
        if self._list_tools_cache is not None:
            return self._list_tools_cache

        from agent_system.tools.base import ToolDef

        # Get tools from schema.yaml
        tools_defs = self.get_tools()
        # The instance's own texts (sysadmin_agent, coder, ...), as ToolServer.list_tools does.
        self._apply_custom_tool_descriptions(tools_defs)

        # Convert to ToolDef format
        tool_defs = []
        for tool_def in tools_defs:
            func = tool_def.get("function", {})
            tool = ToolDef(
                name=func.get("name", "unknown"),
                description=func.get("description", ""),
                input_schema=func.get("parameters", {})
            )
            tool_defs.append(tool)

        self._list_tools_cache = tool_defs
        return self._list_tools_cache

