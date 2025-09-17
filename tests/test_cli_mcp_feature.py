import json
from pathlib import Path

import yaml

from agent_system import cli


def _make_cfg_with_managed(managed_path: Path):
    # Return a minimal config object with `mcp.config_file` attribute
    from types import SimpleNamespace

    cfg = SimpleNamespace()
    cfg.mcp = SimpleNamespace()
    cfg.mcp.config_file = str(managed_path)
    return cfg


def test_mcp_feature_list_no_probe(monkeypatch, tmp_path, capsys):
    master = tmp_path / "agent.yaml"
    master.write_text("mcp: {}")

    managed = tmp_path / "mcp.yaml"
    data = {"mcp": {"external_servers": {"test_server": {"url": "http://localhost", "features": {"sequential_thinking": False}}}}}
    managed.write_text(yaml.safe_dump(data), encoding="utf-8")

    cfg = _make_cfg_with_managed(managed)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "--config", str(master), "mcp", "feature", "test_server", "list", "--no-probe"])
    cli.main()
    out = capsys.readouterr().out
    # handler prints JSON; parse and assert configured_features present
    obj = json.loads(out)
    assert obj.get("server") == "test_server"
    assert obj.get("configured_features", {}).get("sequential_thinking") is False


def test_mcp_feature_set_persists(monkeypatch, tmp_path, capsys):
    master = tmp_path / "agent.yaml"
    master.write_text("mcp: {}")

    managed = tmp_path / "mcp.yaml"
    data = {"mcp": {"external_servers": {"test_server": {"url": "http://localhost", "features": {}}}}}
    managed.write_text(yaml.safe_dump(data), encoding="utf-8")

    cfg = _make_cfg_with_managed(managed)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    # set feature 'sequential_thinking' to on
    monkeypatch.setattr("sys.argv", ["agent-cli", "--config", str(master), "mcp", "feature", "test_server", "sequential_thinking", "on"])
    cli.main()

    # reload managed file and assert persisted
    text = managed.read_text(encoding="utf-8")
    try:
        got = json.loads(text)
    except Exception:
        got = yaml.safe_load(text)

    assert got.get("mcp", {}).get("external_servers", {}).get("test_server", {}).get("features", {}).get("sequential_thinking") is True
