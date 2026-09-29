"""Hook registration says what would otherwise fail silently."""
from __future__ import annotations

import logging

import pytest

from agent_system.config.settings import load_settings
from agent_system.hooks import HooksConfig
from agent_system.hooks.registry import HookRegistry
from agent_system.plugins.discovery import register_plugin_hooks, warn_unknown_hook_overrides
from tests.plugins.test_plugin_hook_discovery import MockHookPlugin


async def test_a_hook_without_its_own_timeout_gets_the_configured_default():
    registry = HookRegistry(default_timeout=30.0)
    metadata = {"hooks": [{"name": "test_hook", "type": "pre_llm_call"}]}

    await register_plugin_hooks("test_plugin", MockHookPlugin("test_plugin"), metadata,
                                registry, HooksConfig(default_timeout=7.0))

    assert registry.get_hook_info("test_plugin.test_hook")["timeout"] == 7.0


@pytest.mark.parametrize("hook_type", ["pre_llm_call", "pre_tool_call", "post_tool_call"])
async def test_a_hook_of_a_type_that_fires_registers_without_a_warning(hook_type, caplog):
    """The tool hooks fire since the tool loop calls them; registering one used
    to warn that it never would."""
    registry = HookRegistry(default_timeout=30.0)
    metadata = {"hooks": [{"name": "some_hook", "type": hook_type}]}

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        await register_plugin_hooks("test_plugin", MockHookPlugin("test_plugin"), metadata,
                                    registry, HooksConfig())

    assert registry.get_hook_info("test_plugin.some_hook")["type"] == hook_type
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("hook_type, on_error, warned", [
    ("pre_tool_call", "block", False),
    ("pre_tool_call", "Block", True),
    ("post_tool_call", "block", True),
])
async def test_an_on_error_that_has_no_effect_is_reported(hook_type, on_error, warned, caplog):
    """on_error: block makes a failing pre_tool_call hook block its call; any
    other spelling reads as the default and lets the call through a policy
    hook without a word."""
    registry = HookRegistry(default_timeout=30.0)
    metadata = {"hooks": [{"name": "policy", "type": hook_type, "on_error": on_error}]}

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        await register_plugin_hooks("test_plugin", MockHookPlugin("test_plugin"), metadata,
                                    registry, HooksConfig())

    assert registry.get_hook_info("test_plugin.policy")["metadata"]["on_error"] == on_error
    assert any("on_error" in r.getMessage() for r in caplog.records) is warned


CONFIG = """
hooks:
  overrides:
    test_plugin:
      enabled: true
    renamed_plugin.test_hook:
      enabled: true
plugins:
  servers:
    good_agent:
      type: basic_agent
      enabled: true
      agent_config:
        hooks:
          overrides:
            test_plugin.test_hook: {enabled: true}
    bad_agent:
      type: basic_agent
      enabled: true
      agent_config:
        hooks:
          overrides:
            test_plugin.tset_hook: {enabled: true}
            test_plugin: {enabled: true}
    disabled_agent:
      type: basic_agent
      enabled: false
      agent_config:
        hooks:
          overrides:
            nowhere.hook: {enabled: true}
"""


async def test_override_keys_that_match_no_hook_are_reported(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("includes:\n  - plugins.yaml\n", encoding="utf-8")
    (tmp_path / "plugins.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    settings = load_settings(str(tmp_path / "config.yaml"))
    registry = HookRegistry(default_timeout=30.0)
    await register_plugin_hooks("test_plugin", MockHookPlugin("test_plugin"),
                                {"hooks": [{"name": "test_hook", "type": "pre_llm_call"}]},
                                registry, HooksConfig())

    warnings = warn_unknown_hook_overrides(settings, registry)

    reported = sorted(w.split("'")[1] for w in warnings)
    # A plugin-wide key works globally, not per agent; a disabled agent runs no hooks.
    assert reported == ["renamed_plugin.test_hook", "test_plugin", "test_plugin.tset_hook"], warnings
    assert all("bad_agent" in w for w in warnings if "agent_config" in w), warnings


async def test_an_override_warning_is_logged_once_per_process(monkeypatch, caplog):
    from agent_system.config.models import AgentSystemConfig
    from agent_system.plugins import discovery

    monkeypatch.setattr(discovery, "_REPORTED_OVERRIDE_WARNINGS", set())
    settings = AgentSystemConfig(hooks={"overrides": {"nowhere.hook": {"enabled": False}}})
    registry = HookRegistry(default_timeout=30.0)

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        first = warn_unknown_hook_overrides(settings, registry)
        second = warn_unknown_hook_overrides(settings, registry)

    assert len(first) == len(second) == 1
    assert sum("nowhere.hook" in r.getMessage() for r in caplog.records) == 1
