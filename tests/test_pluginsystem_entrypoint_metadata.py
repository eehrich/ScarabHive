from pathlib import Path

import yaml
import pytest


def test_entrypoint_plugin_metadata(monkeypatch, tmp_path):
    # Create a fake package directory with plugin.yaml
    pkg_dir = tmp_path / "fakepkg"
    pkg_dir.mkdir()
    meta = {"name": "fakepkg", "description": "entrypoint plugin", "version": "1.2"}
    (pkg_dir / "plugin.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

    # Create a fake factory callable and ensure its __module__ points to the fake package
    def factory(*a, **k):
        return None

    factory.__module__ = "fakepkg.module"

    # Fake entry-point object with .name and .load()
    class FakeEP:
        def __init__(self, name, factory):
            self.name = name
            self._factory = factory

        def load(self):
            return self._factory

        def __repr__(self):
            return f"<FakeEP {self.name}>"

    ep = FakeEP("fakepkg", factory)
    # mimic metadata.entry_points() objects that include a .group attribute
    ep.group = "agent_system.mcp_plugins"

    # Monkeypatch importlib.metadata.entry_points to return our fake entrypoint
    monkeypatch.setattr("importlib.metadata.entry_points", lambda: [ep])

    # Monkeypatch importlib.util.find_spec to return a spec with submodule_search_locations
    from types import SimpleNamespace

    spec = SimpleNamespace(submodule_search_locations=[str(pkg_dir)])
    monkeypatch.setattr("importlib.util.find_spec", lambda name: spec)

    # Execute discovery and assert metadata attached
    from agent_system.plugins import discover_entrypoint_plugins

    found = discover_entrypoint_plugins()
    assert "fakepkg" in found
    f = found["fakepkg"]
    attached = getattr(f, "_plugin_metadata", None)
    assert attached is not None
    assert attached.get("name") == "fakepkg"
