import os
import json
import tempfile
from pathlib import Path
import builtins
import pytest

from agent_system import agent_cli as cli


def _make_cfg_file(tmp_path: Path):
    cfg = tmp_path / "agent.yaml"
    cfg.write_text('{"mcp": {"enabled_servers": []}}')
    return cfg


def test_atomic_write_replace_failure(monkeypatch, tmp_path, capsys):
    cfg = _make_cfg_file(tmp_path)

    # monkeypatch load_settings so plugin discovery doesn't affect test
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServersConfig, LLMSystemConfig, LLMModelConfig
    cfg_model = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=[])
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg_model)

    # simulate os.replace failing when attempting to move temp -> target
    def fake_replace(src, dst):
        raise PermissionError("replace failed")

    monkeypatch.setattr(os, "replace", fake_replace)

    # run enable with --yes to skip prompt
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "x", "--config", str(cfg), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    # On failure the CLI should return an error dict
    assert out.get("error") is not None


def test_atomic_write_tmp_write_failure(monkeypatch, tmp_path, capsys):
    cfg = _make_cfg_file(tmp_path)

    # monkeypatch load_settings
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServersConfig, LLMSystemConfig, LLMModelConfig
    cfg_model = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=[])
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg_model)

    # Simulate NamedTemporaryFile raising on creation
    class FakeNTF:
        def __init__(self, *a, **k):
            raise OSError("disk full")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", FakeNTF)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "y", "--config", str(cfg), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out.get("error") is not None
