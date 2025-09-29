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


def test_cli_plugins_info_raw_json(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "raw_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "raw_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    (plugin_dir / "plugin.yaml").write_text('description: "Raw plugin"\nversion: "0.0"\n')

    cfg = _make_cfg(pdir)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "raw_example", "--raw", "--format", "json"])
    cli.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["name"] == "raw_example"
    assert "factory_repr" in out and "factory_module" in out
