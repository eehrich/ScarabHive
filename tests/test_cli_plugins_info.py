from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from agent_system import cli


def _make_cfg(pdir: Path):
    from agent_system.config.models import AgentConfig, MCPConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            default_model="test-model"
        )
    )
    cfg.mcp = MCPConfig(plugin_dirs=[str(pdir)])
    return cfg


def test_cli_plugins_info_json(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "info_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "info_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    (plugin_dir / "plugin.yaml").write_text('description: "Info plugin"\nversion: "1.2"\n')

    cfg = _make_cfg(pdir)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "info_example", "--format", "json"])
    cli.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["name"] == "info_example"
    assert out["metadata"]["description"] == "Info plugin"


def test_cli_plugins_info_table(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "info_example_table"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "info_example_table"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    (plugin_dir / "plugin.yaml").write_text('description: "Tabular plugin"\nversion: "9.9"\n')

    cfg = _make_cfg(pdir)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "info_example_table", "--format", "table"])
    cli.main()
    captured = capsys.readouterr()
    out = captured.out
    assert "NAME" in out and "DESCRIPTION" in out
    assert "info_example_table" in out
    assert "Tabular plugin" in out
