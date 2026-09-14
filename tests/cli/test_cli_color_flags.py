import pytest

from agent_system import agent_cli as cli
from agent_system.cli_utils import common
from agent_system.config.models import AgentSystemConfig, MCPConfig, PluginsConfig


@pytest.mark.parametrize("no_color, mode, expected", [
    ("1", "auto", "text"),     # NO_COLOR silences the automatic choice ...
    ("1", "always", "ansi"),   # ... but not an explicit --color
    ("", "auto", "ansi"),
])
def test_no_color_env_on_a_terminal_that_renders_ansi(monkeypatch, no_color, mode, expected):
    """Only `users` read NO_COLOR; the rest of agent-cli coloured regardless."""
    monkeypatch.setattr(common, "ansi_capable_stdout", lambda: True)
    monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(common, "color_mode", mode)
    assert common.get_output_format() == expected


@pytest.mark.parametrize("flags, wants_ansi", [
    (["--no-color"], False),
    (["--color", "never"], False),
    (["--color", "always"], True),
])
def test_color_flags_decide_the_escape_sequences(monkeypatch, tmp_path, capsys, flags, wants_ansi):
    pdir = tmp_path / "plugins" / "color_probe"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text('PLUGIN_NAME = "color_probe"\nPLUGIN_FACTORY = lambda name, config, ssl_verify=True: None\n')
    cfg = AgentSystemConfig(plugins=PluginsConfig(
        plugin_dirs=[str(tmp_path / "plugins")],
        servers={"color_probe": MCPConfig(type="color_probe", enabled=True)}))
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", *flags])
    cli.main()
    out = capsys.readouterr().out

    assert "color_probe" in out and "YES" in out
    assert ("\x1b[" in out) is wants_ansi
