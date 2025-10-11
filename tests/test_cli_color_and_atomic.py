import json
import tempfile
from pathlib import Path
import json
import tempfile
from pathlib import Path
import sys

from agent_system import cli


def test_no_color_and_always_color(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--no-color"]) 
    cli.main()
    out = capsys.readouterr().out
    assert "YES" in out or "NO" in out

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--color", "always"]) 
    cli.main()
    out2 = capsys.readouterr().out
    assert "YES" in out2 or "NO" in out2
def test_enable_atomic_write(tmp_path, monkeypatch, capsys):
    # Create a master manifest that includes a separate mcp.yaml (managed file)
    cfg = tmp_path / "config.yaml"
    managed = tmp_path / "mcp.yaml"
    managed.write_text('{"mcp": {"enabled_servers": []}}')
    cfg.write_text('{"includes": ["mcp.yaml"]}')
    # monkeypatch load_settings to minimal config with no plugin_dirs (we won't discover plugins here)
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPServersConfig, LLMSystemConfig, LLMModelConfig
    cfg_model = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")}
        ),
        plugins=PluginsConfig(plugin_dirs=[])
    )
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg_model)

    # run enable action with --yes to skip prompt
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "enable", "example", "--config", str(cfg), "--yes"]) 
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out.get("result") == "ok"
    import yaml
    data = yaml.safe_load(managed.read_text())
    assert "example" in data.get("mcp", {}).get("enabled_servers", [])
    # No backup rotation files should be created for YAML-managed files
    bak = managed.with_suffix(".yaml.bak")
    assert not bak.exists()
