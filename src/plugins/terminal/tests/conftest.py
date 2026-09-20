"""Keep the plugin cache out of the working copy's data/ directory.

``TerminalServer`` builds a ``PluginCache`` in its constructor, and that cache
creates its directory right there -- so every test constructing a server would
leave ``data/cache/<name>/`` behind in the repo. Autouse, because the directory
is made in the constructor and not in any single test's body.
"""
import pytest

from agent_system.plugins import cache as cache_module


@pytest.fixture(autouse=True)
def isolated_plugin_cache(tmp_path, monkeypatch):
    """Point every PluginCache built during a test at this test's tmp_path."""
    root = tmp_path / "cache"
    monkeypatch.setattr(cache_module, "default_cache_root", lambda: root)
    return root
