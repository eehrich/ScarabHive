"""The shipped configuration: the plugin is loaded, records for the coder harness only
(and what inherits from it),
and never with one hook but not the other.

Reads the real configuration (load_settings + get_tool_server_config, the
inheritance included) and registers the hooks from it the way the app does.
"""
from __future__ import annotations

import pytest

from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.file_rewind import unregister_file_rewinder
from agent_system.hooks import HookType, load_hooks_config
from agent_system.hooks.registry import HookRegistry
from agent_system.plugins.discovery import register_plugin_hooks
from plugins.file_checkpoints.plugin import PLUGIN_FACTORY

BEFORE = "file_checkpoints.record_before_change"
AFTER = "file_checkpoints.record_after_change"


@pytest.fixture(scope="module")
def config():
    return load_settings()


@pytest.fixture
def plugin(config):
    instance = get_tool_server_config("file_checkpoints", config)
    assert instance is not None and instance.enabled, "file_checkpoints is not configured and enabled"
    built = PLUGIN_FACTORY("file_checkpoints", None, instance)
    yield instance, built
    unregister_file_rewinder(built)


def _agents(config):
    for name in config.plugins.servers:
        cfg = get_tool_server_config(name, config)
        if cfg is not None and getattr(cfg, "agent_config", None) is not None:
            yield name, cfg


def test_nothing_is_deleted_by_age_as_shipped(plugin):
    _, built = plugin

    assert built.retention_days == 0


async def test_the_hooks_record_for_the_coder_harness_and_nobody_else(config, plugin):
    instance, built = plugin
    registry = HookRegistry()
    names = await register_plugin_hooks("file_checkpoints", built, built.get_schema_data(), registry,
                                        load_hooks_config(config), getattr(instance, "hook_config", None))
    assert sorted(names) == [AFTER, BEFORE]
    defaults = {}
    for hook_type in (HookType.PRE_TOOL_CALL, HookType.POST_TOOL_CALL):
        [(name, _hook, _order, metadata)] = registry._hooks[hook_type]
        defaults[name] = metadata["enabled"]
    assert defaults == {BEFORE: False, AFTER: False}, "registered switched on for every agent"

    agents = list(_agents(config))
    assert len(agents) > 10, "fixture: the configuration lists hardly any agents"
    recording, half = [], []
    for agent_name, cfg in agents:
        hooks = cfg.agent_config.hooks
        on = [hook for hook in (BEFORE, AFTER)
              if hooks.enabled and (hooks.overrides or {}).get(hook, {}).get("enabled", defaults[hook])]
        if len(on) == 2:
            recording.append(agent_name)
        elif on:
            half.append(agent_name)
    assert half == [], f"one hook without the other records nothing: {half}"
    # gamedev and its tester are the coder harness too (type: coder / coder_tester); the
    # read-only explorer and reviewer record nothing, but are not named as spawns that don't.
    assert sorted(recording) == ["coder", "coder_explorer", "coder_reviewer", "coder_tester",
                                 "gamedev", "gamedev_tester"]
