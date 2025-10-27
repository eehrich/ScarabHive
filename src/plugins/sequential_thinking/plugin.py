"""
Sequential Thinking Plugin Factory

Hybrid MCP+Hook plugin:
- MCP tools via SequentialThinkingServer
- Hooks via SequentialThinkingServer.on_pre_llm_call

Exports PLUGIN_FACTORY for AgentSystem plugin discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .server import SequentialThinkingServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig
    from agent_system.hooks import HookContext, HookResult


class SequentialThinkingHybridPlugin:
    """
    Hybrid plugin combining MCP tools and hooks.
    
    - Delegates MCP tools to SequentialThinkingServer
    - Delegates hooks to server.on_pre_llm_call
    """
    
    def __init__(
        self,
        name: str,
        system_config: "AgentSystemConfig",
        mcp_config: "MCPConfig"
    ):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Create MCP server instance (provides tools + hooks)
        self.server = SequentialThinkingServer(name, system_config, mcp_config)
    
    # =========================================================================
    # MCP Interface (delegate to server)
    # =========================================================================
    
    async def call(self, tool: str | None = None, params: dict | None = None, *args, **kwargs) -> Any:
        """
        Legacy MCP call interface (delegate to server).
        
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
                "description": "Sequential Thinking Plugin",
                "active": True
            }
        
        # Delegate to server's call_with_status
        return await self.server.call_with_status(tool, params or {})
    
    def get_tools(self) -> list[dict[str, Any]]:
        """Expose MCP tools from server"""
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
        """Delegate schema loading to MCP server (SchemaBasedMCPServer)"""
        return self.server.get_schema_data()


def PLUGIN_FACTORY(
    name: str,
    system_config: "AgentSystemConfig",
    mcp_config: "MCPConfig"
) -> SequentialThinkingHybridPlugin:
    """
    Factory function for creating SequentialThinkingHybridPlugin instances.
    
    Args:
        name: Plugin name
        system_config: System-level configuration
        mcp_config: MCP client configuration
        
    Returns:
        SequentialThinkingHybridPlugin instance (MCP+Hook)
    """
    return SequentialThinkingHybridPlugin(name, system_config, mcp_config)
