import json
from pathlib import Path
import sys

from agent_system import cli


def test_enable_writes_managed_file(monkeypatch, tmp_path, capsys):
    # create master config and plugins dir
    master = tmp_path / "agent.yaml"
    master.write_text('{"mcp": {"plugin_dirs": ["plugins"], "managed_file": "managed.yaml"}}')

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "m1"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "m1"\nPLUGIN_FACTORY = lambda name, cfg, ssl_verify=True: None\n')

    # managed file does not exist initially
    managed = tmp_path / "managed.yaml"
    assert not managed.exists()

    from agent_system.config.models import AgentConfig, MCPConfig
    cfg = AgentConfig()
    cfg.mcp = MCPConfig(plugin_dirs=[str(pdir)])
    # load_settings should return config but the CLI will read master to find managed_file
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "m1", "--config", str(master), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out.get("result") == "ok"
    assert managed.exists()
    text = managed.read_text()
    try:
        data = json.loads(text)
    except Exception:
        import yaml
        data = yaml.safe_load(text)
    assert "m1" in data.get("mcp", {}).get("enabled_servers", [])
