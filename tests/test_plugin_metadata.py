from pathlib import Path

import pytest

from agent_system.plugins import discovery as plugins


@pytest.mark.asyncio
async def test_plugin_metadata_discovery_has_metadata():
    # discover plugins under the project plugins/ directory
    default_dir = Path('plugins')
    if not default_dir.exists():
        alt = Path('src') / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins_map = plugins.discover_all_plugins(dirs=[default_dir])
    assert 'example' in plugins_map
    factory = plugins_map['example']
    meta = getattr(factory, '_plugin_metadata', None)
    assert meta is not None
    assert meta.get('name') == 'example'
