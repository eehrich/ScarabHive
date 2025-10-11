from __future__ import annotations

import json

from agent_system import cli


def test_cli_plugins_list(monkeypatch, tmp_path, capsys):
    # Create a temporary plugins directory with an example plugin
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    plugin_dir = pdir / "example"
    plugin_dir.mkdir()
    plugin_file = plugin_dir / "plugin.py"
    plugin_file.write_text(
        'PLUGIN_NAME = "cli_test_example"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n'
    )
    meta_file = plugin_dir / "plugin.yaml"
    meta_file.write_text('description: "Example plugin"\nversion: "0.1"\n')

    # Use new AgentSystemConfig structure
    from agent_system.config.models import (
        AgentSystemConfig, 
        LLMSystemConfig, 
        LLMModelConfig,
        PluginsConfig
    )

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

    # Run the CLI main with 'plugins list' behavior
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--format", "json"])
    # Call main and capture output
    cli.main()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert isinstance(out, list)
    assert any(p["name"] == "cli_test_example" for p in out)
    # Confirm metadata present
    ex = next(p for p in out if p["name"] == "cli_test_example")
    assert ex["description"] == "Example plugin"
    assert ex["version"] == "0.1"
