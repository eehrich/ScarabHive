"""The plugin catalog must answer "which types exist" exactly like discovery
does -- without importing a plugin.

Measured 2026-09-04 in a fresh process: resolving config inheritance ran a full
discovery, 0.85 s and 187 plugin modules in sys.modules, for a membership test.
With the catalog: 0.235 s and zero plugin modules.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from agent_system.plugins.catalog import PluginCatalog
from agent_system.plugins.discovery import default_plugin_dirs, discover_all_plugins

REPO = Path(__file__).resolve().parents[2]


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(REPO), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_catalog_names_exactly_what_discovery_registers():
    """The contract, checked against the real plugins: same names, no more, no
    less. This is what makes the catalog usable as a membership test -- a
    plugin form the catalog cannot name (legacy directory, single file,
    PLUGIN_NAME) has to fall back to discovery, and this test is what notices
    when a new one appears."""
    dirs = default_plugin_dirs()
    assert dirs, "fixture: no plugin directory resolved"

    catalog = set(PluginCatalog(dirs).types())
    discovered = set(discover_all_plugins(dirs=dirs))

    assert catalog, "fixture: the catalog found nothing at all"
    assert catalog == discovered, (
        f"only in the catalog: {sorted(catalog - discovered)}\n"
        f"only in discovery:   {sorted(discovered - catalog)}")


def test_a_manifest_name_does_not_rename_a_plugin():
    """``todo`` ships a manifest saying ``todo_management``; discovery
    registers the FOLDER name, so the catalog must too."""
    catalog = PluginCatalog(default_plugin_dirs())
    manifest = catalog.manifest("todo")

    assert manifest is not None, "fixture: the todo plugin has no manifest any more"
    assert manifest.get("name") == "todo_management", \
        "fixture: this test needs a manifest whose name differs from its folder"
    assert "todo" in catalog.types()
    assert "todo_management" not in catalog.types()


def test_a_library_plugin_is_not_a_type():
    """A manifest without an entrypoint module (coder, writer_publish: agents
    and skills, no MCP server) is skipped by discovery -- the catalog skips it
    the same way, or config inheritance would resolve against a type that
    cannot be built."""
    from agent_system.plugins.plugin_manifest import load_plugin_metadata

    coder = REPO / "src" / "plugins" / "coder"
    assert load_plugin_metadata(coder), "fixture: coder has no manifest"
    assert not (coder / "plugin.py").exists(), \
        "fixture: coder grew an entrypoint, pick another library plugin"

    catalog = PluginCatalog([REPO / "src" / "plugins"])
    assert "coder" not in catalog.types()
    assert catalog.manifest("coder") is None, "a non-type must not answer with a manifest"


def test_reading_the_types_imports_no_plugin(tmp_path):
    """The whole point: naming the types must not execute plugin code."""
    out = _run("""
        import sys
        from agent_system.plugins.catalog import PluginCatalog
        from agent_system.plugins.discovery import default_plugin_dirs
        types = PluginCatalog(default_plugin_dirs()).types()
        loaded = [m for m in sys.modules if m.startswith('plugins.') or m.startswith('plugins_')]
        print('types', len(types))
        print('plugin_modules', len(loaded))
    """)
    assert "plugin_modules 0" in out, out
    assert int(out.split("types ")[1].split()[0]) > 10, out


def test_config_inheritance_imports_no_plugin():
    """The production path that paid for it: resolving every server's config."""
    out = _run("""
        import sys
        from agent_system.config.settings import load_settings, get_mcp_config_by_name
        cfg = load_settings()
        for name in cfg.plugins.servers:
            get_mcp_config_by_name(name, cfg)
        loaded = [m for m in sys.modules if m.startswith('plugins.') or m.startswith('plugins_')]
        print('servers', len(cfg.plugins.servers))
        print('plugin_modules', len(loaded))
    """)
    assert "plugin_modules 0" in out, out
    assert int(out.split("servers ")[1].split()[0]) > 100, out


def test_a_form_the_catalog_cannot_name_falls_back_to_discovery(tmp_path):
    """A legacy directory without a manifest: its name is only known after the
    module ran, so the catalog must not simply drop it."""
    root = tmp_path / "plugins"
    legacy = root / "legacy_probe"
    legacy.mkdir(parents=True)
    (legacy / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    return None\n",
        encoding="utf-8")

    modern = root / "modern_probe"
    modern.mkdir()
    (modern / "plugin.toml").write_text('[plugin]\nname = "modern_probe"\n', encoding="utf-8")
    (modern / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    return None\n",
        encoding="utf-8")

    types = PluginCatalog([root]).types()

    assert "modern_probe" in types
    assert "legacy_probe" in types, "the manifest-less plugin was dropped instead of discovered"


