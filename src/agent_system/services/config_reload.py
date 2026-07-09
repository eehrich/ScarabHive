"""Deliberate, in-process config reload for already-registered plugin servers.

Refreshes the config-derived, mutable state of live plugin instances from a
freshly parsed config WITHOUT tearing anything down (no restart, no dropped
sessions/jobs). It is opt-in: only servers whose underlying plugin implements
``reload_config(new_mcp_config) -> dict`` are refreshed.

Scope (by design): this covers editing an EXISTING server's config — e.g. a
sub-agent manager's ``allowed_agents`` / limits / phase filtering, or an agent's
template vars. Registering a BRAND-NEW agent/plugin definition still needs a
restart (the instance has to be created and wired into hooks/tool-schemas). The
reload is always triggered deliberately (POST /admin/reload-config, `agent-cli
reload`) — there is no file watcher.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def reload_plugin_configs(fresh_config: Any) -> Dict[str, Any]:
    """Refresh live plugin instances from ``fresh_config``.

    Args:
        fresh_config: a freshly loaded ``AgentSystemConfig`` (force_reload=True).

    Returns:
        A per-category report:
        - ``refreshed``: [{"server": name, "changes": {field: {old, new}}}]
        - ``unchanged``: [name]  (supports reload, but nothing changed)
        - ``unsupported``: [name]  (no ``reload_config`` — needs a restart to change)
        - ``errors``: [{"server": name, "error": str}]
        - ``not_in_config``: [name]  (live but absent from the fresh config)
    """
    # The process-wide registry that holds the LIVE plugin instances. Its
    # adapters expose the underlying plugin as ``.plugin_server``.
    from agent_system.plugins.mcp_adapter import plugin_mcp_registry

    servers_cfg: Dict[str, Any] = {}
    plugins_cfg = getattr(fresh_config, "plugins", None)
    if plugins_cfg is not None and getattr(plugins_cfg, "servers", None):
        servers_cfg = plugins_cfg.servers or {}

    report: Dict[str, Any] = {
        "refreshed": [],
        "unchanged": [],
        "unsupported": [],
        "errors": [],
        "not_in_config": [],
    }

    for name in plugin_mcp_registry.list_servers():
        adapter = plugin_mcp_registry.get_server(name)
        plugin = getattr(adapter, "plugin_server", None)
        new_cfg = servers_cfg.get(name)

        if new_cfg is None:
            report["not_in_config"].append(name)
            continue

        reload_fn = getattr(plugin, "reload_config", None)
        if not callable(reload_fn):
            report["unsupported"].append(name)
            continue

        try:
            changed = reload_fn(new_cfg) or {}
            if changed:
                report["refreshed"].append({"server": name, "changes": changed})
            else:
                report["unchanged"].append(name)
        except Exception as e:  # one bad reload must not abort the others
            logger.exception("reload_config failed for server '%s'", name)
            report["errors"].append({"server": name, "error": str(e)})

    logger.info(
        "config reload: %d refreshed, %d unchanged, %d unsupported, %d errors",
        len(report["refreshed"]), len(report["unchanged"]),
        len(report["unsupported"]), len(report["errors"]),
    )
    return report
