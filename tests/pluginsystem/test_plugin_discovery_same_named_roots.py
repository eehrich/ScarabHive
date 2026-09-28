"""Two plugin directories of the same name do not load one module twice.

A root's package name is its directory's name, so two roots both called
``plugins`` with a folder of the same name would both be the module
``plugins.<folder>``. The second replaced the first in sys.modules -- its relative
imports running against the first root's files -- and was dropped as a
duplicate type anyway, and every later discovery executed the first root's
plugin afresh: a new factory each time.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from agent_system.plugins import discovery
from agent_system.plugins.tool_adapter import PluginToolRegistry

PROBE = "same_named_roots_probe"


def _root(base: Path, origin: str) -> Path:
    root = base / origin / "plugins"
    plugin = root / PROBE
    plugin.mkdir(parents=True)
    (plugin / "helper.py").write_text(f'ORIGIN = "{origin}"\n', encoding="utf-8")
    (plugin / "plugin.py").write_text(
        "from .helper import ORIGIN\n\n\nclass Probe:\n    origin = ORIGIN\n\n\nPLUGIN_FACTORY = Probe\n",
        encoding="utf-8")
    return root


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "discover_entrypoint_plugins", lambda group: {})
    before = set(sys.modules)
    yield _root(tmp_path, "one"), _root(tmp_path, "two")
    for name in [n for n in sys.modules if n not in before or n.startswith(f"plugins.{PROBE}")]:
        module = sys.modules[name]
        places = [getattr(module, "__file__", None) or "", *(getattr(module, "__path__", None) or [])]
        if any(str(place).startswith(str(tmp_path)) for place in places):
            del sys.modules[name]


def test_the_first_root_keeps_a_folder_both_have(roots, caplog):
    one, two = roots

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        first = discovery.discover_all_plugins([one, two])[PROBE]
        again = discovery.discover_all_plugins([one, two])[PROBE]

    assert first.origin == "one"
    assert again is first, "the first root's plugin was executed afresh at the next discovery"
    assert sys.modules[f"plugins.{PROBE}.plugin"].__file__.startswith(str(one))
    assert f"'{PROBE}'" in caplog.text and str(two) in caplog.text, "the skipped folder was not named"


def test_the_tool_registry_keeps_it_too(roots):
    one, two = roots
    registry, again = PluginToolRegistry(), PluginToolRegistry()

    registry.discover_plugins([str(one), str(two)])
    again.discover_plugins([str(one), str(two)])

    assert registry.plugin_factories[PROBE].origin == "one"
    assert again.plugin_factories[PROBE] is registry.plugin_factories[PROBE]


def test_a_folder_with_no_plugin_in_the_first_root_blocks_nothing(roots):
    """Only a plugin or shared module of the first root claims the name: a folder
    with neither a plugin.py nor an __init__.py there blocks nothing."""
    one, two = roots
    (one / f"{PROBE}_elsewhere").mkdir()  # no plugin.py: nothing loaded, no module
    loaded = two / f"{PROBE}_elsewhere"
    loaded.mkdir()
    (loaded / "plugin.py").write_text("class Probe:\n    origin = 'two'\n\n\nPLUGIN_FACTORY = Probe\n",
                                      encoding="utf-8")

    plugins = discovery.discover_all_plugins([one, two])

    assert plugins[f"{PROBE}_elsewhere"].origin == "two"


def test_a_single_file_plugin_both_roots_have_stays_the_first_root_s(roots):
    one, two = roots
    for root, origin in ((one, "one"), (two, "two")):
        (root / f"{PROBE}_file.py").write_text(
            f"class Probe:\n    origin = '{origin}'\n\n\nPLUGIN_FACTORY = Probe\n", encoding="utf-8")

    first = discovery.discover_all_plugins([one, two])[f"{PROBE}_file"]
    again = discovery.discover_all_plugins([one, two])[f"{PROBE}_file"]

    assert first.origin == "one" and again is first


def test_a_shared_module_both_roots_have_stays_the_first_root_s(roots):
    """A plugin of the first root that imports its shared module reads the first
    root's -- not one a later root put in its place."""
    one, two = roots
    for root, origin in ((one, "one"), (two, "two")):
        shared = root / f"{PROBE}_common"
        shared.mkdir()
        (shared / "__init__.py").write_text(f"ORIGIN = '{origin}'\n", encoding="utf-8")
    (one / PROBE / "plugin.py").write_text(
        "def origin():\n    from ..same_named_roots_probe_common import ORIGIN\n    return ORIGIN\n\n\n"
        "class Probe:\n    pass\n\n\nPLUGIN_FACTORY = Probe\n", encoding="utf-8")

    discovery.discover_all_plugins([one, two])

    assert sys.modules[f"plugins.{PROBE}.plugin"].origin() == "one"


