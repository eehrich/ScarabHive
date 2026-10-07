"""
Tool Discovery Service

Handles tool discovery, filtering, and visibility checks.
Extracted from servers/agent/server.py to reduce complexity (Issue #11).
"""

from __future__ import annotations

from typing import List, Optional, Tuple, TYPE_CHECKING
import logging

from .tool_schema_builder import server_matches_patterns
# Module level on purpose: inside _is_tool_visible it would sit in a
# try/except that answers "invisible" -- a future import cycle would
# then empty every agent's tool list in silence instead of failing loud.
from ...runtime import ServerView

if TYPE_CHECKING:
    from agent_system.config.models import AgentConfig
    from agent_system.tools.base import ToolServerRegistry
    from agent_system.servers.agent.components.tool_integration import ToolIntegrationManager

logger = logging.getLogger(__name__)


class ToolDiscoveryService:
    """Discovers and filters tools based on agent configuration."""
    
    def __init__(
        self,
        agent_name: str,
        agent_config: AgentConfig,
        tool_integration_manager: ToolIntegrationManager,
        registry: Optional[ToolServerRegistry] = None
    ):
        """
        Initialize tool discovery service.
        
        Args:
            agent_name: Name of the agent
            agent_config: Agent configuration with tool settings
            tool_integration_manager: tool integration manager
            registry: Optional local tool registry
        """
        self.agent_name = agent_name
        self.agent_config = agent_config
        self.tool_integration_manager = tool_integration_manager
        self.registry = registry
    
    async def discover_allowed_tools(self) -> Tuple[List[str], Optional[List[str]], Optional[List[str]]]:
        """
        Discover all available tools and apply allow-list filtering.
        
        Combines tools from:
        1. Plugin-provided tool servers
        2. External MCP servers
        3. Local registry (filtered by _tool_visible flag)
        
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
        available_tools = await self.tool_integration_manager.get_available_tools(plugin_tools)
        
        # NOTE: Do NOT expand tools here - build_schemas() will do that when building tool schemas
        # Expansion here causes duplicate processing and breaks schema building
        
        # Add registry tools (filtered by visibility)
        registry_tools = self._get_registry_tools()
        for tool_name in registry_tools:
            if tool_name not in available_tools:
                available_tools.append(tool_name)
        
        return available_tools
    
    def _get_plugin_tools(self) -> List[str]:
        """Get list of plugin-provided tool servers."""
        if not (self.tool_integration_manager.tool_integration and
                self.tool_integration_manager.tool_integration.initialized):
            return []
        
        return self.tool_integration_manager.tool_integration.plugin_registry.list_servers()
    
    def _get_registry_tools(self) -> List[str]:
        """
        Get tools from local registry, filtered by _tool_visible flag.
        
        Only includes agents that have _tool_visible=True or don't have
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
        Check if a tool is visible (exposed as tool).
        
        Args:
            tool_name: Name of the tool to check
            
        Returns:
            True if tool is visible, False otherwise
        """
        # Inside the try, like everything else here: an invisible tool is the
        # fail-safe answer this method has always given when it could not
        # look, and the view must not be the one path that throws out of it.
        try:
            # Asked through describe(), not get(): this runs for EVERY server
            # on EVERY request, and building a server to read one boolean off
            # it is the one thing a lazy start must not do. The view answers
            # from the instance whenever there is one, so the result is
            # identical.
            #
            # isinstance, not "is not None": the suite is full of registries
            # that are Mock() objects, and a Mock answers describe() with a
            # truthy Mock whose every attribute is truthy too. That would
            # silently turn this filter into "everything is visible" and stop
            # consulting the get() those tests steer.
            view = self.registry.describe(tool_name)
            if isinstance(view, ServerView):
                return bool(view.tool_visible)

            # Unbound registry, or a declaration that cannot answer for its
            # instance: the instance is the only source. Verbatim the old path.
            server = self.registry.get(tool_name)
            if server and hasattr(server, '_tool_visible'):
                visible = getattr(server, '_tool_visible', True)
                if not visible:
                    return False
            # No _tool_visible attribute → include as tool (backward compat)
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
    
    async def _apply_wildcard_fallback(self, patterns: List[str]) -> List[str]:
        """
        Fallback for wildcard patterns that produced empty results.
        
        If '*' was specified but nothing matched, try fetching plugin
        server names directly from the plugin registry.
        
        Args:
            patterns: Allowed patterns
            
        Returns:
            Plugin server names if wildcard fallback applies, empty list otherwise
        """
        # Only apply if global wildcard was specified
        if not any(p == '*' for p in patterns):
            return []
        
        if not (self.tool_integration_manager.tool_integration and
                self.tool_integration_manager.tool_integration.initialized):
            return []
        
        try:
            plugin_registry = self.tool_integration_manager.tool_integration.plugin_registry
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
        Check if a discovery-stage name matches any pattern.

        Thin delegate to the SINGLE shared discovery-stage matcher
        ``tool_schema_builder.server_matches_patterns`` (see its docstring for
        pattern semantics). Kept as a method for existing callers/tests.
        """
        return server_matches_patterns(tool, patterns)
