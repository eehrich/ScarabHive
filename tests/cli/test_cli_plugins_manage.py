from __future__ import annotations

import json
from pathlib import Path


from agent_system import agent_cli as cli


def _write_cfg(tmp_path: Path, data: dict):
    p = tmp_path / "config.yaml"
    p.write_text(json.dumps(data))
    return p


def test_cli_plugins_enable_disable(monkeypatch, tmp_path, capsys):
    # Prepare empty config file
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")

    # Create plugins dir
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "pm_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "pm_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')

    # Monkeypatch load_settings to return a config with plugin_dirs pointing to our pdir
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            profiles={}
        ),
        plugins=PluginsConfig(
            plugin_dirs=[str(pdir)],
            servers={}
        )
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    # Enable the plugin
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "pm_example", "--config", str(cfg_file)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    # Enable/disable commands have been removed - expect error message
    assert out.get("error") == "enable/disable commands removed"

    # Disable the plugin
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "disable", "pm_example", "--config", str(cfg_file)])
    cli.main()
    out2 = json.loads(capsys.readouterr().out)
    # Enable/disable commands have been removed - expect error message
    assert out2.get("error") == "enable/disable commands removed"


def test_cli_plugins_search(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "search_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "search_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    (plugin_dir / "plugin.yaml").write_text('description: "Searchable plugin"\nversion: "0.0"\n')

    from agent_system.config.models import AgentSystemConfig, PluginsConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            profiles={}
        ),
        plugins=PluginsConfig(
            plugin_dirs=[str(pdir)],
            servers={}
        )
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "search", "searchable"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert any(p["name"] == "search_example" for p in out)
