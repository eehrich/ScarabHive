import json
from pathlib import Path
import sys

from agent_system import cli


def test_plugins_info_single_name(monkeypatch, tmp_path, capsys):
    # Create plugins directory and example plugin
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "example"\nPLUGIN_FACTORY = lambda name, cfg, ssl_verify=True: None\n')

    from agent_system.config.models import AgentConfig, MCPConfig
    cfg = AgentConfig()
    cfg.mcp = MCPConfig(plugin_dirs=[str(pdir)])
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "example"]) 
    cli.main()
    out = capsys.readouterr().out
    # There should only be one "NAME:" line at top
    assert out.count("NAME:") == 1
    assert "DESCRIPTION:" in out or "VERSION:" in out
