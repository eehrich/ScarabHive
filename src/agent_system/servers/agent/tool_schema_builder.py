"""
Tool Schema Builder

Builds tool schemas for LLM consumption from various sources.
Extracted from servers/agent/server.py to reduce _run_events() complexity (Issue #9).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
import logging
import fnmatch

if TYPE_CHECKING:
    from agent_system.servers.agent.components.mcp_integration import MCPIntegrationManager

logger = logging.getLogger(__name__)


def tool_matches_patterns(tool_name: str, server_name: str, patterns: List[str]) -> bool:
    """Match an individual tool against allow/block patterns — THE single
    matcher for tool authorization.

    Schema build (what the LLM sees) and programmatic dispatch
    (``Agent.dispatch_tool_call``, used by tool-scripting) MUST agree on what a
    pattern matches, otherwise a tool hidden from the LLM could still be
    dispatched (or a visible one rejected). Both paths therefore call this one
    function; a parity test asserts the semantics.

    Patterns (identical for allowed and blocked):
    - ``server/tool``: exact match on the full path
    - ``server/*``:    every tool of that server
    - ``server``:      every tool of that server (shorthand, no slash)
    - ``*tool*``:      fnmatch wildcard on full path or bare tool name
    """
    full_tool_path = f"{server_name}/{tool_name}" if server_name else tool_name

    for pattern in patterns:
        # Exact match on full path
        if pattern == full_tool_path:
            return True

        # Server/* pattern - every tool of that server
        if pattern.endswith("/*"):
            if server_name == pattern[:-2]:
                return True

        # Server-level pattern (no slash) - every tool of that server
        if "/" not in pattern and pattern == server_name:
            return True

        # Wildcard matching using fnmatch
        if "*" in pattern:
            if fnmatch.fnmatch(full_tool_path, pattern):
                return True
            if fnmatch.fnmatch(tool_name, pattern):
                return True

    return False


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
        available_tools: List[str],
        allowed_patterns: Optional[List[str]] = None,
        blocked_patterns: Optional[List[str]] = None
    ) -> Tuple[List[Dict], Dict[str, str], List[str], List[str]]:
        """
        Build tool schemas for LLM and maintain name mapping.

        Processes:
        1. External MCP tools (e.g., "server.tool_name")
        2. Internal tools (plugins, agents) from available_tools list
        3. Apply allowed patterns to filter to only wanted tools (if specified)
        4. Apply blocked patterns to filter out unwanted tools

        Args:
            available_tools: List of available tool server names
            allowed_patterns: Optional list of allowed tool patterns (e.g., "plugin/tool_name")
            blocked_patterns: Optional list of blocked tool patterns (e.g., "plugin/tool_name")

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

        # Apply allowed patterns FIRST (if specified) to whitelist tools
        if allowed_patterns:
            tools_schema, tool_name_mapping, usable_tools, display_tools = self._apply_allowed_patterns(
                tools_schema, tool_name_mapping, usable_tools, display_tools, allowed_patterns
            )

        # Apply blocked patterns AFTER tools have been expanded to individual names
        if blocked_patterns:
            tools_schema, tool_name_mapping, usable_tools, display_tools = self._apply_blocked_patterns(
                tools_schema, tool_name_mapping, usable_tools, display_tools, blocked_patterns
            )

        return tools_schema, tool_name_mapping, usable_tools, display_tools

    def _apply_allowed_patterns(
        self,
        tools_schema: List[Dict],
        tool_name_mapping: Dict[str, str],
        usable_tools: List[str],
        display_tools: List[str],
        allowed_patterns: List[str]
    ) -> Tuple[List[Dict], Dict[str, str], List[str], List[str]]:
        """
        Apply allowed patterns to whitelist only wanted tools.

        Patterns support:
        - Exact match: "plugin_name/tool_name"
        - Wildcards: "plugin_name/*" (all tools from plugin)
        - Server-level: "plugin_name" (allows all tools from that server)

        Args:
            tools_schema: List of tool schemas
            tool_name_mapping: Maps tool names to server names
            usable_tools: List of usable tool names
            display_tools: List of display tool names
            allowed_patterns: List of allowed patterns

        Returns:
            Filtered tuple of (tools_schema, tool_name_mapping, usable_tools, display_tools)
        """
        allowed_tool_names: set[str] = set()

        # Find all tools that match allowed patterns
        for tool_name in list(tool_name_mapping.keys()):
            server_name = tool_name_mapping.get(tool_name, "")
            
            if self._is_tool_allowed(tool_name, server_name, allowed_patterns):
                allowed_tool_names.add(tool_name)

        if allowed_tool_names:
            logger.debug(
                f"Agent {self.agent_name}: Allowed tools kept: {sorted(allowed_tool_names)}"
            )

            # Filter tools_schema to only allowed tools
            filtered_schema = [
                schema for schema in tools_schema
                if schema.get("type") == "function" and
                schema.get("function", {}).get("name") in allowed_tool_names
            ]

            # Filter tool_name_mapping to only allowed tools
            filtered_mapping = {
                name: server for name, server in tool_name_mapping.items()
                if name in allowed_tool_names
            }

            # Filter usable_tools to only allowed tools
            filtered_usable = [
                tool for tool in usable_tools
                if tool in allowed_tool_names or tool in [tool_name_mapping.get(t, "") for t in allowed_tool_names]
            ]

            # Filter display_tools to only allowed tools
            filtered_display = [
                tool for tool in display_tools
                if tool in allowed_tool_names
            ]

            return filtered_schema, filtered_mapping, filtered_usable, filtered_display

        # If no tools match allowed patterns, return empty lists (deny all)
        return [], {}, [], []

    def _is_tool_allowed(self, tool_name: str, server_name: str, allowed_patterns: List[str]) -> bool:
        """
        Check if a tool matches any allowed pattern.

        Patterns:
        - "server/tool": exact match on "server/tool" or tool_name == "tool" with server_name == "server"
        - "server/*": all tools from server
        - "server": all tools from server (shorthand)
        - "*tool*": wildcard matching on tool name

        Args:
            tool_name: Individual tool name (e.g., "writer_graph_batch_link")
            server_name: Server the tool belongs to (e.g., "writer_graph")
            allowed_patterns: List of allowed patterns

        Returns:
            True if tool should be allowed
        """
        # Single shared matcher — MUST stay in sync with programmatic dispatch
        # (Agent.dispatch_tool_call); see tool_matches_patterns docstring.
        allowed = tool_matches_patterns(tool_name, server_name, allowed_patterns)
        if allowed:
            logger.debug(f"Tool '{tool_name}' allowed by patterns {allowed_patterns}")
        return allowed

    def _apply_blocked_patterns(
        self,
        tools_schema: List[Dict],
        tool_name_mapping: Dict[str, str],
        usable_tools: List[str],
        display_tools: List[str],
        blocked_patterns: List[str]
    ) -> Tuple[List[Dict], Dict[str, str], List[str], List[str]]:
        """
        Apply blocked patterns to filter out unwanted tools.

        Patterns support:
        - Exact match: "plugin_name/tool_name"
        - Wildcards: "plugin_name/*" (all tools from plugin)
        - Server-level: "plugin_name" (blocks all tools from that server)

        Args:
            tools_schema: List of tool schemas
            tool_name_mapping: Maps tool names to server names
            usable_tools: List of usable tool names
            display_tools: List of display tool names
            blocked_patterns: List of blocked patterns

        Returns:
            Filtered tuple of (tools_schema, tool_name_mapping, usable_tools, display_tools)
        """
        blocked_tool_names: set[str] = set()

        # Find all tools that match blocked patterns
        for tool_name in list(tool_name_mapping.keys()):
            server_name = tool_name_mapping.get(tool_name, "")
            
            if self._is_tool_blocked(tool_name, server_name, blocked_patterns):
                blocked_tool_names.add(tool_name)

        if blocked_tool_names:
            logger.debug(
                f"Agent {self.agent_name}: Blocked tools removed: {sorted(blocked_tool_names)}"
            )

            # Filter tools_schema
            filtered_schema = [
                schema for schema in tools_schema
                if schema.get("type") == "function" and
                schema.get("function", {}).get("name") not in blocked_tool_names
            ]

            # Filter tool_name_mapping
            filtered_mapping = {
                name: server for name, server in tool_name_mapping.items()
                if name not in blocked_tool_names
            }

            # Filter usable_tools
            filtered_usable = [
                tool for tool in usable_tools
                if tool not in blocked_tool_names
            ]

            # Filter display_tools
            filtered_display = [
                tool for tool in display_tools
                if tool not in blocked_tool_names
            ]

            return filtered_schema, filtered_mapping, filtered_usable, filtered_display

        return tools_schema, tool_name_mapping, usable_tools, display_tools

    def _is_tool_blocked(self, tool_name: str, server_name: str, blocked_patterns: List[str]) -> bool:
        """
        Check if a tool matches any blocked pattern.

        Patterns:
        - "server/tool": exact match on "server/tool" or tool_name == "tool" with server_name == "server"
        - "server/*": all tools from server
        - "server": all tools from server (shorthand)
        - "*tool*": wildcard matching on tool name

        Args:
            tool_name: Individual tool name (e.g., "writer_graph_batch_link")
            server_name: Server the tool belongs to (e.g., "writer_graph")
            blocked_patterns: List of blocked patterns

        Returns:
            True if tool should be blocked
        """
        # Single shared matcher — same semantics as allow (see tool_matches_patterns).
        blocked = tool_matches_patterns(tool_name, server_name, blocked_patterns)
        if blocked:
            logger.debug(f"Tool '{tool_name}' blocked by patterns {blocked_patterns}")
        return blocked

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
                # MCPTool uses snake_case (input_schema) internally
                # The MCP client converts from JSON camelCase (inputSchema) to Python snake_case
                input_schema = mcp_tool.input_schema
                tool_schema = {
                    "type": "function",
                    "function": {
                        "name": mcp_tool.name,
                        "description": mcp_tool.description,
                        "parameters": input_schema
                    }
                }
                server_tools.append(tool_schema)

            tools_schema.extend(server_tools)

            # Map individual tool names back to server name
            added_tool_names = []
            for tool_schema in server_tools:
                if tool_schema.get("type") == "function" and "function" in tool_schema:
                    func_schema = tool_schema["function"]
                    individual_tool_name = func_schema.get("name") if isinstance(func_schema, dict) else None
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
