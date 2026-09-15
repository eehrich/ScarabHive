"""agent-cli subcommands that do not run an agent: hooks, users, and the
commands removed in the 2026-09 cleanup."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent_system import agent_cli as cli
from agent_system.cli_utils import users as users_cli
from agent_system.cli_utils.commands import hooks as hooks_cmd
from agent_system.config.models import AgentSystemConfig, AuthConfig


class _HookRegistry:
    """Holds hooks only once somebody registers them -- like the real one."""

    def __init__(self):
        self.hooks: dict = {}

    def list_hooks(self, hook_type=None):
        return {"pre_llm_call": list(self.hooks)}

    def get_hook_info(self, name):
        return self.hooks.get(name)


def test_hooks_list_loads_the_plugins_that_register_them(monkeypatch, capsys):
    """Without loading plugins the registry is empty: `hooks list` said
    "No hooks registered" on every installation."""
    registry = _HookRegistry()
    monkeypatch.setattr(hooks_cmd, "get_hook_registry", lambda: registry)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: AgentSystemConfig())

    integration = AsyncMock()

    async def initialize(config):
        registry.hooks["probe.on_pre_llm_call"] = {"name": "probe.on_pre_llm_call"}

    integration.initialize.side_effect = initialize
    monkeypatch.setattr(cli, "MCPIntegration", lambda config: integration)

    monkeypatch.setattr("sys.argv", ["agent-cli", "hooks", "list", "--format", "json"])
    cli.main()

    assert json.loads(capsys.readouterr().out) == [{"name": "probe.on_pre_llm_call"}]
    integration.shutdown.assert_awaited_once()


def test_users_failure_exits_non_zero_and_reads_the_given_config(monkeypatch, tmp_path, capsys):
    """The argparse copy swallowed typer's exit codes (every failure exited 0)
    and the user commands read the default config whatever --config said."""
    monkeypatch.setattr(users_cli, "CONFIG_PATH", None)  # restored after the test
    # setup_database() stores the database module-wide; put the old one back
    from agent_system.auth import database
    monkeypatch.setattr(database, "_db", database._db)
    seen = []
    cfg = AgentSystemConfig(auth=AuthConfig(database_path=str(tmp_path / "users.db")))
    monkeypatch.setattr(users_cli, "load_settings", lambda path=None: seen.append(path) or cfg)

    monkeypatch.setattr("sys.argv", ["agent-cli", "--config", "elsewhere.yaml", "users", "info", "nobody"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    assert seen == ["elsewhere.yaml"]


def test_users_update_and_create_validate_like_the_api_and_never_echo_the_password(monkeypatch, tmp_path, capsys):
    """`users update` assigned fields to an empty model, which skips validation: short passwords and bad emails
    were stored. Pydantic's own message carries the input, so a refused password must not be printed either."""
    monkeypatch.setattr(users_cli, "CONFIG_PATH", None)
    from agent_system.auth import database
    from agent_system.auth.security import verify_password
    monkeypatch.setattr(database, "_db", database._db)
    cfg = AgentSystemConfig(auth=AuthConfig(database_path=str(tmp_path / "users.db")))
    monkeypatch.setattr(users_cli, "load_settings", lambda path=None: cfg)

    def run(*args):
        capsys.readouterr()
        monkeypatch.setattr("sys.argv", ["agent-cli", "users", *args])
        try:
            cli.main()
            code = 0
        except SystemExit as exit_info:
            code = exit_info.code or 0
        out = capsys.readouterr()
        return code, out.out + out.err

    assert run("create", "bob", "bob@example.com", "-p", "correct-horse")[0] == 0
    code, said = run("create", "carol", "carol@example.com", "-p", "Short7x")
    assert code == 1 and "password" in said and "Short7x" not in said
    before = database.get_db().get_user_by_username("bob")
    refused = {
        "short": ("-p", "Short7x"),
        "long": ("-p", "€" * 25),
        "email": ("-e", "not-an-email"),
        "role": ("-r", "owner"),
    }
    for name, option in refused.items():
        code, said = run("update", "bob", *option)
        assert code == 1, name
        assert option[1] not in said or name in ("email", "role"), name
    assert "Invalid role: owner" in said
    assert "longer than 72 bytes" in run("update", "bob", "-p", "€" * 25)[1]
    after = database.get_db().get_user_by_username("bob")
    assert (after.email, after.hashed_password, after.role) == (before.email, before.hashed_password, before.role)
    assert database.get_db().get_user_by_username("carol") is None
    assert run("update", "bob", "-p", "a-new-secret", "-e", "bob2@example.com")[0] == 0
    assert verify_password("a-new-secret", database.get_db().get_user_by_username("bob").hashed_password)


@pytest.mark.parametrize("argv, handed_over", [
    # The preliminary parser reads option values too: `-vS3cret` is -v plus
    # -S3cret to it, and the password became "-S3cret".
    (["users", "update", "bob", "-p", "-vS3cret"], ["update", "bob", "-p", "-vS3cret"]),
    # ... and died on an abbreviation it could not resolve
    (["users", "update", "bob", "-p", "--no"], ["update", "bob", "-p", "--no"]),
    (["-v", "users", "list"], ["list"]),
    (["--config", "x.yaml", "users", "info", "bob"], ["info", "bob"]),
    (["users"], ["list"]),
    (["users", "--limit", "5"], ["list", "--limit", "5"]),
    (["users", "-h"], ["-h"]),
])
def test_users_gets_the_tokens_as_typed(monkeypatch, argv, handed_over):
    monkeypatch.setattr(users_cli, "CONFIG_PATH", None)
    received = []
    monkeypatch.setattr(users_cli, "app", lambda args, prog_name=None: received.append(args))
    monkeypatch.setattr("sys.argv", ["agent-cli", *argv])
    cli.main()
    assert received == [handed_over]


