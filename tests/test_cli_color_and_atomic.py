import json
import tempfile
from pathlib import Path
import json
import tempfile
from pathlib import Path
import sys

from agent_system import cli


def test_no_color_and_always_color(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--no-color"]) 
    cli.main()
    out = capsys.readouterr().out
    assert "YES" in out or "NO" in out

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--color", "always"]) 
    cli.main()
    out2 = capsys.readouterr().out
    assert "YES" in out2 or "NO" in out2
def test_enable_atomic_write(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "agent.yaml"
    cfg.write_text('{"mcp": {"enabled_servers": []}}')
    # monkeypatch load_settings to minimal config with no plugin_dirs (we won't discover plugins here)
    from agent_system.config.models import AgentConfig, MCPConfig
    cfg_model = AgentConfig()
    cfg_model.mcp = MCPConfig(plugin_dirs=[])
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg_model)

    # run enable action with --yes to skip prompt
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "example", "--config", str(cfg), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out.get("result") == "ok"
    import yaml
    data = yaml.safe_load(cfg.read_text())
    assert "example" in data.get("mcp", {}).get("enabled_servers", [])
    # backup file should exist
    bak = cfg.with_suffix(".yaml.bak")
    assert bak.exists()