def test_a_module_without_a_factory_is_not_a_type(tmp_path):
    """The entry FILE existing is not enough: discovery drops a module that
    exports no factory, so a catalog counting the file alone would hand config
    inheritance a type nothing can build -- and inheritance would stop there
    instead of chaining to the server of the same name."""
    root = tmp_path / "plugins"
    probe = root / "no_factory_probe"
    probe.mkdir(parents=True)
    (probe / "plugin.toml").write_text('[plugin]\nname = "no_factory_probe"\n', encoding="utf-8")
    (probe / "plugin.py").write_text("VERSION = '1.0'\n", encoding="utf-8")

    assert "no_factory_probe" not in discover_all_plugins(dirs=[root]), \
        "fixture: discovery accepted a module without a factory"
    assert "no_factory_probe" not in PluginCatalog([root]).types()


def test_an_unreadable_entry_file_hands_over_to_discovery(tmp_path, monkeypatch):
    """The catalog decides from the module's SOURCE. When it cannot read it,
    the honest answer is not a guess: a silent skip would drop a live plugin
    out of every membership test for the rest of the process."""
    root = tmp_path / "plugins"
    probe = root / "unreadable_probe"
    probe.mkdir(parents=True)
    (probe / "plugin.toml").write_text('[plugin]\nname = "unreadable_probe"\n', encoding="utf-8")
    (probe / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    return None\n",
        encoding="utf-8")

    monkeypatch.setattr(PluginCatalog, "_read", staticmethod(lambda entry_file: None))

    assert "unreadable_probe" in PluginCatalog([root]).types()


def test_an_entrypoint_without_a_colon_keeps_both_defaults(tmp_path):
    """Discovery splits an entrypoint only when it carries a colon; ``server``
    alone leaves module AND factory at their defaults. A catalog that read the
    value as the module name would look at a file that does not exist and drop
    a live plugin."""
    root = tmp_path / "plugins"
    probe = root / "colonless_probe"
    probe.mkdir(parents=True)
    (probe / "plugin.toml").write_text(
        '[plugin]\nname = "colonless_probe"\nentrypoint = "server"\n', encoding="utf-8")
    (probe / "plugin.py").write_text(
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    return None\n",
        encoding="utf-8")

    assert "colonless_probe" in discover_all_plugins(dirs=[root]), \
        "fixture: discovery stopped reading a colonless entrypoint as plugin.py"
    assert "colonless_probe" in PluginCatalog([root]).types()


def test_a_plugin_named_by_its_register_falls_back_to_discovery(tmp_path):
    """``register()`` returns the name AND the factory -- discovery reads it
    before it looks for a factory at all, so a module that carries nothing
    else is still a plugin. A directory plugin may carry it, not only a
    single-file one; PLUGIN_NAME is not the only form the catalog cannot read.
    """
    root = tmp_path / "plugins"
    probe = root / "register_probe"
    probe.mkdir(parents=True)
    (probe / "plugin.toml").write_text('[plugin]\nname = "register_probe"\n', encoding="utf-8")
    (probe / "plugin.py").write_text(
        "def _build(name, system_config, mcp_config):\n"
        "    return None\n"
        "def register():\n"
        "    return 'named_by_register', _build\n",
        encoding="utf-8")

    discovered = discover_all_plugins(dirs=[root])
    assert "named_by_register" in discovered, "fixture: discovery no longer honours register()"

    types = PluginCatalog([root]).types()
    assert "named_by_register" in types, "the catalog kept the folder name a register() renamed"
    assert "register_probe" not in types, \
        "the catalog invented the folder name next to the one discovery registers"


def test_a_plugin_that_renames_itself_falls_back_to_discovery(tmp_path):
    """PLUGIN_NAME overrides the folder name -- only readable by executing the
    module, so the catalog hands over to discovery."""
    root = tmp_path / "plugins"
    d = root / "folder_name"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text('[plugin]\nname = "folder_name"\n', encoding="utf-8")
    (d / "plugin.py").write_text(
        'PLUGIN_NAME = "its_own_name"\n'
        "def PLUGIN_FACTORY(name, system_config, mcp_config):\n"
        "    return None\n",
        encoding="utf-8")

    types = PluginCatalog([root]).types()

    assert "its_own_name" in types, "the catalog kept the folder name a module renamed"
    assert "folder_name" not in types, \
        "the catalog invented a type discovery never registers (the folder next to the real name)"
