"""AgentSystem Plugin Framework.

This package provides the plugin discovery, loading, and tool adapter infrastructure
for the AgentSystem. Actual plugin implementations are located in src/plugins/.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

# Importing ANY submodule runs this file first. Re-exporting eagerly made a
# light module such as plugin_manifest (meant to be importable by the build
# scripts without the runtime) pull in tool_adapter, web_adapter and fastapi.
# PEP 562 defers each name to its first access, as agent_system.tools does;
# `from agent_system.plugins import discover_all_plugins` still works.
if TYPE_CHECKING:  # for type checkers and IDEs only -- never executed
    from .discovery import discover_plugins, discover_entrypoint_plugins, discover_all_plugins  # noqa: F401
    from .tool_adapter import PluginToolAdapter  # noqa: F401
    from .schema_loader import load_schema_from_dir  # noqa: F401
    from .web_adapter import PluginWebInterface, PluginWebRegistry, plugin_web_registry  # noqa: F401

#: Exported name -> submodule that defines it.
_LAZY_EXPORTS = {
    "discover_plugins": "discovery",
    "discover_entrypoint_plugins": "discovery",
    "discover_all_plugins": "discovery",
    "PluginToolAdapter": "tool_adapter",
    "load_schema_from_dir": "schema_loader",
    "PluginWebInterface": "web_adapter",
    "PluginWebRegistry": "web_adapter",
    "plugin_web_registry": "web_adapter",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    """Resolve a re-exported name on first access (PEP 562)."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module_name}", __name__), name)
    # Cache in the module namespace so __getattr__ runs once per name.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
