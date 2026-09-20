"""Keep the machine store out of the real data/ directory.

Since the store is read at construction and written by
``add_machine(persistent=true)``, every test that builds this plugin would
otherwise touch ``data/ssh_control/`` and ``config/mcp.yaml`` in the working
copy -- reading real hosts into a test, and writing test hosts into the
operator's store. Autouse, because the danger is in the constructor and not
in any single test's body.
"""
import pytest

from agent_system.plugins import cache as cache_module
from plugins.ssh_control import machine_store


@pytest.fixture(autouse=True)
def isolated_machine_store(tmp_path, monkeypatch):
    """Point the store and the legacy path at this test's tmp_path."""
    monkeypatch.setattr(machine_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(machine_store, "LEGACY_PATH", tmp_path / "legacy_mcp.yaml")
    return tmp_path / "store"


@pytest.fixture(autouse=True)
def isolated_plugin_cache(tmp_path, monkeypatch):
    """The tool server builds a PluginCache in its constructor, and that cache
    creates its directory there -- without this, every test would leave
    data/cache/<name>/ behind in the working copy."""
    root = tmp_path / "cache"
    monkeypatch.setattr(cache_module, "default_cache_root", lambda: root)
    return root
