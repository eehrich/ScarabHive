from pathlib import Path

import pytest

from agent_system.plugins import discover_all_plugins


@pytest.mark.asyncio
async def test_duckduckgo_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'duckduckgo_search' in plugins
    factory = plugins['duckduckgo_search']
    # Ensure factory is callable / instantiable
    inst = factory('duckduckgo_search', {})
    assert inst is not None
