"""Deliberate, in-process config reload for already-registered plugin servers.

Refreshes the config-derived, mutable state of live plugin instances from a
freshly parsed config WITHOUT tearing anything down (no restart, no dropped
sessions/jobs). It is opt-in: only servers whose underlying plugin implements
``reload_config(new_server_config) -> dict`` are refreshed.

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


#: A hybrid plugin (tools + hooks + web UI in one class) is what the registry
#: holds, and it keeps the actual tool server behind an attribute instead of
#: inheriting from it. Looking for ``reload_config`` only on the registered
#: object therefore missed the ONLY implementation there was: measured
#: 2026-09-01 on the live system, the reload reported "ok" and refreshed 0 of
#: 286 servers, ``sub_agent_manager`` among the unsupported — although it
#: implements the method on ``SubAgentManagerServer``.
_INNER_SERVER_ATTRS = ("server", "tool_server")


def _reload_target(plugin: Any):
    """The bound ``reload_config`` to call, or None if there is none.

    Checks the registered object first, then its inner tool server — the
    hybrid-plugin convention. Delegating per plugin would work too, but
    every future hybrid would have to remember it; the class of bug is
    what needs closing, not the one instance.
    """
    fn = getattr(plugin, "reload_config", None)
    if callable(fn):
        return fn
    for attr in _INNER_SERVER_ATTRS:
        inner = getattr(plugin, attr, None)
        if inner is None:
            continue
        fn = getattr(inner, "reload_config", None)
        if callable(fn):
            return fn
    return None


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
    from agent_system.plugins.tool_adapter import plugin_tool_registry
    from agent_system.config.settings import get_tool_server_config

    report: Dict[str, Any] = {
        "refreshed": [],
        "unchanged": [],
        "unsupported": [],
        "errors": [],
        "not_in_config": [],
    }

    for name in plugin_tool_registry.list_servers():
        adapter = plugin_tool_registry.get_server(name)
        plugin = getattr(adapter, "plugin_server", None)

        # The MERGED config, exactly as bootstrap built the live instance from
        # it (default_config + the ``type:`` inheritance chain). Handing over
        # the raw ``plugins.servers[name]`` entry instead would feed back the
        # unresolved base values: measured 2026-09-01 against the real config,
        # 133 of 203 agents carry a raw ``max_steps`` of 20 while their merged
        # value is 100 or 30 — a reload would have silently DOWNGRADED them.
        try:
            new_cfg = get_tool_server_config(name, fresh_config)
        except Exception as e:  # a broken entry must not abort the others
            logger.exception("could not resolve config for server '%s'", name)
            report["errors"].append(
                {"server": name, "error": f"config resolution failed: {e}"})
            continue

        if new_cfg is None:
            report["not_in_config"].append(name)
            continue

        reload_fn = _reload_target(plugin)
        if reload_fn is None:
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
