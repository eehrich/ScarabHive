from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from agent_system import cli
from agent_system.config.models import MCPConfig


def _make_cfg(tmp_path: Path, plugin_dirs):
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServersConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=plugin_dirs)
    )
    return cfg


def test_cli_plugins_enable_dry_run(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "adv_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "adv_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('')

    cfg = _make_cfg(tmp_path, [str(pdir)])
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "adv_example", "--config", str(cfg_file), "--dry-run"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    # Enable/disable commands have been removed - expect error message
    assert out.get("error") == "enable/disable commands removed"


def test_cli_plugins_enable_yes(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "adv_yes"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "adv_yes"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('')

    cfg = _make_cfg(tmp_path, [str(pdir)])
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    # provide --yes to skip interactive prompt
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "adv_yes", "--config", str(cfg_file), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    # Enable/disable commands have been removed - expect error message
    assert out.get("error") == "enable/disable commands removed"

def test_cli_plugins_status(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "st_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "st_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    cfg_file = tmp_path / "config.yaml"
    # Write a config with the plugin enabled in YAML format
    cfg_file.write_text('mcp:\n  enabled_servers:\n    - st_example')

    cfg = _make_cfg(tmp_path, [str(pdir)])
    cfg.plugins.servers = {"st_example": MCPConfig(type="st_example", enabled=True, agent_config=None)}  # ensure the plugin is enabled in the config
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "status", "--config", str(cfg_file)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    st = next(p for p in out if p["name"] == "st_example")
    assert st["enabled"] is True
