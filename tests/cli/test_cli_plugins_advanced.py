from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_system import agent_cli as cli
from agent_system.config.models import MCPConfig


def _make_cfg(tmp_path: Path, plugin_dirs):
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=plugin_dirs)
    )
    return cfg


def _plugin_dir(tmp_path: Path, name: str) -> Path:
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(f'PLUGIN_NAME = "{name}"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    return pdir


def test_cli_plugins_list_json_reports_enabled(monkeypatch, tmp_path, capsys):
    pdir = _plugin_dir(tmp_path, "st_example")
    cfg = _make_cfg(tmp_path, [str(pdir)])
    cfg.plugins.servers = {"st_example": MCPConfig(type="st_example", enabled=True, agent_config=None)}
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--format", "json"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    st = next(p for p in out if p["name"] == "st_example")
    assert st["enabled"] is True


@pytest.mark.parametrize("instance_enabled", [True, False])
def test_cli_plugins_info_enabled_follows_instances_of_the_type(monkeypatch, tmp_path, capsys, instance_enabled):
    """The only instance carries a name of its own -- as writer_audio_ops is
    an audio_ops. Matching the TYPE against instance names said NO for it."""
    pdir = _plugin_dir(tmp_path, "st_example")
    cfg = _make_cfg(tmp_path, [str(pdir)])
    cfg.plugins.servers = {"renamed_instance": MCPConfig(type="st_example", enabled=instance_enabled, agent_config=None)}
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "st_example", "--format", "json"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["enabled"] is instance_enabled


def test_cli_plugins_follow_a_type_that_names_another_server(monkeypatch, tmp_path, capsys):
    """`child: {type: base}` is built from base's plugin. Read raw, the child
    belonged to no plugin at all and st_example looked switched off."""
    pdir = _plugin_dir(tmp_path, "st_example")
    cfg = _make_cfg(tmp_path, [str(pdir)])
    cfg.plugins.servers = {
        "base": MCPConfig(type="st_example", enabled=False, agent_config=None),
        "child": MCPConfig(type="base", enabled=True, agent_config=None),
    }
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "st_example", "--format", "json"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["enabled"] is True

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--format", "json"])
    cli.main()
    st = next(p for p in json.loads(capsys.readouterr().out) if p["name"] == "st_example")
    assert {i["instance_name"] for i in st["instances"]} == {"base", "child"}
