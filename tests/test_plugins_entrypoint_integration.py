from importlib import metadata
from pathlib import Path
import pytest

from agent_system.plugins import discovery as plugins


class FakeEP:
    def __init__(self, name, group, load_callable):
        self.name = name
        self.group = group
        self._load = load_callable

    def load(self):
        return self._load


class FakeDist:
    def __init__(self, eps):
        self._eps = eps

    def entry_points(self):
        return self._eps


@pytest.mark.asyncio
async def test_plugins_integration_discover_entrypoint_and_filesystem(monkeypatch, tmp_path):
    # filesystem plugin exists in project plugins/ (or src/plugins/) - ensure discover_all_plugins sees it
    default_dir = Path("plugins")
    if not default_dir.exists():
        alt = Path("src") / "plugins"
        if alt.exists():
            default_dir = alt
    assert default_dir.exists()

    # create a fake entry point that points to a factory returning an ExampleServer
    def factory(name=None, cfg=None, ssl_verify=True):
        class X:
            def __init__(self, *a, **k):
                self.name = name or "ep"

            async def call(self, *a, **k):
                return {"status": "ep", "name": self.name}

        return X(name)

    fake_ep = FakeEP('ep_example', 'agent_system.mcp_plugins', factory)
    fake_dist = FakeDist([fake_ep])

    # monkeypatch metadata.distributions or entry_points depending on API
    try:
        monkeypatch.setattr(metadata, 'entry_points', lambda: [fake_ep])
    except Exception:
        # older API
        monkeypatch.setattr(metadata, 'distributions', lambda: [fake_dist])

    # Discover all plugins (filesystem + entrypoint)
    plugins_map = plugins.discover_all_plugins(dirs=[default_dir])

    # Should find filesystem example and the entrypoint
    assert 'example' in plugins_map
    assert 'ep_example' in plugins_map

    # instantiate and test calls
    fs_factory = plugins_map['example']
    ep_factory = plugins_map['ep_example']

    fs_server = fs_factory('example', {})
    ep_server = ep_factory('ep_example', {})

    # Call their call() methods (filesystem plugin is async)
    res1 = await fs_server.call("example_status", {})
    res2 = await ep_server.call("example_status", {})

    assert res1['status'] == 'active'  # Example plugin status response
    assert res2['status'] == 'ep'
