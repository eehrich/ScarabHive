from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from agent_system import cli


def _make_cfg(tmp_path: Path, plugin_dirs):
    from agent_system.config.models import AgentConfig, MCPConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            default_model="test-model"
        )
    )
    cfg.mcp = MCPConfig(plugin_dirs=plugin_dirs)
    return cfg


def test_cli_plugins_enable_dry_run(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "adv_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "adv_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text('')

    cfg = _make_cfg(tmp_path, [str(pdir)])
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "adv_example", "--config", str(cfg_file), "--dry-run"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert "adv_example" in out["preview_enabled"]


def test_cli_plugins_enable_yes(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "adv_yes"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "adv_yes"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    cfg_file = tmp_path / "agent.yaml"
    cfg_file.write_text('')

    cfg = _make_cfg(tmp_path, [str(pdir)])
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    # provide --yes to skip interactive prompt
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "adv_yes", "--config", str(cfg_file), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == "ok"
    assert "adv_yes" in out["enabled"]

def test_cli_plugins_status(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "st_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "st_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    cfg_file = tmp_path / "agent.yaml"
    # Write a config with the plugin enabled in YAML format
    cfg_file.write_text('mcp:\n  enabled_servers:\n    - st_example')

    cfg = _make_cfg(tmp_path, [str(pdir)])
    cfg.mcp.enabled_servers = ["st_example"]  # ensure the plugin is enabled in the config
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "status", "--config", str(cfg_file)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    st = next(p for p in out if p["name"] == "st_example")
    assert st["enabled"] is True