@pytest.mark.parametrize("argv, config_path", [
    (["--config", "c.yaml", "users", "list"], "c.yaml"),
    # A password that happens to abbreviate --config became the config path
    (["users", "create", "bob", "b@x.de", "-p", "--conf", "x.yaml"], None),
])
def test_users_config_comes_only_from_before_the_subcommand(monkeypatch, argv, config_path):
    monkeypatch.setattr(users_cli, "CONFIG_PATH", "untouched")
    seen = []
    monkeypatch.setattr(users_cli, "app", lambda args, prog_name=None: seen.append(users_cli.CONFIG_PATH))
    monkeypatch.setattr("sys.argv", ["agent-cli", *argv])
    cli.main()
    assert seen == [config_path]


def _plugins_loaded_with(monkeypatch, registry):
    monkeypatch.setattr(hooks_cmd, "get_hook_registry", lambda: registry)
    monkeypatch.setattr(cli, "load_settings", lambda path=None: AgentSystemConfig())
    monkeypatch.setattr(cli, "MCPIntegration", lambda config: AsyncMock())


def test_hooks_inspect_of_an_unknown_hook_exits_1(monkeypatch, capsys):
    _plugins_loaded_with(monkeypatch, _HookRegistry())
    monkeypatch.setattr("sys.argv", ["agent-cli", "hooks", "inspect", "nope.hook"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert "not found" in json.loads(capsys.readouterr().out)["error"]


def test_plugins_info_of_an_unknown_plugin_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: AgentSystemConfig())
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "info", "no_such_plugin_xyz"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "plugin not found"


def test_reload_without_a_server_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: AgentSystemConfig())
    # Port 9 (discard) is closed on a normal machine: the connect is refused at once
    monkeypatch.setattr("sys.argv", ["agent-cli", "reload", "--url", "http://127.0.0.1:9", "--timeout", "5"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "cannot connect to server"


@pytest.mark.parametrize("llm_params", [["temperatur=0.2"], ["no_equals_sign"]])
def test_bad_llm_params_are_refused_before_the_bootstrap(monkeypatch, capsys, llm_params):
    """They were checked after the full bootstrap and answered with exit 0."""
    def no_config_expected(path=None):
        raise AssertionError("config loaded -- the refusal came too late")

    monkeypatch.setattr(cli, "load_settings", no_config_expected)
    monkeypatch.setattr("sys.argv", ["agent-cli", "run", "task", "--llm-params", *llm_params])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert "--llm-params" in capsys.readouterr().err


def test_a_mistyped_llm_profile_is_refused_before_the_bootstrap(monkeypatch, capsys):
    """It was checked only after the whole plugin start."""
    from agent_system.config.models import LLMModelConfig, LLMProfile, LLMSystemConfig
    from agent_system.services import initialization_service

    cfg = AgentSystemConfig(llm_system=LLMSystemConfig(
        models={"m": LLMModelConfig(provider="openai", model="m")},
        profiles={"normal": LLMProfile(model_ref="m")}))
    monkeypatch.setattr(cli, "load_settings", lambda path=None: cfg)

    def no_bootstrap_expected(self):
        raise AssertionError("bootstrapped -- the refusal came too late")

    monkeypatch.setattr(initialization_service.InitializationService,
                        "initialize_for_cli", no_bootstrap_expected)
    monkeypatch.setattr("sys.argv", ["agent-cli", "run", "task", "--llm", "nromal"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "'nromal' not found" in err and "normal" in err


def test_users_short_help_is_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["agent-cli", "users", "-h"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "generate-api-key" in out
    # Offered by typer by default, but `list` is prepended to a leading
    # option, so the completion flags could only fail
    assert "--show-completion" not in out


def test_hooks_refuses_a_partial_list_when_plugins_fail_to_load(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path=None: AgentSystemConfig())
    integration = AsyncMock()
    integration.initialize.side_effect = RuntimeError("circular inheritance")
    monkeypatch.setattr(cli, "MCPIntegration", lambda config: integration)

    monkeypatch.setattr("sys.argv", ["agent-cli", "hooks", "list"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "circular inheritance" in captured.err
    assert "No hooks registered" not in captured.out
    integration.shutdown.assert_awaited_once()


def test_without_config_flag_the_loader_decides(monkeypatch, capsys):
    """load_settings(None) reads AGENT_CONFIG_PATH. The CLI used to pass
    config/config.yaml by default, which shadowed the variable."""
    seen = []
    monkeypatch.setattr(cli, "load_settings", lambda path=None: seen.append(path) or AgentSystemConfig())
    monkeypatch.setattr("sys.argv", ["agent-cli", "plugins", "list", "--format", "json"])
    cli.main()
    assert seen == [None]


@pytest.mark.parametrize("argv", [
    ["plugins", "enable", "x"],
    ["plugins", "status"],
    ["mcp", "connect", "x"],
    ["mcp", "tool", "x", "block", "y"],
    ["hooks", "stats"],
])
def test_removed_commands_are_refused_not_answered(monkeypatch, argv):
    """They used to print an error as JSON on stdout with exit code 0."""
    monkeypatch.setattr("sys.argv", ["agent-cli", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
