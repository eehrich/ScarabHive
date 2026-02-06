import json

from agent_system import agent_cli as cli


def test_enable_writes_managed_file(monkeypatch, tmp_path, capsys):
    # create master config and plugins dir
    master = tmp_path / "config.yaml"
    master.write_text('{"mcp": {"plugin_dirs": ["plugins"], "managed_file": "managed.yaml"}}')

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "m1"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "m1"\nPLUGIN_FACTORY = lambda name, cfg, ssl_verify=True: None\n')

    # managed file does not exist initially
    managed = tmp_path / "managed.yaml"
    assert not managed.exists()

    from agent_system.config.models import AgentSystemConfig, PluginsConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=[str(pdir)])
    )
    # load_settings should return config but the CLI will read master to find managed_file
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "managed_example", "--config", str(master), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    # Enable/disable commands have been removed - expect error message
    assert out.get("error") == "enable/disable commands removed"
