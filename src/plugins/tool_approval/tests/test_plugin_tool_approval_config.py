"""The shipped configuration switches approvals on for nobody.

Merging the plugin must change nothing for anyone who does not enable it. So
this reads the real configuration (load_settings + get_tool_server_config, the
inheritance included) and registers the hook from it the way the app does, then
asks every configured agent whether the hook would run for it.
"""
from __future__ import annotations

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.hooks import HookType, load_hooks_config
from agent_system.hooks.registry import HookRegistry
from agent_system.plugins.discovery import register_plugin_hooks
from plugins.tool_approval.plugin import PLUGIN_FACTORY

HOOK = "tool_approval.check_tool_call"


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture(scope="module")
def instance(config):
    cfg = get_tool_server_config("tool_approval", config)
    assert cfg is not None, "tool_approval is not configured"
    return cfg


def _agents(config):
    """Every configured server that is an agent, as its resolved config."""
    for name in config.plugins.servers:
        cfg = get_tool_server_config(name, config)
        if cfg is not None and getattr(cfg, "agent_config", None) is not None:
            yield name, cfg


def test_the_instance_config_is_one_the_hook_can_read(instance):
    plugin = PLUGIN_FACTORY("tool_approval", None, instance)

    settings = plugin.settings_for({})

    assert settings.mode in ("ask", "auto", "off")


async def test_no_agent_runs_the_hook_as_shipped(config, instance):
    plugin = PLUGIN_FACTORY("tool_approval", None, instance)
    registry = HookRegistry()
    names = await register_plugin_hooks("tool_approval", plugin, plugin.get_schema_data(), registry,
                                        load_hooks_config(config), getattr(instance, "hook_config", None))
    assert names == [HOOK]
    [(name, _hook, _order, metadata)] = registry._hooks[HookType.PRE_TOOL_CALL]
    assert metadata["enabled"] is False, "registered switched on for every agent"
    assert metadata.get("on_error") == "block"

    agents = list(_agents(config))
    assert len(agents) > 10, "fixture: the configuration lists hardly any agents"
    switched_on = []
    for agent_name, cfg in agents:
        hooks = cfg.agent_config.hooks
        override = (hooks.overrides or {}).get(HOOK, {})
        if hooks.enabled and override.get("enabled", metadata["enabled"]):
            switched_on.append(agent_name)
    assert switched_on == []
