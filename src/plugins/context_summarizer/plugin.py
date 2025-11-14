"""Context Summarizer Plugin - Factory and exports.

Hybrid plugin: Schema-based hooks + Web UI for viewing summarization history.
Hook implementations are in hooks.py, web endpoints in web_endpoints.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any, TYPE_CHECKING

from agent_system.plugins.web_base import SchemaBasedPluginWebInterface
from .hooks import ContextSummarizerPlugin
from .web_endpoints import ContextSummarizerWebFactory

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class ContextSummarizerHybridPlugin(SchemaBasedPluginWebInterface):
    """Hybrid plugin that provides both hooks and web capabilities."""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with standard hybrid plugin signature."""
        # Initialize base class (loads schema automatically)
        super().__init__(name, system_config, mcp_config)
        
        plugin_dir = Path(__file__).parent
        
        # Shared history list for both hooks and web UI
        self._summarization_history: List[Dict[str, Any]] = []
        
        # Create hooks plugin with history tracking
        self.hooks_plugin = ContextSummarizerPlugin(plugin_dir, summarization_history=self._summarization_history)
        
        # Create web UI factory with plugin name for dynamic routing
        self.web_factory = ContextSummarizerWebFactory(
            self._summarization_history,
            name=name,
            plugin=self
        )
    
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

    def get_static_assets(self):
        """Context summarizer does not expose static assets."""
        return None

    def get_panels(self):
        """Return panel configuration derived from schema."""
        schema = self.get_schema_data()
        web_ui = schema.get("web_ui", {}) if schema else {}
        panel = web_ui.get("panel", {})

        if not panel.get("enabled", False):
            return []

        panel_id = panel.get("panel_id", f"{self.name}_panel")
        endpoint = panel.get("endpoint", f"/plugins/{self.name}/panel")

        return [
            {
                "id": panel_id,
                "title": panel.get("title", "Context Summarizer"),
                "url": endpoint,
                "icon": panel.get("icon", "📝"),
                "category": panel.get("category", "monitoring"),
            }
        ]
    

PLUGIN_FACTORY = ContextSummarizerHybridPlugin
