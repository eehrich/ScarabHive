from pathlib import Path

from agent_system.mcp.plugins import discover_all_plugins


def test_duckduckgo_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    plugins = discover_all_plugins([repo_root / 'plugins'])
    assert 'duckduckgo_search' in plugins
    factory = plugins['duckduckgo_search']
    # Ensure factory is callable / instantiable
    inst = factory('duckduckgo_search', {})
    assert inst is not None
