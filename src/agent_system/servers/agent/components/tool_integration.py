"""
Tool integration Setup for Agent Server
Handles tool discovery, schema building, and external server integration.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ....config.models import AgentSystemConfig, AgentConfig

from ....tools.integration import get_tool_integration

logger = logging.getLogger(__name__)


class ToolIntegrationManager:
    """Manages tool integration and schema building for the agent."""

    def __init__(self, system_config: AgentSystemConfig, agent_config: AgentConfig):
        """Initialize Tool integration Manager.
        
        Args:
            system_config: AgentSystemConfig object with full system configuration
            agent_config: AgentConfig object with agent-specific configuration
        """
        self.system_config = system_config
        self.agent_config = agent_config
        self.tool_integration = None
        self.tools_initialized_locally = False
        self._agent_ref = None  # Will be set by Agent after creation

    async def setup_tool_integration(self) -> None:
        """Initialize tool integration if needed."""
        try:
            # First try to get an existing global integration (from API or CLI bootstrap)
            try:
                # Check if there's already a global initialized integration (no config needed for check)
                existing_integration = get_tool_integration()
                if existing_integration and existing_integration.initialized:  # type: ignore[unreachable]
                    self.tool_integration = existing_integration
                    logger.debug("Using existing initialized tool integration for agent")
                    # Set agent reference for cancellation support if available
                    if hasattr(self, '_agent_ref') and self._agent_ref:  # type: ignore[unreachable]
                        self.tool_integration.main_agent_ref = self._agent_ref  # type: ignore[unreachable]
                    return
            except (ValueError, Exception):
                # No existing integration available, create new one
                logger.debug("No existing tool integration found, creating new one")

            # Create or get a new integration with system_config (not agent_config!)
            self.tool_integration = get_tool_integration(config=self.system_config)
            if not self.tool_integration.initialized:
                # Initialize with the full system configuration
                await self.tool_integration.initialize(self.system_config)
                self.tools_initialized_locally = True
                logger.debug("Initialized tool integration for agent with full system configuration")

            # Set agent reference for cancellation support if available
            if hasattr(self, '_agent_ref') and self._agent_ref:
                self.tool_integration.main_agent_ref = self._agent_ref  # type: ignore[unreachable]
        except Exception as e:
            logger.debug("Failed to initialize tool integration: %s", e)

    async def get_available_tools(self, plugin_tools: List[str]) -> List[str]:
        """Get all available tools including external tools."""
        available_tools = plugin_tools.copy()

        # Also treat plugin tool server names themselves as callable namespaces so high-level
        # patterns like '*' or 'plugin' or 'plugin/*' can match even if no external tools expanded yet.
        try:
            if self.tool_integration and self.tool_integration.initialized:  # type: ignore[unreachable]
                for srv in self.tool_integration.plugin_registry.list_servers():  # type: ignore[unreachable]
                    if srv not in available_tools:
                        available_tools.append(srv)
        except Exception:
            pass

        if self.tool_integration and self.tool_integration.initialized:  # type: ignore[unreachable]
            try:  # type: ignore[unreachable]
                all_tools = await self.tool_integration.list_all_tools()
                # Add external server tools to available tools
                for server_name, tools in all_tools.get("external_servers", {}).items():
                    for tool in tools:
                        tool_name = f"{server_name}.{tool['name']}"
                        available_tools.append(tool_name)
                        logger.debug("Added external tool: %s", tool_name)
            except Exception as e:
                logger.debug("Failed to get external tools: %s", e)

        return available_tools

    async def build_tool_schemas(self, available_tools: List[str]) -> tuple[List[Dict], Dict[str, str]]:
        """Build OpenAI-compatible tool schemas and name mappings."""
        tools_schema: List[Dict] = []
        tool_name_mapping = {}  # Maps OpenAI-compatible names to original names

        for tool_name in available_tools:
            # Check if it's an external tool (contains a dot)
            if "." in tool_name:
                schema, mapping = await self._build_external_tool_schema(tool_name)
                if schema:
                    tools_schema.append(schema)
                    tool_name_mapping.update(mapping)
            else:
                # Regular plugin tool - this would need to be passed from the main server
                # For now, we'll handle this in the main server
                pass

        return tools_schema, tool_name_mapping

    async def _build_external_tool_schema(self, tool_name: str) -> tuple[Optional[Dict], Dict[str, str]]:
        """Build schema for an external tool."""
        server_name, actual_tool_name = tool_name.split(".", 1)
        
        # Create OpenAI-compatible name (replace dots and other invalid chars with underscores)
        # OpenAI requires pattern: ^[a-zA-Z0-9_-]+$
        openai_tool_name = tool_name.replace(".", "_")  # Replace dots first
        openai_tool_name = openai_tool_name.replace("/", "__")  # Replace slashes with double underscore
        openai_tool_name = openai_tool_name.replace(" ", "_")  # Replace spaces
        # Remove any remaining invalid characters
        import re
        openai_tool_name = re.sub(r'[^a-zA-Z0-9_-]', '', openai_tool_name)
        
        tool_name_mapping = {openai_tool_name: tool_name}

        try:
            if self.tool_integration and self.tool_integration.initialized:  # type: ignore[unreachable]
                all_tools = await self.tool_integration.list_all_tools()  # type: ignore[unreachable]
                external_tools = all_tools.get("external_servers", {}).get(server_name, [])
                for tool in external_tools:
                    if tool["name"] == actual_tool_name:
                        schema = {
                            "type": "function",
                            "function": {
                                "name": openai_tool_name,
                                "description": f"[{server_name}] {tool['description']}",
                                "parameters": tool.get("input_schema", {})
                            }
                        }
                        return schema, tool_name_mapping
        except Exception as e:
            logger.debug("Failed to build schema for external tool %s: %s", tool_name, e)

        return None, {}

    async def shutdown(self) -> None:
        """Shutdown tool integration if we initialized it locally."""
        if self.tools_initialized_locally and self.tool_integration:  # type: ignore[unreachable]
            try:  # type: ignore[unreachable]
                await self.tool_integration.shutdown()
                logger.debug("Shut down tool integration")
            except Exception as e:
                logger.debug("Error shutting down tool integration: %s", e)