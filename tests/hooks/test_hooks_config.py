"""Tests for hook configuration loading and management."""
from __future__ import annotations

import pytest

from agent_system.hooks.config import (
    HooksConfig,
    load_hooks_config,
)


def test_hooks_config_defaults():
    """Test HooksConfig with default values."""
    config = HooksConfig()
    
    assert config.enabled is True
    assert config.default_timeout == 30.0
    assert config.overrides == {}


def test_hooks_config_custom_values():
    """Test HooksConfig with custom values."""
    overrides = {
        "test_hook": {
            "enabled": False,
            "timeout": 10.0,
            "order": {"before": ["end"], "after": ["begin"]}
        }
    }
    
    config = HooksConfig(
        enabled=False,
        default_timeout=15.0,
        overrides=overrides
    )
    
    assert config.enabled is False
    assert config.default_timeout == 15.0
    assert config.overrides == overrides


def _config_dir(root, plugins_yaml: str, later_yaml: str | None = None):
    """A config/config.yaml that includes plugins.yaml, as the real one does
    -- and, given `later_yaml`, a second include merged after it."""
    cfg = root / "cfg"
    cfg.mkdir()
    includes = "  - plugins.yaml\n" + ("  - later.yaml\n" if later_yaml is not None else "")
    (cfg / "config.yaml").write_text("includes:\n" + includes, encoding="utf-8")
    (cfg / "plugins.yaml").write_text(plugins_yaml, encoding="utf-8")
    if later_yaml is not None:
        (cfg / "later.yaml").write_text(later_yaml, encoding="utf-8")
    return cfg / "config.yaml"


def test_load_hooks_config_without_settings_is_defaults():
    config = load_hooks_config()

    assert (config.enabled, config.default_timeout, config.overrides) == (True, 30.0, {})


def test_hooks_come_from_the_loaded_config_not_the_working_directory(tmp_path, monkeypatch):
    """The section was re-read from config/plugins.yaml relative to the
    working directory -- whatever --config or AGENT_CONFIG_PATH named."""
    config_path = _config_dir(tmp_path, """
hooks:
  enabled: false
  default_timeout: 15.0
  overrides:
    test_hook:
      enabled: true
      timeout: 5.0
      order:
        before: ["end"]
""")
    # A different plugins.yaml where the old code looked
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    (work / "config" / "plugins.yaml").write_text(
        "hooks:\n  default_timeout: 99.0\n", encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)

    from agent_system.config.settings import load_settings
    config = load_hooks_config(load_settings(str(config_path)))

    assert config.enabled is False
    assert config.default_timeout == 15.0
    # Only what the YAML sets: registration asks `'key' in override`
    assert config.overrides == {"test_hook": {"enabled": True, "timeout": 5.0,
                                              "order": {"before": ["end"]}}}


def test_a_misspelled_hook_setting_fails_the_load(tmp_path, monkeypatch):
    """The file reader passed an unknown key through, where nothing read it:
    the override looked configured and did nothing."""
    config_path = _config_dir(tmp_path, "hooks:\n  overrides:\n    x:\n      timout: 5\n")
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)

    from pydantic import ValidationError
    from agent_system.config.settings import load_settings
    with pytest.raises(ValidationError, match="timout"):
        load_settings(str(config_path))


@pytest.mark.asyncio
async def test_a_commented_out_key_means_nothing_set(tmp_path, monkeypatch):
    """All lines under a key commented out leaves it null. As `order: None`
    the hook raised inside registration and was silently not registered; as
    `enabled: None` it registered switched off."""
    config_path = _config_dir(tmp_path, """
hooks:
  overrides:
    p.a:
      order:
        # before: [end]
    p.b:
      enabled:
    p.c:
""")
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    from agent_system.config.settings import load_settings
    from agent_system.hooks.registry import HookRegistry
    from agent_system.plugins.discovery import register_plugin_hooks
    from tests.plugins.test_plugin_hook_discovery import MockHookPlugin

    hooks_config = load_hooks_config(load_settings(str(config_path)))
    registry = HookRegistry(default_timeout=30.0)
    metadata = {"hooks": [{"name": n, "type": "pre_llm_call", "enabled": True}
                          for n in ("a", "b", "c")]}
    registered = await register_plugin_hooks("p", MockHookPlugin("p"), metadata,
                                             registry, hooks_config)

    assert sorted(registered) == ["p.a", "p.b", "p.c"]
    assert all(registry.get_hook_info(name)["enabled"] is True for name in registered)


@pytest.mark.parametrize("hooks", [None, {"overrides": None}])
def test_an_empty_section_or_overrides_key_loads_as_defaults(hooks):
    from agent_system.config.models import AgentSystemConfig

    config = load_hooks_config(AgentSystemConfig(hooks=hooks))

    assert (config.enabled, config.default_timeout, config.overrides) == (True, 30.0, {})


