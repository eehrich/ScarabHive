"""Schema-based MCP Server Base Class

Provides a base class for MCP servers that load their tool definitions
from schema.yaml files, eliminating code duplication across plugins.

This class uses SchemaBasedToolMixin for shared functionality with SchemaBasedAgent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import MCPServer
from .schema_mixin import SchemaBasedToolMixin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPServerConfig

logger = logging.getLogger(__name__)


class SchemaBasedMCPServer(SchemaBasedToolMixin, MCPServer):
    """Base class for MCP servers that load tools from schema.yaml files.
    
    This class eliminates the need for every plugin to implement identical
    get_tools() methods that load and parse schema.yaml files.
    
    Plugins can simply inherit from this class and their schema.yaml file
    will be automatically loaded and parsed.
    
    The generic call() dispatcher (from SchemaBasedToolMixin) automatically routes
    tool calls to methods matching the tool names. If tool names include a
    "{name}_" prefix, it will be automatically stripped:
    - Tool: "my_server_search" → Method: search(params)
    - Tool: "search" → Method: search(params)
    
    Example:
        class MyServer(SchemaBasedMCPServer):
            async def my_tool(self, params: dict) -> Any:
                return {"result": params["input"]}
    
    Note: This class uses SchemaBasedToolMixin's call() dispatcher for automatic
    tool routing, which overrides MCPServer's basic call() implementation.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPServerConfig):
        super().__init__(name, system_config, mcp_config)
        # Initialize schema mixin
        self._init_schema_mixin()
