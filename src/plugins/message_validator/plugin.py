"""
Message Validator Plugin - Schema-Based Hook Plugin.

This plugin demonstrates schema-based hook implementation.
Hook definitions are loaded from schema.yaml, handlers from hooks.py.

**Plugin Type:** Hook-only (inherits from SchemaBasedPluginHook)

**Hooks Defined in schema.yaml:**
- message_validator: Main validation hook (pre_llm_call)
- message_structure_validator: Structure validation hook (pre_llm_call)
"""

from .hooks import MessageValidatorPlugin

# Plugin factory for discovery
def PLUGIN_FACTORY() -> MessageValidatorPlugin:
    """Factory function for plugin discovery."""
    return MessageValidatorPlugin()
