from pathlib import Path

from agent_system.mcp import plugins


def test_discovered_plugin_has_metadata():
    # discover plugins under the project plugins/ directory
    plugins_map = plugins.discover_all_plugins(dirs=[Path('plugins')])
    assert 'example' in plugins_map
    factory = plugins_map['example']
    meta = getattr(factory, '_plugin_metadata', None)
    assert meta is not None
    assert meta.get('name') == 'example'
