"""Agent Continuation Plugin — Factory.

Hook-only plugin that evaluates whether an agent's text response is a
final answer or an intermediate status report.  When intermediate, it
signals the LLM loop to inject a continuation prompt and keep going.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- evaluate_completion: POST_LLM_CALL — checks if the response is final
"""

from pathlib import Path
from typing import Any

from .hooks import AgentContinuationPlugin


def PLUGIN_FACTORY(name: str, system_config: Any, mcp_config: Any) -> AgentContinuationPlugin:
    """Factory function for plugin discovery.

    Args:
        name: Plugin instance name
        system_config: System configuration
        mcp_config: MCP-specific configuration (contains config from plugins.yaml)

    Returns:
        AgentContinuationPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return AgentContinuationPlugin(plugin_dir, mcp_config)
