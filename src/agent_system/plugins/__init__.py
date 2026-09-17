"""AgentSystem Plugin Framework.

This package provides the plugin discovery, loading, and tool adapter infrastructure
for the AgentSystem. Actual plugin implementations are located in src/plugins/.
"""

from __future__ import annotations

# Export main plugin framework components
from .discovery import discover_plugins, discover_entrypoint_plugins, discover_all_plugins
from .tool_adapter import PluginToolAdapter
from .schema_loader import load_schema_from_dir
from .web_adapter import PluginWebInterface, PluginWebRegistry, plugin_web_registry

__all__ = [
    "discover_plugins",
    "discover_entrypoint_plugins", 
    "discover_all_plugins",
    "PluginToolAdapter",
    "load_schema_from_dir",
    "PluginWebInterface",
    "PluginWebRegistry",
    "plugin_web_registry",
]