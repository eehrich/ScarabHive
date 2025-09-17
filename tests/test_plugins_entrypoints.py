from importlib import metadata

from agent_system.plugins import discovery as plugins


class DummyEP:
    def __init__(self, name, group, load_callable):
        self.name = name
        self.group = group
        self._load = load_callable

    def load(self):
        return self._load


def test_plugins_discover_entrypoint_plugins(monkeypatch):
    called = {}

    def factory(cfg=None):
        called['ok'] = True
        return 'server'

    # build a fake entry_points() return shape
    fake_eps = [DummyEP('dummy', 'agent_system.mcp_plugins', factory)]

    class FakeMetadata:
        def entry_points(self):
            return fake_eps

    monkeypatch.setattr(metadata, 'entry_points', lambda: fake_eps)

    found = plugins.discover_entrypoint_plugins(group='agent_system.mcp_plugins')
    # Should expose the factory under the entrypoint name
    assert 'dummy' in found
    server = found['dummy']()
    assert server == 'server'