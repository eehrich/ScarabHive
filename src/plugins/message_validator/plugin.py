"""
Message Validator Plugin - Schema-Based Hook Plugin.

This plugin demonstrates schema-based hook implementation.
Hook definitions are loaded from schema.yaml, handlers from hooks.py.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- message_validator: Main validation hook (pre_llm_call)
- message_structure_validator: Structure validation hook (pre_llm_call)
"""

from pathlib import Path
from typing import Any
from .hooks import MessageValidatorPlugin

# Plugin factory for discovery
def PLUGIN_FACTORY(name: str, system_config: Any, mcp_config: Any) -> MessageValidatorPlugin:
    """Factory function for plugin discovery.
    
    Args:
        name: Plugin instance name
        system_config: System configuration
        mcp_config: MCP-specific configuration (contains config from plugins.yaml)
        
    Returns:
        MessageValidatorPlugin instance
    """
    plugin_dir = Path(__file__).parent
    return MessageValidatorPlugin(plugin_dir, mcp_config)
