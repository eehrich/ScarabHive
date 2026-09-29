from __future__ import annotations

import json

from agent_system import agent_cli as cli


def test_cli_plugins_search(monkeypatch, tmp_path, capsys):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "search_example"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text('PLUGIN_NAME = "search_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    (plugin_dir / "plugin.toml").write_text('[plugin]\ndescription = "Searchable plugin"\nversion = "0.0"\n')

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
