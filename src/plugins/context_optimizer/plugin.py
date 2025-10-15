"""
Context Optimizer Plugin - Schema-Based Hook Plugin.

This plugin demonstrates schema-based hook implementation.
Hook definitions are loaded from schema.yaml, handlers from hooks.py.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- context_optimizer: Main optimization hook (pre_llm_call)
- context_stats_logger: Optional stats logging hook (post_llm_call)
"""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from .hooks import ContextOptimizerPlugin

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


# Plugin factory for discovery
def PLUGIN_FACTORY(name: str = None, system_config: "AgentSystemConfig" = None, mcp_config: "MCPConfig" = None) -> ContextOptimizerPlugin:
    """Factory function for plugin discovery.
    
    Args:
        name: Plugin name (ignored, for compatibility)
        system_config: System configuration (ignored, for compatibility)
        mcp_config: MCP configuration (ignored, for compatibility)
    
    Returns:
        ContextOptimizerPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return ContextOptimizerPlugin(plugin_dir)
