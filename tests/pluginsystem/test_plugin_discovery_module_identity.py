"""Discovering the plugins twice must yield the SAME factory and class objects.

Two discoveries run per process (servers/bootstrap.py and
config/settings._get_plugins_cached) and discovery used to exec every
plugin.py again -- measured 2026-09-04: of 50 shared types only 20 factories
were identical, and a class defined in plugin.py (SubAgentManagerHybridPlugin)
existed twice, so ``isinstance`` against it was wrong between the two runs.
"""
from __future__ import annotations

from pathlib import Path

from agent_system.plugins.discovery import discover_all_plugins

PLUGIN_DIRS = [Path(__file__).resolve().parents[2] / "src" / "plugins"]


def test_second_discovery_returns_the_same_objects():
    first = discover_all_plugins(dirs=PLUGIN_DIRS)
    second = discover_all_plugins(dirs=PLUGIN_DIRS)

    assert "sub_agent_manager" in first and "basic_agent" in first, "fixture: expected plugins missing"
    different = [name for name in first if name in second and first[name] is not second[name]]
    assert not different, f"re-discovery produced new factory objects for: {different}"


def test_a_plugin_class_keeps_its_identity_across_discoveries():
    """The concrete symptom: a class defined in plugin.py must be one class."""
    import sys

    discover_all_plugins(dirs=PLUGIN_DIRS)
    module_first = sys.modules["plugins.sub_agent_manager.plugin"]
    cls_first = module_first.SubAgentManagerHybridPlugin
    discover_all_plugins(dirs=PLUGIN_DIRS)
    module_second = sys.modules["plugins.sub_agent_manager.plugin"]

    assert module_second is module_first
    assert module_second.SubAgentManagerHybridPlugin is cls_first


def test_a_different_file_under_the_same_module_name_is_still_loaded(tmp_path):
    """Reuse is keyed by file, not by name: tests build throwaway plugins
    under the same package names in different directories, and each must
    get its own module."""
    # The module name is derived from the plugin ROOT's directory name, so two
    # roots both called "plugins" put their plugin under the same name.
    for label in ("one", "two"):
        d = tmp_path / label / "plugins" / "identity_probe"
        d.mkdir(parents=True)
        (d / "plugin.toml").write_text('[plugin]\nname = "identity_probe"\n', encoding="utf-8")
        (d / "plugin.py").write_text(
            f"LABEL = {label!r}\n"
            "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
            "    return None\n",
            encoding="utf-8")

    one = discover_all_plugins(dirs=[tmp_path / "one" / "plugins"])
    two = discover_all_plugins(dirs=[tmp_path / "two" / "plugins"])

    assert one["identity_probe"] is not two["identity_probe"]
    import sys
    assert sys.modules["plugins.identity_probe.plugin"].LABEL == "two"


def test_a_plugin_that_failed_to_execute_is_not_remembered_as_loaded(tmp_path, caplog):
    """A failed exec must leave nothing behind in sys.modules.

    Reuse asks sys.modules for (module name, file). The torso of a module
    whose exec raised looks exactly like a loaded one, so the next discovery
    would hand it out, the plugin would stay missing and the warning would
    never be logged again -- the failure becomes invisible from the second
    discovery on.
    """
    import logging
    import sys

    root = tmp_path / "plugins"
    d = root / "exec_probe"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text('[plugin]\nname = "exec_probe"\n', encoding="utf-8")
    (d / "plugin.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        broken = discover_all_plugins(dirs=[root])
    assert "exec_probe" not in broken, "fixture: the broken plugin must not be discovered"
    assert any("boom" in r.message or "boom" in str(r.exc_info) for r in caplog.records), \
        "fixture: the failed exec was not even logged the first time"
    assert "plugins.exec_probe.plugin" not in sys.modules, \
        "the half-executed module stayed behind and would be reused as 'loaded'"

    (d / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    return None\n",
        encoding="utf-8")
    repaired = discover_all_plugins(dirs=[root])
    assert "exec_probe" in repaired
