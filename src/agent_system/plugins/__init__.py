"""AgentSystem Plugin Framework.

This package provides the plugin discovery, loading, and MCP adapter infrastructure
for the AgentSystem. Actual plugin implementations are located in src/plugins/.
"""

from __future__ import annotations

# Export main plugin framework components
from .discovery import discover_plugins, discover_entrypoint_plugins, discover_all_plugins
from .mcp_adapter import PluginMCPAdapter
from .schema_loader import load_schema_from_dir
from .web_adapter import PluginWebInterface, PluginWebRegistry, plugin_web_registry

__all__ = [
    "discover_plugins",
    "discover_entrypoint_plugins", 
    "discover_all_plugins",
    "PluginMCPAdapter",
    "load_schema_from_dir",
    "PluginWebInterface",
    "PluginWebRegistry",
    "plugin_web_registry",
]