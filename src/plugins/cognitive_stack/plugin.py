"""
Cognitive Stack Plugin Factory

Hybrid MCP+Hook plugin:
- tools via CognitiveStackServer
- Hooks via CognitiveStackServer.on_pre_llm_call

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .server import CognitiveStackServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig
    from agent_system.hooks import HookContext, HookResult


class CognitiveStackHybridPlugin:
    """
    Hybrid plugin combining tools and hooks.

    - Delegates tools to CognitiveStackServer
    - Delegates hooks to server.on_pre_llm_call
    """

    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        server_config: "ToolServerConfig"
    ):
        self.name = name
        self.system_config = system_config
        self.server_config = server_config

        # Create tool server instance (provides tools + hooks)
        self.server = CognitiveStackServer(name, system_config, server_config)

    # =========================================================================
    # Tool interface (delegate to server)
    # =========================================================================

    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs) -> Any:
        """
        Legacy tool call interface (delegate to server).

        Args:
            tool: Tool name
            params: Tool parameters

        Returns:
            Tool execution result
        """
        if tool is None and params is None:
            # Status call
            return {
                "status": "ok",
                "name": self.name,
                "description": "Cognitive Stack Plugin - Working Memory for LLMs",
                "active": True
            }

        # Delegate to server's call_with_status
        return await self.server.call_with_status(tool, params or {})

    def get_tools(self) -> list[dict[str, Any]]:
        """Expose tools from server"""
        return self.server.get_tools()

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Delegate tool execution to server"""
        return await self.server.call_tool(tool_name, arguments)

    # =========================================================================
    # Hook Interface (delegate to server)
    # =========================================================================

    async def on_pre_llm_call(
        self,
        context: "HookContext"
    ) -> "HookResult":
        """Delegate hook execution to server"""
        return await self.server.on_pre_llm_call(context)

    # =========================================================================
    # Schema Interface (delegate to server)
    # =========================================================================

    def get_schema_data(self) -> dict[str, Any]:
        """Delegate schema loading to tool server (SchemaBasedToolServer)"""
        return self.server.get_schema_data()


def PLUGIN_FACTORY(
    name: str,
    system_config: "AgentSystemConfig",
    server_config: "ToolServerConfig"
) -> CognitiveStackHybridPlugin:
    """
    Factory function for creating CognitiveStackHybridPlugin instances.

    Args:
        name: Plugin name
        system_config: System-level configuration
        server_config: MCP client configuration

    Returns:
        CognitiveStackHybridPlugin instance (MCP+Hook)
    """
    return CognitiveStackHybridPlugin(name, system_config, server_config)
