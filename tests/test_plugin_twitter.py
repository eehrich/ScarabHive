from pathlib import Path

import pytest

from agent_system.plugins import discover_all_plugins


@pytest.mark.asyncio
async def test_twitter_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'twitter_search' in plugins
    factory = plugins['twitter_search']
    inst = factory('twitter_search', {})
    assert inst is not None
