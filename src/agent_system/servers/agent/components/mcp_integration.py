"""
MCP Integration Setup for Agent Server
Handles MCP tool discovery, schema building, and external server integration.
"""
import logging
from typing import Dict, List, Optional

from ....mcp.integration import get_mcp_integration

logger = logging.getLogger(__name__)


class MCPIntegrationManager:
    """Manages MCP tool integration and schema building for the agent."""

    def __init__(self, agent_config: dict):
        self.agent_config = agent_config
        self.mcp_integration = None
        self.mcp_initialized_locally = False
        self._agent_ref = None  # Will be set by Agent after creation

    async def setup_mcp_integration(self) -> None:
        """Initialize MCP integration if needed."""
        try:
            self.mcp_integration = get_mcp_integration()
            if not self.mcp_integration.initialized:
                # Initialize with full agent configuration including LLM system
                if hasattr(self.agent_config, 'mcp'):
                    # Build complete config including LLM system for plugin inheritance
                    full_config = {"mcp": self.agent_config.mcp.model_dump() if hasattr(self.agent_config.mcp, "model_dump") else getattr(self.agent_config.mcp, "__dict__", {})}
                    
                    # Add LLM system configuration for plugins that need it
                    if hasattr(self.agent_config, 'llm_system'):
                        full_config['llm_system'] = self.agent_config.llm_system.model_dump() if hasattr(self.agent_config.llm_system, "model_dump") else getattr(self.agent_config.llm_system, "__dict__", {})
                    
                    if hasattr(self.agent_config, 'agent_llm_profiles'):
                        full_config['agent_llm_profiles'] = self.agent_config.agent_llm_profiles
                    
                    # Add servers configuration for MCP plugin initialization
                    if hasattr(self.agent_config, 'servers'):
                        if hasattr(self.agent_config.servers, 'model_dump'):
                            full_config['servers'] = self.agent_config.servers.model_dump()
                        elif isinstance(self.agent_config.servers, dict):
                            full_config['servers'] = self.agent_config.servers
                        else:
                            full_config['servers'] = getattr(self.agent_config.servers, '__dict__', {})
                        
                    await self.mcp_integration.initialize(full_config)
                    self.mcp_initialized_locally = True
                    logger.debug("Initialized MCP integration for agent with full configuration")
                    
                    # Set agent reference for cancellation support if available
                    if hasattr(self, '_agent_ref') and self._agent_ref:
                        self.mcp_integration.main_agent_ref = self._agent_ref
        except Exception as e:
            logger.debug("Failed to initialize MCP integration: %s", e)

    async def get_available_tools(self, plugin_tools: List[str]) -> List[str]:
        """Get all available tools including external MCP tools."""
        available_tools = plugin_tools.copy()

        if self.mcp_integration and self.mcp_integration.initialized:
            try:
                all_tools = await self.mcp_integration.list_all_tools()
                # Add external server tools to available tools
                for server_name, tools in all_tools.get("external_servers", {}).items():
                    for tool in tools:
                        tool_name = f"{server_name}.{tool['name']}"
                        available_tools.append(tool_name)
                        logger.debug("Added external tool: %s", tool_name)
            except Exception as e:
                logger.debug("Failed to get external MCP tools: %s", e)

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
        """Build schema for an external MCP tool."""
        server_name, actual_tool_name = tool_name.split(".", 1)
        # Create OpenAI-compatible name (replace dots with underscores)
        openai_tool_name = tool_name.replace(".", "_")
        tool_name_mapping = {openai_tool_name: tool_name}

        try:
            if self.mcp_integration and self.mcp_integration.initialized:
                all_tools = await self.mcp_integration.list_all_tools()
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
        """Shutdown MCP integration if we initialized it locally."""
        if self.mcp_initialized_locally and self.mcp_integration:
            try:
                await self.mcp_integration.shutdown()
                logger.debug("Shut down MCP integration")
            except Exception as e:
                logger.debug("Error shutting down MCP integration: %s", e)