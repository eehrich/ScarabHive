"""
Context Optimizer Plugin - Schema-Based Hook Plugin.

This plugin demonstrates schema-based hook implementation.
Hook definitions are loaded from schema.yaml, handlers from hooks.py.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- context_optimizer: Main optimization hook (pre_llm_call)
- context_stats_logger: Optional stats logging hook (post_llm_call)
"""

from pathlib import Path
from .hooks import ContextOptimizerPlugin

# Plugin factory for discovery
def PLUGIN_FACTORY() -> ContextOptimizerPlugin:
    """Factory function for plugin discovery."""
    plugin_dir = Path(__file__).parent
    return ContextOptimizerPlugin(plugin_dir)
