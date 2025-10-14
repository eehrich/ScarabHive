"""
Context Optimizer Plugin - Hybrid Plugin (Hooks + Web UI).

This plugin demonstrates hybrid plugin implementation combining hooks and web UI.
Hook definitions are loaded from schema.yaml, handlers from hooks.py.
Web UI provides summarization history visualization.

**Plugin Type:** Hybrid (SchemaBasedPluginHook + Web UI)

**Hooks Defined in schema.yaml:**
- context_optimizer: Main optimization hook (pre_llm_call)
- context_stats_logger: Optional stats logging hook (post_llm_call)

**Web UI:**
- /panel: Summarization history dashboard
- /history: Get summarization events
- /stats: Get aggregate statistics
"""

from pathlib import Path
from typing import List, Dict, Any
from .hooks import ContextOptimizerPlugin
from .web_endpoints import ContextOptimizerWebEndpoints

# Shared state for hooks and web UI
_summarization_history: List[Dict[str, Any]] = []


# Plugin factory for discovery
def PLUGIN_FACTORY() -> tuple:
    """Factory function for plugin discovery.
    
    Returns tuple of (hooks_plugin, web_plugin) for hybrid plugin.
    """
    plugin_dir = Path(__file__).parent
    
    # Create hooks plugin with shared history
    hooks_plugin = ContextOptimizerPlugin(plugin_dir, summarization_history=_summarization_history)
    
    # Factory for web endpoints (will be called by web adapter)
    def web_factory(name: str, system_config, mcp_config):
        return ContextOptimizerWebEndpoints(
            name=name,
            system_config=system_config,
            mcp_config=mcp_config,
            summarization_history=_summarization_history
        )
    
    return (hooks_plugin, web_factory)
