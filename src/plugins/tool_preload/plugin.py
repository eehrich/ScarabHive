"""Tool Preload Plugin — Factory.

Hook-only plugin: after a user turn, executes the predictable opening tool
calls (configured per agent as regex rules) BEFORE the first LLM call and
appends the results to the conversation — saving one LLM round trip per call.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- preload: PRE_LLM_CALL — matches rules, dispatches tools, appends results
"""

from pathlib import Path
from typing import Any

from .hooks import ToolPreloadPlugin


def PLUGIN_FACTORY(name: str, system_config: Any, mcp_config: Any) -> ToolPreloadPlugin:
    """Factory function for plugin discovery.

    Args:
        name: Plugin instance name
        system_config: System configuration
        mcp_config: MCP-specific configuration (contains config from plugins.yaml)

    Returns:
        ToolPreloadPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return ToolPreloadPlugin(plugin_dir, mcp_config)
