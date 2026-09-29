"""Tool servers: the internal plugin API and the layer that boots it.

This package holds two layers that only look like one:

* the **plugin base** (``base``, ``status``, ``schema_mixin``, ``schema_based``)
  -- the internal API every plugin is written against, and
* the **integration layer** (``tool_cache``, ``integration``) -- which
  bootstraps plugins and looks up whoever federates external tools.

Nothing here speaks a protocol. Talking to foreign MCP servers is the
``mcp_client`` plugin's job, which speaks the current protocol through the
official SDK. The base must stay independent of the layer above it -- that
independence is what let the client move out in the first place.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

# Importing ANY submodule runs this file first. Re-exporting the names below
# eagerly therefore made every plugin pay for the whole protocol client: the
# 44 plugins that do `from agent_system.tools.schema_based import ...` pulled in
# client.py, integration.py and their aiohttp/fastapi dependencies -- 11
# modules and ~730 ms for code they never call. Worse, it welded the two
# layers together, so the client could not be extracted.
#
# PEP 562 defers each name to its first attribute access. The public API is
# unchanged: `from agent_system.tools import ToolServerIntegration` still works.

# noqa below: these exist purely so type checkers and IDEs still resolve the
# re-exported names. __all__ is built from _LAZY_EXPORTS at runtime, and ruff
# cannot follow that indirection back to these imports.
if TYPE_CHECKING:  # for type checkers and IDEs only -- never executed
    from .integration import ToolServerIntegration  # noqa: F401
    from .base import ToolDef, ToolServer, ToolServerCapability, ToolServerRegistry  # noqa: F401
    from .schema_based import SchemaBasedToolServer  # noqa: F401
    from .hook_tool_server import SchemaBasedHookToolServer  # noqa: F401

#: Exported name -> submodule that defines it.
_LAZY_EXPORTS = {
    "ToolServerIntegration": "integration",
    "ToolDef": "base",
    "ToolServer": "base",
    "ToolServerCapability": "base",
    "ToolServerRegistry": "base",
    "SchemaBasedToolServer": "schema_based",
    "SchemaBasedHookToolServer": "hook_tool_server",
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
