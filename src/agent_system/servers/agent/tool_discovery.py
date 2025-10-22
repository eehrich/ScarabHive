"""
Tool Discovery Service

Handles tool discovery, filtering, and visibility checks.
Extracted from servers/agent/server.py to reduce complexity (Issue #11).
"""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING
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
    
    async def discover_allowed_tools(self) -> List[str]:
        """
        Discover all allowed tools for this agent.
        
        Combines tools from:
        1. Plugin-provided tool servers
        2. External MCP servers
        3. Local registry (filtered by _mcp_tool_visible flag)
        
        Then applies allow-list and block-list filtering.
        
        Returns:
            List of allowed tool names
        """
        # Get allow/block patterns
        allowed_patterns = self._get_allowed_patterns()
        if not allowed_patterns:
            logger.debug(
                "Agent %s: no tools.allowed configured -> deny-all (0 tools)",
                self.agent_name
            )
            return []
        
        # Discover all available tools
        available_tools = await self._discover_all_tools()
        
        # Apply allow-list filter
        available_tools = self._apply_allow_list(available_tools, allowed_patterns)
        
        # Apply wildcard fallback if needed
        if not available_tools:
            available_tools = await self._apply_wildcard_fallback(allowed_patterns)
        
        # Apply block-list filter
        blocked_patterns = self._get_blocked_patterns()
        if blocked_patterns:
            available_tools = self._apply_block_list(available_tools, blocked_patterns)
        
        return available_tools
    
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
            List of all discovered tool names
        """
        available_tools: List[str] = []
        
        # Get plugin tools
        plugin_tools = self._get_plugin_tools()
        
        # Get external + plugin + adapter tools
        available_tools = await self.mcp_integration_manager.get_available_tools(plugin_tools)
        
        # Add registry tools (filtered by visibility)
        registry_tools = self._get_registry_tools()
        for tool_name in registry_tools:
            if tool_name not in available_tools:
                available_tools.append(tool_name)
        
        return available_tools
    
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
            if hasattr(server, '_mcp_tool_visible'):
                if not server._mcp_tool_visible:
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
