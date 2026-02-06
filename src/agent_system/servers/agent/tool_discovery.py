"""
Tool Discovery Service

Handles tool discovery, filtering, and visibility checks.
Extracted from servers/agent/server.py to reduce complexity (Issue #11).
"""

from __future__ import annotations

from typing import List, Optional, Tuple, TYPE_CHECKING
import logging
import fnmatch

if TYPE_CHECKING:
    from agent_system.config.models import AgentConfig
    from agent_system.mcp.base import MCPRegistry
    from agent_system.servers.agent.components.mcp_integration import MCPIntegrationManager

logger = logging.getLogger(__name__)


class ToolDiscoveryService:
    """Discovers and filters tools based on agent configuration."""
    
    def __init__(
        self,
        agent_name: str,
        agent_config: AgentConfig,
        mcp_integration_manager: MCPIntegrationManager,
        registry: Optional[MCPRegistry] = None
    ):
        """
        Initialize tool discovery service.
        
        Args:
            agent_name: Name of the agent
            agent_config: Agent configuration with tool settings
            mcp_integration_manager: MCP integration manager
            registry: Optional local MCP registry
        """
        self.agent_name = agent_name
        self.agent_config = agent_config
        self.mcp_integration_manager = mcp_integration_manager
        self.registry = registry
    
    async def discover_allowed_tools(self) -> Tuple[List[str], Optional[List[str]], Optional[List[str]]]:
        """
        Discover all available tools and apply allow-list filtering.
        
        Combines tools from:
        1. Plugin-provided tool servers
        2. External MCP servers
        3. Local registry (filtered by _mcp_tool_visible flag)
        
        Then applies allow-list filtering at server level. Both allowed and blocked
        patterns are returned for fine-grained filtering after tool expansion.
        
        Returns:
            Tuple of:
            - List of allowed tool names (server names at this stage)
            - List of allowed patterns (for fine-grained filtering after expansion)
            - List of blocked patterns (to be applied after tool expansion)
        """
        # Get allow/block patterns
        allowed_patterns = self._get_allowed_patterns()
        # None or [] means deny-all (security by default)
        if allowed_patterns is None or (isinstance(allowed_patterns, list) and len(allowed_patterns) == 0):
            logger.debug(
                "Agent %s: tools.allowed not configured or empty -> deny-all (0 tools)",
                self.agent_name
            )
            return [], None, None
        
        # Discover all available tools
        available_tools = await self._discover_all_tools()
        
        # Apply allow-list filter (server-level)
        available_tools = self._apply_allow_list(available_tools, allowed_patterns)
        
        # Apply wildcard fallback if needed
        if not available_tools:
            available_tools = await self._apply_wildcard_fallback(allowed_patterns)
        
        # Get blocked patterns but don't apply yet - tools need to be expanded first
        # Both allowed_patterns and blocked_patterns are applied in ToolSchemaBuilder
        # after individual tools are discovered
        blocked_patterns = self._get_blocked_patterns()
        
        return available_tools, allowed_patterns, blocked_patterns
    
    def _get_allowed_patterns(self) -> Optional[List[str]]:
        """Get allowed tool patterns from agent config."""
        try:
            return self.agent_config.tools.allowed if self.agent_config.tools else None
        except Exception as e:
            logger.warning(
                f"Failed to get allowed_patterns from agent config: {e}",
                exc_info=True
            )
            return None
    
    def _get_blocked_patterns(self) -> Optional[List[str]]:
        """Get blocked tool patterns from agent config."""
        try:
            return self.agent_config.tools.blocked if self.agent_config.tools else None
        except Exception as e:
            logger.warning(
                f"Failed to get blocked_patterns from agent config: {e}",
                exc_info=True
            )
            return None
    
    async def _discover_all_tools(self) -> List[str]:
        """
        Discover all available tools from all sources.
        
        Returns:
            List of all discovered tool names in format:
            - Server names: "web_scraper"
            - Individual tools: "web_scraper/scrape_webpage"
        """
        available_tools: List[str] = []
        
        # Get plugin tools
        plugin_tools = self._get_plugin_tools()
        
        # Get external + plugin + adapter tools
        available_tools = await self.mcp_integration_manager.get_available_tools(plugin_tools)
        
        # NOTE: Do NOT expand tools here - build_schemas() will do that when building tool schemas
        # Expansion here causes duplicate processing and breaks schema building
        
        # Add registry tools (filtered by visibility)
        registry_tools = self._get_registry_tools()
        for tool_name in registry_tools:
            if tool_name not in available_tools:
                available_tools.append(tool_name)
        
        return available_tools
    
    async def _expand_plugin_tools(self, server_names: List[str]) -> List[str]:
        """
        Expand plugin server names into individual tool names.
        
        Converts: ["web_scraper", "duckduckgo_search"]
        Into: ["web_scraper/scrape_webpage", "duckduckgo_search/search", ...]
        
        Args:
            server_names: List of plugin server names
            
        Returns:
            List of expanded tool names in servername/toolname format
        """
        expanded = []
        
        if not (self.mcp_integration_manager.mcp_integration and
                self.mcp_integration_manager.mcp_integration.initialized):
            logger.warning(f"Agent {self.agent_name}: Cannot expand tools - MCP not initialized")
            return expanded
        
        try:
            all_tools_dict = await self.mcp_integration_manager.mcp_integration.list_all_tools()
            logger.debug(f"Agent {self.agent_name}: Available plugin servers for expansion: {list(all_tools_dict.get('plugins', {}).keys())}")
            
            for server_name, tools in all_tools_dict.get("plugins", {}).items():
                for tool in tools:
                    tool_name = f"{server_name}/{tool['name']}"  # tool is a dict, not object
                    expanded.append(tool_name)
                    logger.debug(f"Agent {self.agent_name}: Expanded {server_name} -> {tool_name}")
                    
            logger.info(f"Agent {self.agent_name}: Expanded {len(expanded)} tools from {len(all_tools_dict.get('plugins', {}))} plugin servers")
        except Exception as e:
            logger.error(f"Agent {self.agent_name}: Failed to expand plugin tools: {e}", exc_info=True)
        
        return expanded
    
    def _get_plugin_tools(self) -> List[str]:
        """Get list of plugin-provided tool servers."""
        if not (self.mcp_integration_manager.mcp_integration and
                self.mcp_integration_manager.mcp_integration.initialized):
            return []
        
        return self.mcp_integration_manager.mcp_integration.plugin_registry.list_servers()
    
    def _get_registry_tools(self) -> List[str]:
        """
        Get tools from local registry, filtered by _mcp_tool_visible flag.
        
        Only includes agents that have _mcp_tool_visible=True or don't have
        the attribute (backward compatibility).
        
        Returns:
            List of visible tool names from registry
        """
        if not self.registry:
            return []
        
        registry_tools = self.registry.list()
        visible_tools: List[str] = []
        
        for tool_name in registry_tools:
            if self._is_tool_visible(tool_name):
                visible_tools.append(tool_name)
        
        return visible_tools
    
    def _is_tool_visible(self, tool_name: str) -> bool:
        """
        Check if a tool is visible (exposed as MCP tool).
        
        Args:
            tool_name: Name of the tool to check
            
        Returns:
            True if tool is visible, False otherwise
        """
        try:
            server = self.registry.get(tool_name)
            if server and hasattr(server, '_mcp_tool_visible'):
                visible = getattr(server, '_mcp_tool_visible', True)
                if not visible:
                    logger.debug(
                        f"Skipping agent '{tool_name}' in tool discovery "
                        f"(not exposed as tool: _mcp_tool_visible=False)"
                    )
                    return False
            # No _mcp_tool_visible attribute → include as tool (backward compat)
            return True
        except Exception as e:
            logger.debug(f"Failed to check tool visibility for '{tool_name}': {e}")
            return False
    
    def _apply_allow_list(self, tools: List[str], patterns: List[str]) -> List[str]:
        """
        Filter tools by allow-list patterns.
        
        Supports:
        - Exact matches: "tool_name"
        - Wildcards: "tool_*", "*_server"
        - Global wildcard: "*"
        
        Args:
            tools: List of tool names
            patterns: List of allowed patterns
            
        Returns:
            Filtered list of allowed tools
        """
        filtered_tools = self._filter_tools_by_patterns(tools, patterns)
        logger.debug(
            "Filtered available tools for agent %s (allow list) -> %s",
            self.agent_name,
            filtered_tools
        )
        
        if not filtered_tools:
            logger.warning(
                "Agent %s allow list patterns produced an empty tool set",
                self.agent_name
            )
        
        return filtered_tools
    
    def _apply_block_list(self, tools: List[str], patterns: List[str]) -> List[str]:
        """
        Remove blocked tools from list.
        
        Args:
            tools: List of tool names
            patterns: List of blocked patterns
            
        Returns:
            Filtered list with blocked tools removed
        """
        before_block = list(tools)
        filtered_tools = [
            t for t in tools 
            if not self._matches_any_pattern(t, patterns)
        ]
        
        removed = set(before_block) - set(filtered_tools)
        if removed:
            logger.debug(
                "Agent %s blocked_tools removed: %s",
                self.agent_name,
                sorted(removed)
            )
        
        if not filtered_tools:
            logger.warning("Agent %s blocked_tools removed all tools", self.agent_name)
        
        return filtered_tools
    
    async def _apply_wildcard_fallback(self, patterns: List[str]) -> List[str]:
        """
        Fallback for wildcard patterns that produced empty results.
        
        If '*' was specified but nothing matched, try fetching plugin
        server names directly from the MCP plugin registry.
        
        Args:
            patterns: Allowed patterns
            
        Returns:
            Plugin server names if wildcard fallback applies, empty list otherwise
        """
        # Only apply if global wildcard was specified
        if not any(p == '*' for p in patterns):
            return []
        
        if not (self.mcp_integration_manager.mcp_integration and
                self.mcp_integration_manager.mcp_integration.initialized):
            return []
        
        try:
            plugin_registry = self.mcp_integration_manager.mcp_integration.plugin_registry
            plugin_names = list(plugin_registry.list_servers())
            
            if plugin_names:
                logger.debug(
                    "Wildcard fallback adding plugin servers for agent %s: %s",
                    self.agent_name,
                    plugin_names
                )
                return plugin_names
        except Exception as e:
            logger.debug(f"Wildcard plugin fallback failed: {e}")
        
        return []
    
    def _filter_tools_by_patterns(self, tools: List[str], patterns: List[str]) -> List[str]:
        """
        Filter tools that match any of the patterns.
        
        Args:
            tools: List of tool names
            patterns: List of patterns to match
            
        Returns:
            Tools that match at least one pattern
        """
        if not patterns:
            return []
        
        # If wildcard present, return all tools
        if '*' in patterns:
            return tools
        
        filtered: List[str] = []
        for tool in tools:
            if self._matches_any_pattern(tool, patterns):
                filtered.append(tool)
        
        return filtered
    
    def _matches_any_pattern(self, tool: str, patterns: List[str]) -> bool:
        """
        Check if tool matches any pattern.
        
        Supports:
        - Exact matches
        - Wildcards with '*'
        - Dot notation patterns
        - Server name matching for wildcard patterns (e.g., "ssh_control" matches "ssh_control/*")
        - Server name matching for specific tool patterns (e.g., "writer_content" matches "writer_content/writer_content_book")
        
        Args:
            tool: Tool name to check
            pattern: Pattern to match against
            
        Returns:
            True if tool matches any pattern
        """
        for pattern in patterns:
            # Exact match
            if tool == pattern:
                return True
            
            # Wildcard match
            if '*' in pattern:
                if fnmatch.fnmatch(tool, pattern):
                    return True
                
                # Special case: If pattern is "server_name/*", also match "server_name"
                # This allows server names to pass through for later expansion
                if pattern.endswith('/*'):
                    server_name = pattern[:-2]  # Remove "/*"
                    if tool == server_name:
                        return True
            
            # Special case: If pattern is "server_name/tool_name", match "server_name"
            # This allows server names to pass through for later tool-level filtering
            if '/' in pattern and '*' not in pattern:
                server_name = pattern.split('/')[0]
                if tool == server_name:
                    return True
            
            # Dot notation match (e.g., "plugin.tool" matches "plugin.*")
            if '.' in pattern and '.' in tool:
                pattern_parts = pattern.split('.')
                tool_parts = tool.split('.')
                if self._matches_dot_pattern(tool_parts, pattern_parts):
                    return True
        
        return False
    
    def _matches_dot_pattern(self, tool_parts: List[str], pattern_parts: List[str]) -> bool:
        """
        Match dot-separated patterns.
        
        Examples:
        - "plugin.*" matches "plugin.tool1", "plugin.tool2"
        - "plugin.sub.*" matches "plugin.sub.tool1"
        
        Args:
            tool_parts: Tool name split by '.'
            pattern_parts: Pattern split by '.'
            
        Returns:
            True if pattern matches
        """
        if len(pattern_parts) > len(tool_parts):
            return False
        
        for i, pattern_part in enumerate(pattern_parts):
            if pattern_part == '*':
                return True  # Wildcard matches rest
            if i >= len(tool_parts) or tool_parts[i] != pattern_part:
                return False
        
        return True
