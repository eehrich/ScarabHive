"""Schema-based Agent - Agent that loads tools from schema.yaml files.

This class extends the base Agent with automatic schema.yaml loading,
following the same pattern as SchemaBasedMCPServer.

Most agent plugins should inherit from this class instead of Agent directly,
as it provides the standard tool definition mechanism via schema.yaml.

This class uses SchemaBasedToolMixin for shared functionality with SchemaBasedMCPServer.

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
from ...mcp.schema_mixin import SchemaBasedToolMixin

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

    async def list_tools(self) -> list:
        """Return tools defined in schema.yaml (MCPServer interface).
        
        Override base Agent.list_tools() to return multiple tools from schema.yaml
        instead of just a single agent tool.
        
        Returns:
            List[MCPTool] - Tools defined in this agent's schema.yaml
        """
        from agent_system.mcp.core import MCPTool
        
        # Get tools from schema.yaml
        tools_defs = self.get_tools()
        
        # Convert to MCPTool format
        mcp_tools = []
        for tool_def in tools_defs:
            func = tool_def.get("function", {})
            tool = MCPTool(
                name=func.get("name", "unknown"),
                description=func.get("description", ""),
                input_schema=func.get("parameters", {})
            )
            mcp_tools.append(tool)
        
        return mcp_tools