def test_empty_keys_in_the_master_config_are_dropped_by_the_model(tmp_path, monkeypatch):
    """config.yaml's own `hooks:` is not merged from an include, so the
    stripping before the merge never sees it -- the model has to."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "hooks:\n  overrides:\n    p:\n      enabled: false\n    p.c:\n      order:\n        before:\n",
        encoding="utf-8")
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    from agent_system.config.settings import load_settings

    config = load_hooks_config(load_settings(str(cfg / "config.yaml")))

    assert config.overrides == {"p": {"enabled": False}}


@pytest.mark.parametrize("first", [None, "hooks:\n  enabled: true\n"])
def test_a_wrongly_shaped_hooks_section_in_an_include_fails_the_load(tmp_path, monkeypatch, first):
    """Merged onto an earlier section it raised inside the per-file except:
    the section vanished and the log claimed the whole file had been skipped."""
    if first is None:
        config_path = _config_dir(tmp_path, "hooks: []\n")
    else:
        config_path = _config_dir(tmp_path, first, later_yaml="hooks: []\n")
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)

    from pydantic import ValidationError
    from agent_system.config.settings import load_settings
    with pytest.raises(ValidationError, match="hooks"):
        load_settings(str(config_path))


@pytest.mark.parametrize("later", [
    "hooks:\n  enabled:\n",
    "hooks:\n  overrides:\n",
    "hooks:\n  overrides:\n    p.h:\n",
    "hooks:\n  overrides:\n    p.h:\n      timeout:\n",
    "hooks:\n",
])
def test_an_empty_key_in_a_later_file_does_not_undo_an_earlier_one(tmp_path, monkeypatch, later):
    """deep_merge put the null over the earlier value and the model turned it
    into the default: an empty `enabled:` switched hooks back on."""
    config_path = _config_dir(
        tmp_path, "hooks:\n  enabled: false\n  overrides:\n    p.h:\n      timeout: 5\n",
        later_yaml=later)
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    from agent_system.config.settings import load_settings

    config = load_hooks_config(load_settings(str(config_path)))

    assert config.enabled is False
    assert config.overrides == {"p.h": {"timeout": 5.0}}


@pytest.mark.asyncio
async def test_a_commented_out_exact_entry_leaves_the_plugin_wide_override(tmp_path, monkeypatch):
    """Validated to an empty override, `p.c:` counted as set and hid `p`."""
    config_path = _config_dir(tmp_path, """
hooks:
  overrides:
    p:
      enabled: false
    p.c:
    p.d:
      order:
        before:
""")
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    from agent_system.config.settings import load_settings
    from agent_system.hooks.registry import HookRegistry
    from agent_system.plugins.discovery import register_plugin_hooks
    from tests.plugins.test_plugin_hook_discovery import MockHookPlugin

    hooks_config = load_hooks_config(load_settings(str(config_path)))
    registry = HookRegistry(default_timeout=30.0)
    metadata = {"hooks": [{"name": n, "type": "pre_llm_call", "enabled": True} for n in ("c", "d")]}
    registered = await register_plugin_hooks("p", MockHookPlugin("p"), metadata, registry, hooks_config)

    assert sorted(registered) == ["p.c", "p.d"]
    assert [registry.get_hook_info(n)["enabled"] for n in ("p.c", "p.d")] == [False, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tool_integration", "bootstrapped"])
async def test_registration_uses_the_hooks_of_its_config(monkeypatch, path):
    """Both registration paths hand the config's overrides on."""
    from agent_system.config.models import AgentSystemConfig
    from agent_system.plugins import discovery

    settings = AgentSystemConfig(hooks={"overrides": {"probe.h": {"enabled": False}}})
    server = type("Server", (), {"plugin_schema": {"hooks": [{"name": "h"}]},
                                 "plugin_server": object()})()
    registry = type("Registry", (), {"list_servers": lambda self: ["probe"],
                                     "get_server": lambda self, name: server})()
    received = []

    async def capture(**kwargs):
        received.append(kwargs["hooks_config"].overrides)
        return []

    monkeypatch.setattr(discovery, "register_plugin_hooks", capture)
    if path == "tool_integration":
        from agent_system.tools.integration import ToolServerIntegration
        integration = ToolServerIntegration.__new__(ToolServerIntegration)  # no global side effects
        integration.plugin_registry = registry
        await integration._register_plugin_hooks(settings)
    else:
        from agent_system.plugins import tool_adapter
        monkeypatch.setattr(tool_adapter, "plugin_tool_registry", registry)
        monkeypatch.setattr(discovery, "_BOOTSTRAPPED_HOOKS_REGISTERED", False)
        await discovery.register_bootstrapped_plugin_hooks(settings)

    assert received == [{"probe.h": {"enabled": False}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tool_integration", "bootstrapped"])
async def test_registration_reports_overrides_that_match_no_hook(monkeypatch, path):
    """Both registration paths check the override keys once all hooks are in."""
    from agent_system.config.models import AgentSystemConfig
    from agent_system.plugins import discovery

    settings = AgentSystemConfig()
    registry = type("Registry", (), {"list_servers": lambda self: [],
                                     "get_server": lambda self, name: None})()
    checked = []
    monkeypatch.setattr(discovery, "warn_unknown_hook_overrides", lambda s, *a: checked.append(s))
    if path == "tool_integration":
        from agent_system.tools.integration import ToolServerIntegration
        integration = ToolServerIntegration.__new__(ToolServerIntegration)  # no global side effects
        integration.plugin_registry = registry
        await integration._register_plugin_hooks(settings)
    else:
        from agent_system.plugins import tool_adapter
        monkeypatch.setattr(tool_adapter, "plugin_tool_registry", registry)
        monkeypatch.setattr(discovery, "_BOOTSTRAPPED_HOOKS_REGISTERED", False)
        await discovery.register_bootstrapped_plugin_hooks(settings)

    assert checked == [settings]
