from pathlib import Path

from agent_system.mcp.plugins import discover_all_plugins


def test_web_scraper_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'web_scraper' in plugins
    factory = plugins['web_scraper']
    inst = factory('web_scraper', {})
    assert inst is not None
