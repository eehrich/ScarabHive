"""Shared server/tool-name resolution building blocks.

Flat tool names (``v6_json_manage_json``) do not carry a separator between
server name and tool name, so resolving them requires a longest-prefix search
over ``'_'``-joined segments. That loop — and the "local registry, then plugin
registry" cascade — used to be copied across
``Agent._get_server_from_any_registry``, ``Agent._resolve_flat_tool_name`` and
``ToolExecutionManager._invoke_tool`` (with per-copy drift risk).

This module owns each building block ONCE; those three call sites compose
them in their historical precedence order (behavior-preserving consolidation).

NOT consolidated: ``ToolExecutionManager._execute_plugin_tool`` (the LLM tool
path) keeps its own older exact-match cascade WITHOUT a prefix walk — it
receives names that already went through schema-build name mapping, so a
prefix walk there would change which server handles a call. If you change
resolution order here, check that function separately.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)


def resolve_registry_server(registry: Any, tool_integration_manager: Any,
                            server_name: str) -> Optional[Any]:
    """Resolve a server by EXACT name: local registry first (contains all
    servers: plugins + config agents), then the plugin registry's adapter
    (unwrapped to its ``plugin_server``).

    This is the central cascade behind ``Agent._get_server_from_any_registry``.
    Returns None if nothing matches.
    """
    if registry is not None:
        try:
            server = registry.get(server_name)
            if server:
                return server
        except Exception as e:
            logger.debug(f"Failed to get server '{server_name}' from local registry: {e}")

    tool_integration = getattr(tool_integration_manager, "tool_integration", None)
    if tool_integration is not None and tool_integration.initialized:
        try:
            plugin_adapter = tool_integration.plugin_registry.get_server(server_name)
            if plugin_adapter and hasattr(plugin_adapter, 'plugin_server'):
                return plugin_adapter.plugin_server
        except Exception as e:
            logger.debug(f"Failed to get server '{server_name}' from plugin registry: {e}")

    return None


def resolve_longest_prefix(lookup: Callable[[str], Optional[Any]],
                           tool_name: str) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve a flat tool name via its longest matching server-name prefix.

    Tries ``lookup(prefix)`` for every ``'_'``-joined prefix of ``tool_name``,
    longest first (``a_b_c_d`` → ``a_b_c``, ``a_b``, ``a``). Returns
    ``(resolved, prefix)`` for the first hit, else ``(None, None)``.

    ``lookup`` decides WHAT is being resolved (a server, a plugin adapter, ...)
    — the segment-walk itself lives only here. Exceptions from ``lookup``
    PROPAGATE (matching the historical inline walk): a raising registry must
    surface its fault, not be silently skipped to a shorter — possibly wrong —
    prefix. Lookups that want per-attempt error tolerance implement it
    themselves (``resolve_registry_server`` does).
    """
    parts = tool_name.split("_")
    for i in range(len(parts) - 1, 0, -1):
        candidate = "_".join(parts[:i])
        resolved = lookup(candidate)
        if resolved:
            return resolved, candidate
    return None, None
