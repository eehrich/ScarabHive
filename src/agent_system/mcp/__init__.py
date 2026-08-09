"""
Model Context Protocol (MCP) Implementation for AgentSystem

This package holds two layers that only look like one:

* the **plugin base** (``core``, ``status``, ``base``, ``schema_mixin``,
  ``schema_based``) -- the internal API every plugin is written against, and
* the **protocol client** (``security``, ``tool_cache``, ``http_transport``,
  ``streaming_transport``, ``client``, ``integration``) -- what talks to
  external MCP servers.

The client knows the base; the base does not know the client. Keep it that
way: it is what allows the client to move out into a plugin of its own.

Key Components:
- MCPIntegration: Main integration class
- StandardMCPClient: MCP client implementation
- PluginMCPAdapter: Available from agent_system.plugins package
- MCPSecurityManager: Authentication and security handling
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

# Importing ANY submodule runs this file first. Re-exporting the names below
# eagerly therefore made every plugin pay for the whole protocol client: the
# 44 plugins that do `from agent_system.mcp.schema_based import ...` pulled in
# client.py, integration.py and their aiohttp/fastapi dependencies -- 11
# modules and ~730 ms for code they never call. Worse, it welded the two
# layers together, so the client could not be extracted.
#
# PEP 562 defers each name to its first attribute access. The public API is
# unchanged: `from agent_system.mcp import MCPIntegration` still works.

# noqa below: these exist purely so type checkers and IDEs still resolve the
# re-exported names. __all__ is built from _LAZY_EXPORTS at runtime, and ruff
# cannot follow that indirection back to these imports.
if TYPE_CHECKING:  # for type checkers and IDEs only -- never executed
    from .integration import MCPIntegration  # noqa: F401
    from .client import StandardMCPClient, MCPClientManager  # noqa: F401
    from .core import MCPTool, MCPResource, MCPPrompt, MCPMessage, MCPError  # noqa: F401
    from .security import MCPSecurityManager, configure_security  # noqa: F401
    from .schema_based import SchemaBasedMCPServer  # noqa: F401

#: Exported name -> submodule that defines it.
_LAZY_EXPORTS = {
    "MCPIntegration": "integration",
    "StandardMCPClient": "client",
    "MCPClientManager": "client",
    "MCPTool": "core",
    "MCPResource": "core",
    "MCPPrompt": "core",
    "MCPMessage": "core",
    "MCPError": "core",
    "MCPSecurityManager": "security",
    "configure_security": "security",
    "SchemaBasedMCPServer": "schema_based",
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
