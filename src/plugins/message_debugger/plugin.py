"""Message Debugger Plugin - Factory and exports.

Hybrid plugin: Schema-based hooks + Web UI for viewing captured message snapshots.
Hook implementations are in hooks.py, web endpoints in web_endpoints.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any, TYPE_CHECKING

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
from .hooks import MessageDebuggerPlugin
from .web_endpoints import MessageDebuggerWebFactory

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class MessageDebuggerHybridPlugin(SchemaBasedPluginWebInterface):
    """Hybrid plugin that provides both hooks and web capabilities."""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with standard hybrid plugin signature."""
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, mcp_config)
        
        plugin_dir = Path(__file__).parent
        
        # Shared message history for both hooks and web UI
        self._message_history: List[Dict[str, Any]] = []
        
        # Create hooks plugin with history tracking and config
        self.hooks_plugin = MessageDebuggerPlugin(plugin_dir, message_history=self._message_history, mcp_config=mcp_config)
        
        # Create web UI factory with plugin name for dynamic routing
        self.web_factory = MessageDebuggerWebFactory(self._message_history, name=name, server=self)
    
    # Hook interface - delegate to hooks plugin
    def get_hooks(self):
        """Return hooks from the hooks plugin."""
        return self.hooks_plugin.get_hooks()
    
    async def execute_hook(self, hook_type, context):
        """Execute hook - delegate to hooks plugin."""
        return await self.hooks_plugin.execute_hook(hook_type, context)
    
    # Web interface - delegate to web factory
    def get_web_router(self):
        """Return FastAPI router for web UI."""
        return self.web_factory.get_web_router()


PLUGIN_FACTORY = MessageDebuggerHybridPlugin
