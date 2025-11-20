"""
Tool Schema Builder

Builds tool schemas for LLM consumption from various sources.
Extracted from servers/agent/server.py to reduce _run_events() complexity (Issue #9).
"""

from __future__ import annotations

from typing import Dict, List, Tuple, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from agent_system.servers.agent.components.mcp_integration import MCPIntegrationManager

logger = logging.getLogger(__name__)


class ToolSchemaBuilder:
    """Builds OpenAI-compatible tool schemas from MCP servers."""

    def __init__(
        self,
        agent_name: str,
        mcp_integration_manager: MCPIntegrationManager,
        server_getter_func
    ):
        """
        Initialize tool schema builder.

        Args:
            agent_name: Name of the agent
            mcp_integration_manager: MCP integration manager
            server_getter_func: Function to get server by name (checks all registries)
        """
        self.agent_name = agent_name
        self.mcp_integration_manager = mcp_integration_manager
        self.get_server = server_getter_func

    async def build_schemas(
        self,
        available_tools: List[str]
    ) -> Tuple[List[Dict], Dict[str, str], List[str], List[str]]:
        """
        Build tool schemas for LLM and maintain name mapping.

        Processes:
        1. External MCP tools (e.g., "server.tool_name")
        2. Internal tools (plugins, agents) from available_tools list

        Args:
            available_tools: List of available tool server names

        Returns:
            Tuple of:
            - tools_schema: List of OpenAI function schemas
            - tool_name_mapping: Maps individual tool names to server names
            - usable_tools: Extended list with individual tool names (for internal use)
            - display_tools: List of ONLY individual tool names (for prompt display)
        """
        tools_schema: List[Dict] = []
        tool_name_mapping: Dict[str, str] = {}

        # Build schemas for external MCP tools
        external_schemas, external_mapping = await self.mcp_integration_manager.build_tool_schemas(
            available_tools
        )
        tools_schema.extend(external_schemas)
        tool_name_mapping.update(external_mapping)

        # Build schemas for internal tools and collect individual tool names
        internal_tools_to_add = await self._build_internal_tool_schemas(
            available_tools,
            tools_schema,
            tool_name_mapping
        )

        # Extend available_tools with individual tool names from multi-tool servers
        usable_tools = available_tools + internal_tools_to_add

        # For prompt display, show ONLY individual tool names (not server names)
        # This avoids listing both "duckduckgo_search" and "duckduckgo_search_web_search"
        display_tools = internal_tools_to_add + [
            tool for tool in available_tools if "." in tool  # External tools (e.g., "context7.resolve-library-id")
        ]

        return tools_schema, tool_name_mapping, usable_tools, display_tools

    async def _build_internal_tool_schemas(
        self,
        available_tools: List[str],
        tools_schema: List[Dict],
        tool_name_mapping: Dict[str, str]
    ) -> List[str]:
        """
        Build schemas for internal tools (plugins + config agents).

        Args:
            available_tools: List of tool server names
            tools_schema: Schema list to append to (modified in place)
            tool_name_mapping: Mapping dict to update (modified in place)

        Returns:
            List of individual tool names to add to available_tools
        """
        internal_tools_to_add: List[str] = []

        for tool_name in available_tools.copy():
            # Skip external tools (contain dots, e.g., "context7.resolve-library-id")
            if "." in tool_name:
                continue

            # Get server from registries
            server = self.get_server(tool_name)
            if not server:
                logger.debug(f"Server '{tool_name}' not found in any registry")
                continue

            # Try modern multi-tool interface first (list_tools)
            if hasattr(server, 'list_tools'):
                added_tools = await self._build_from_list_tools(
                    server,
                    tool_name,
                    tools_schema,
                    tool_name_mapping
                )
                internal_tools_to_add.extend(added_tools)
            # Fallback to get_tools()
            elif hasattr(server, 'get_tools'):
                added_tools = await self._build_from_get_tools(
                    server,
                    tool_name,
                    tools_schema,
                    tool_name_mapping
                )
                internal_tools_to_add.extend(added_tools)
            # Legacy single-tool interface
            elif hasattr(server, 'get_schema'):
                await self._build_from_get_schema(server, tool_name, tools_schema)

        return internal_tools_to_add

    async def _build_from_list_tools(
        self,
        server,
        server_name: str,
        tools_schema: List[Dict],
        tool_name_mapping: Dict[str, str]
    ) -> List[str]:
        """
        Build schemas using server.list_tools() (modern interface with custom descriptions).

        Args:
            server: MCP server instance
            server_name: Name of the server
            tools_schema: Schema list to append to
            tool_name_mapping: Mapping dict to update

        Returns:
            List of individual tool names added
        """
        try:
            mcp_tools = await server.list_tools()

            # Convert MCPTool objects to OpenAI function format
            server_tools = []
            for mcp_tool in mcp_tools:
                tool_schema = {
                    "type": "function",
                    "function": {
                        "name": mcp_tool.name,
                        "description": mcp_tool.description,
                        "parameters": mcp_tool.input_schema
                    }
                }
                server_tools.append(tool_schema)

            tools_schema.extend(server_tools)

            # Map individual tool names back to server name
            added_tool_names = []
            for tool_schema in server_tools:
                if tool_schema.get("type") == "function" and "function" in tool_schema:
                    individual_tool_name = tool_schema["function"].get("name")
                    if individual_tool_name:
                        tool_name_mapping[individual_tool_name] = server_name
                        added_tool_names.append(individual_tool_name)

            logger.debug(
                f"Added {len(server_tools)} tools from server '{server_name}' "
                f"(via list_tools with custom descriptions): "
                f"{[t['function']['name'] for t in server_tools if 'function' in t]}"
            )

            return added_tool_names

        except Exception as e:
            logger.debug(f"Failed to list tools from server '{server_name}': {e}")
            return []

    async def _build_from_get_tools(
        self,
        server,
        server_name: str,
        tools_schema: List[Dict],
        tool_name_mapping: Dict[str, str]
    ) -> List[str]:
        """
        Build schemas using server.get_tools() (fallback, no custom descriptions).

        Args:
            server: MCP server instance
            server_name: Name of the server
            tools_schema: Schema list to append to
            tool_name_mapping: Mapping dict to update

        Returns:
            List of individual tool names added
        """
        try:
            server_tools = server.get_tools()

            # Convert tools to OpenAI format if needed (handle both MCP and OpenAI formats)
            converted_tools = []
            for tool in server_tools:
                converted_tool = self._ensure_openai_format(tool)
                if converted_tool:
                    converted_tools.append(converted_tool)

            tools_schema.extend(converted_tools)

            # Map individual tool names back to server name
            added_tool_names = []
            for tool_schema in converted_tools:
                if tool_schema.get("type") == "function" and "function" in tool_schema:
                    individual_tool_name = tool_schema["function"].get("name")
                    if individual_tool_name:
                        tool_name_mapping[individual_tool_name] = server_name
                        added_tool_names.append(individual_tool_name)

            logger.debug(
                f"Added {len(converted_tools)} tools from server '{server_name}' "
                f"(via get_tools): "
                f"{[t['function']['name'] for t in converted_tools if 'function' in t]}"
            )

            return added_tool_names

        except Exception as e:
            logger.debug(f"Failed to get tools from server '{server_name}': {e}")
            return []

    async def _build_from_get_schema(
        self,
        server,
        server_name: str,
        tools_schema: List[Dict]
    ) -> None:
        """
        Build schema using server.get_schema() (legacy single-tool interface).

        Args:
            server: MCP server instance
            server_name: Name of the server
            tools_schema: Schema list to append to
        """
        try:
            schema = server.get_schema()
            # Ensure schema is in OpenAI format
            converted_schema = self._ensure_openai_format(schema)
            if converted_schema:
                tools_schema.append(converted_schema)
                logger.debug(f"Added legacy tool schema from server '{server_name}'")
        except Exception as e:
            logger.debug(f"Failed to get schema from server '{server_name}': {e}")

    def _ensure_openai_format(self, tool: Dict) -> Dict:
        """
        Ensure tool schema is in OpenAI format, converting from MCP format if needed.

        Handles two formats:
        1. OpenAI format: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
        2. MCP format: {"name": "...", "description": "...", "inputSchema": {...}}

        Args:
            tool: Tool schema in either format

        Returns:
            Tool schema in OpenAI format, or None if invalid
        """
        if not isinstance(tool, dict):
            logger.warning(f"Tool schema is not a dict: {type(tool)}")
            return None

        # Check if already in OpenAI format (has "type": "function" and "function" key)
        if tool.get("type") == "function" and "function" in tool:
            return tool

        # Check if in MCP format (has "name" and "inputSchema")
        if "name" in tool and "inputSchema" in tool:
            # Convert MCP format to OpenAI format
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["inputSchema"]
                }
            }
            logger.debug(f"Converted tool '{tool['name']}' from MCP format to OpenAI format")
            return openai_tool

        # If it has "function" but no "type", add the type field
        if "function" in tool and "type" not in tool:
            tool["type"] = "function"
            logger.debug(f"Added missing 'type' field to tool schema: {tool.get('function', {}).get('name', 'unknown')}")
            return tool

        # Invalid format
        logger.error(f"Tool schema has invalid format (missing required fields): {tool.keys()}")
        return None
