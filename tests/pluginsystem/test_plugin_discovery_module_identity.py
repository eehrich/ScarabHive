"""Discovering the plugins twice must yield the SAME factory and class objects.

Two discoveries run per process (servers/bootstrap.py and
config/settings._get_plugins_cached) and discovery used to exec every
plugin.py again -- measured 2026-09-04: of 50 shared types only 20 factories
were identical, and a class defined in plugin.py (SubAgentManagerHybridPlugin)
existed twice, so ``isinstance`` against it was wrong between the two runs.
"""
from __future__ import annotations

import pytest
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
            "def PLUGIN_FACTORY(name, system_config, server_config):\n"
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
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    return None\n",
        encoding="utf-8")
    repaired = discover_all_plugins(dirs=[root])
    assert "exec_probe" in repaired


def test_a_module_that_exits_hard_leaves_nothing_behind(tmp_path):
    """``except Exception`` does not catch a module-level ``sys.exit()``.

    The torso would stay in sys.modules and be handed out as "loaded" from
    then on -- and "the process dies anyway" does not hold: under pytest a
    SystemExit from one test leaves the session running, and every later test
    would see the plugin missing.
    """
    import sys

    root = tmp_path / "plugins"
    d = root / "exit_probe"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text('[plugin]\nname = "exit_probe"\n', encoding="utf-8")
    (d / "plugin.py").write_text("import sys\nsys.exit('bad config')\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        discover_all_plugins(dirs=[root])
    assert "plugins.exit_probe.plugin" not in sys.modules, \
        "the torso of the exited module stayed behind and would be reused as 'loaded'"

    (d / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    return None\n",
        encoding="utf-8")
    assert "exit_probe" in discover_all_plugins(dirs=[root])


def test_a_single_file_plugin_does_not_take_a_packages_name(tmp_path):
    """Single-file plugins register under ``plugins.<stem>`` -- the same name
    a directory plugin owns as its synthetic package. Overwriting it would
    break that plugin's relative imports for the rest of the process."""
    import sys

    root = tmp_path / "plugins"
    d = root / "twin"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text('[plugin]\nname = "twin_dir"\n', encoding="utf-8")
    (d / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    return 'from-the-directory'\n",
        encoding="utf-8")
    (root / "twin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    return 'from-the-file'\n",
        encoding="utf-8")

    found = discover_all_plugins(dirs=[root])

    assert "twin" in found, "fixture: nothing was discovered at all"
    assert "plugins.twin.plugin" in sys.modules, \
        "fixture: the directory plugin never ran, so there is no package to protect"
    package = sys.modules.get("plugins.twin")
    assert package is not None and hasattr(package, "__path__"), \
        "the single-file plugin replaced the directory plugin's package in sys.modules"


def test_a_single_file_plugin_belongs_to_the_root_it_lies_in(tmp_path):
    """A discovery root that is not called "plugins" keeps its own package.

    Under a fixed name the roots share one package: whichever is scanned first
    decides where its __path__ points, and every relative import of the other
    root's plugins then reads this one's files.
    """
    import sys

    root = tmp_path / "plugins_elsewhere"
    root.mkdir()
    (root / "lonely.py").write_text(
        "PLUGIN_NAME = 'lonely'\n"
        "def PLUGIN_FACTORY(name, system_config, server_config):\n"
        "    return 'from-the-other-root'\n",
        encoding="utf-8")

    found = discover_all_plugins(dirs=[root])

    assert "lonely" in found, "fixture: the single-file plugin was not discovered at all"
    assert "plugins_elsewhere.lonely" in sys.modules, "the module was not named after its root"
    package = sys.modules.get("plugins_elsewhere")
    assert package is not None and package.__path__ == [str(root.resolve())], \
        "the root's package does not point at the root"
    assert "plugins.lonely" not in sys.modules, "the module went into another root's package"


def test_a_shared_submodule_that_failed_is_retried(tmp_path, caplog):
    """Shared modules (writer_core and friends) are imported once per process
    and skipped afterwards via ``sys.modules``. A submodule whose import blew
    up must therefore not stay behind -- it would never be tried again, and
    the failure was only a DEBUG line."""
    import logging
    import sys

    root = tmp_path / "plugins"
    shared = root / "shared_probe"
    shared.mkdir(parents=True)
    (shared / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (shared / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        discover_all_plugins(dirs=[root])
    assert any("shared_probe" in r.message for r in caplog.records), \
        "a shared module that does not load must be a warning, not a debug line"
    assert "plugins.shared_probe.broken" not in sys.modules, \
        "the failed submodule stayed behind and would never be retried"

    (shared / "broken.py").write_text("VALUE = 'repaired'\n", encoding="utf-8")
    discover_all_plugins(dirs=[root])
    assert sys.modules["plugins.shared_probe.broken"].VALUE == "repaired"


def test_a_module_someone_else_imported_survives_a_failed_registration(tmp_path, monkeypatch):
    """Taking back the torso must take back only OUR module.

    A shared module that pytest (or any importer) already loaded properly is
    still in sys.modules when a submodule of it blows up; dropping it there
    would force a re-import and hand out a second identity of every class in
    it -- the exact bug the module reuse in this file exists to prevent.
    """
    import sys
    import types

    root = tmp_path / "plugins"
    shared = root / "keep_probe"
    shared.mkdir(parents=True)
    (shared / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (shared / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    already_imported = types.ModuleType("plugins.keep_probe")
    already_imported.MARKER = "the one somebody else imported"
    monkeypatch.setitem(sys.modules, "plugins.keep_probe", already_imported)

    discover_all_plugins(dirs=[root])

    assert sys.modules["plugins.keep_probe"] is already_imported, \
        "the properly imported module was dropped because a submodule failed"
