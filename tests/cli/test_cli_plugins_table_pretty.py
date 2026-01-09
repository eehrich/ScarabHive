from __future__ import annotations



from agent_system import agent_cli as cli


def test_cli_plugins_table_pretty(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "pretty"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "pretty"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    (plugin_dir / "plugin.yaml").write_text('description: "Pretty plugin"\nversion: "0.1"\n')

    from agent_system.config.models import AgentSystemConfig, PluginsConfig, LLMSystemConfig, LLMModelConfig
    cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=[str(pdir)])
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--format", "table"])    
    cli.main()
    out = capsys.readouterr().out
    assert "NAME" in out and "DESCRIPTION" in out and "ENABLED" in out
    assert "pretty" in out