def test_the_same_root_listed_twice_loads_and_warns_nothing_twice(roots, caplog):
    one, _ = roots

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        plugins = discovery.discover_all_plugins([one, one])

    assert plugins[PROBE].origin == "one"
    assert "is not loaded" not in caplog.text, caplog.text


PLUGIN = "class Probe:\n    origin = 'two'\n\n\nPLUGIN_FACTORY = Probe\n"
BROKEN = "raise RuntimeError('broken')\n"


@pytest.mark.parametrize("kind", ["single file", "shared module", "shared module against a folder"])
def test_a_name_the_first_root_has_stays_its_when_its_module_fails(roots, caplog, kind):
    """Freed by the failure, the name would go to the second root in one discovery and back to the first in
    the next, which would run against -- and drop -- what the second one left in sys.modules: one discovery
    would have the second root's plugin and the next one not, or a module would mix the files of both."""
    one, two = roots
    name = f"{PROBE}_x"
    if kind == "single file":
        (one / f"{name}.py").write_text(BROKEN, encoding="utf-8")
        (two / f"{name}.py").write_text(PLUGIN, encoding="utf-8")
    else:
        (one / name).mkdir()
        (one / name / "__init__.py").write_text(BROKEN, encoding="utf-8")
        (two / name).mkdir()
        if kind == "shared module":
            (two / name / "__init__.py").write_text("ORIGIN = 'two'\n", encoding="utf-8")
        else:
            (two / name / "plugin.py").write_text(PLUGIN, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        first = discovery.discover_all_plugins([one, two])
        again = discovery.discover_all_plugins([one, two])

    assert name not in first and name not in again
    from_two = [n for n, m in list(sys.modules.items()) if (n == f"plugins.{name}" or n.startswith(f"plugins.{name}."))
                and str(getattr(m, "__file__", None) or "").startswith(str(two))]
    assert from_two == [], "the second root's module took the name"
    skipped = [r.getMessage() for r in caplog.records if f"'{name}' in {two}" in r.getMessage()]
    assert len(skipped) == 2, skipped  # one per discovery


@pytest.mark.parametrize("kind", ["folder", "single file"])
def test_a_plugin_that_fails_puts_back_the_module_another_directory_left_under_its_name(roots, kind):
    """Discovered one after the other (not together), two directories of the same name share the module names.
    A failed load dropped what was there -- the other directory's module -- and the next discovery of that
    directory executed its plugin afresh: a new factory, a second identity of its classes."""
    one, two = roots
    name = PROBE if kind == "folder" else f"{PROBE}_file"
    if kind == "folder":
        (one / PROBE / "plugin.py").write_text(BROKEN, encoding="utf-8")
    else:
        (one / f"{name}.py").write_text(BROKEN, encoding="utf-8")
        (two / f"{name}.py").write_text(PLUGIN, encoding="utf-8")

    first = discovery.discover_all_plugins([two])[name]
    assert name not in discovery.discover_all_plugins([one])
    again = discovery.discover_all_plugins([two])[name]

    assert again is first, "the other directory's plugin was executed afresh"


def test_a_skipped_shared_module_is_warned_about_once_and_a_folder_that_loads_nothing_not_at_all(roots, caplog):
    one, two = roots
    for root, origin in ((one, "one"), (two, "two")):
        shared = root / f"{PROBE}_common"
        shared.mkdir()
        (shared / "__init__.py").write_text(f"ORIGIN = '{origin}'\n", encoding="utf-8")
    (one / f"{PROBE}_file.py").write_text("class Probe:\n    pass\n\n\nPLUGIN_FACTORY = Probe\n", encoding="utf-8")
    (two / f"{PROBE}_file").mkdir()  # no plugin.py: loads nothing, blocks nothing
    (two / f"{PROBE}_file" / "README.md").write_text("notes\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_system.plugins.discovery"):
        discovery.discover_all_plugins([one, two])

    skipped = [r.getMessage() for r in caplog.records if "is not loaded" in r.getMessage()]
    assert len([m for m in skipped if f"'{PROBE}_common'" in m]) == 1, skipped
    assert not [m for m in skipped if f"'{PROBE}_file'" in m], skipped



def test_a_shared_module_that_fails_puts_back_the_plugin_package_it_replaced(roots):
    """Another directory's plugin package under the name, from an earlier discovery: taken back without it,
    its plugin module stayed without a parent package, and a relative import it makes later found none."""
    one, two = roots
    (one / PROBE).rename(one / f"{PROBE}_unused")
    (one / PROBE).mkdir()  # a shared module in the first directory, a plugin in the second
    (one / PROBE / "__init__.py").write_text(BROKEN, encoding="utf-8")

    discovery.discover_all_plugins([two])
    package = sys.modules[f"plugins.{PROBE}"]
    discovery.discover_all_plugins([one])

    assert sys.modules.get(f"plugins.{PROBE}") is package, "the plugin package was dropped"
