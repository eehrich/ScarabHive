"""Keep the machine store out of the real data/ directory.

Since the store is read at construction and written by
``add_machine(persistent=true)``, every test that builds this plugin would
otherwise touch ``data/ssh_control/`` and ``config/mcp.yaml`` in the working
copy -- reading real hosts into a test, and writing test hosts into the
operator's store. Autouse, because the danger is in the constructor and not
in any single test's body.
"""
import pytest

from plugins.ssh_control import machine_store


@pytest.fixture(autouse=True)
def isolated_machine_store(tmp_path, monkeypatch):
    """Point the store and the legacy path at this test's tmp_path."""
    monkeypatch.setattr(machine_store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(machine_store, "LEGACY_PATH", tmp_path / "legacy_mcp.yaml")
    return tmp_path / "store"
