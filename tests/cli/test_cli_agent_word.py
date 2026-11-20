"""``agent-cli chat AGENT [TASK]`` -- the leading word names the agent.

`chat some_agent` used to send "some_agent" as the first message to the
DEFAULT agent: the only positional was the task. Now a word the config knows
as an agent is read as --agent; every other word stays the task, so
`chat "a question"` works as before. Two words: the first must be an agent.

Driven through agent_cli._main with the config and the command stood in: the
word can only be told apart where the config is loaded.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import agent_system.agent_cli as agent_cli


def _config(*agents: str) -> SimpleNamespace:
    servers = {name: SimpleNamespace(agent_config={"name": name}) for name in agents}
    # A tool server: no agent_config in its raw entry, so no agent.
    servers["file_tool"] = SimpleNamespace(agent_config=None)
    return SimpleNamespace(plugins=SimpleNamespace(servers=servers), llm_system=None)


@pytest.fixture
def cli(monkeypatch):
    """Run agent-cli's line reading; returns the args the command would get."""
    seen: dict = {}
    monkeypatch.setattr(agent_cli, "enter_project", lambda: None)
    monkeypatch.setattr(agent_cli, "load_settings", lambda _path: _config("helper", "writer_bot"))
    monkeypatch.setattr(agent_cli, "run_agent_command",
                        lambda args, config, params, vprint: seen.setdefault("args", args))

    def run(*argv: str):
        monkeypatch.setattr(sys, "argv", ["agent-cli", *argv])
        agent_cli._main()
        return seen.pop("args")
    return run


class TestOneWord:
    def test_an_agent_name_is_the_agent(self, cli):
        args = cli("chat", "helper")

        assert args.agent_override == "helper"
        assert args.task is None, "the agent's name went out as a first message"

    def test_any_other_word_is_the_first_message(self, cli):
        args = cli("chat", "hello there")

        assert args.agent_override is None
        assert args.task == "hello there"

    def test_a_tool_server_is_no_agent(self, cli):
        args = cli("chat", "file_tool")

        assert args.agent_override is None
        assert args.task == "file_tool"

    def test_run_keeps_its_default_task(self, cli):
        args = cli("run", "helper")

        assert args.agent_override == "helper"
        assert args.task == "What can you do?"

    def test_with_agent_given_the_word_is_the_task(self, cli):
        """--agent was said out loud; the word is then what to ask."""
        args = cli("chat", "--agent", "writer_bot", "helper")

        assert args.agent_override == "writer_bot"
        assert args.task == "helper"


class TestTwoWords:
    def test_agent_then_message(self, cli):
        args = cli("chat", "helper", "fix the test")

        assert (args.agent_override, args.task) == ("helper", "fix the test")

    def test_options_between_them(self, cli):
        args = cli("chat", "helper", "--session-user", "u1", "fix the test")

        assert (args.agent_override, args.task, args.session_user) == ("helper", "fix the test", "u1")

    def test_a_first_word_that_is_no_agent_is_refused(self, cli, capsys):
        """A typo in the agent's name must not become a message to the default agent."""
        with pytest.raises(SystemExit) as stop:
            cli("chat", "helpr", "fix the test")

        assert stop.value.code == 2
        assert "'helpr' is no agent" in capsys.readouterr().err

    def test_agent_flag_and_two_words_is_refused(self, cli, capsys):
        with pytest.raises(SystemExit):
            cli("chat", "--agent", "helper", "write", "a poem")

        assert "in quotes" in capsys.readouterr().err


def test_options_first_without_a_command_mean_run(cli):
    """`run` may be left out -- also when the line starts with one of its options."""
    args = cli("-s", "ab12", "do it")

    assert (args.subcommand, args.session_id, args.task) == ("run", "ab12", "do it")


def test_help_after_runs_options_is_runs_help(cli, capsys):
    with pytest.raises(SystemExit):
        cli("-s", "ab12", "-h")

    usage = capsys.readouterr().out.splitlines()[0]
    assert usage.startswith("usage:") and " run [-h]" in usage, usage


def test_a_command_named_after_runs_options_stays_the_command(cli):
    """`-s ID chat` is chat: the word is a command, not a task for run."""
    with pytest.raises(SystemExit) as stop:
        cli("-s", "ab12", "chat")

    assert stop.value.code == 2, "it ran 'chat' as run's task"


def test_s_is_short_for_session(cli):
    args = cli("chat", "helper", "-s", "ab12")

    assert args.session_id == "ab12"
    assert args.agent_override == "helper"
