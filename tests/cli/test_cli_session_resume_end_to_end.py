"""The whole resume path through main(), against a session on disk.

The pieces are unit-tested next door, but the wiring in main() is where this
feature actually lives -- and where it broke twice: the stored values were
read into variables nobody used, and the SAVE at the end wrote the raw --llm
flag back, which put the agent's default over the session's own choice on
every bare resume. Both are invisible to a test that only calls the helpers.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

import agent_system.agent_cli as cli
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    MCPConfig,
    PluginsConfig,
)
from agent_system.services.session_manager import SessionManager, SessionPermissionError
from agent_system.services.session_service import SessionService

STORED_AGENT = "stored_agent"
STORED_PROFILE = "profile_stored"
AGENT_DEFAULT_PROFILE = "profile_agent_default"


class _DummyAgent:
    """Enough Agent for main() to reach the save."""

    def __init__(self, *args, **kwargs):
        self.agent_config = AgentConfig(system_prompt="x",
                                        llm_profile=AGENT_DEFAULT_PROFILE)
        self.registry = None
        self.llm = SimpleNamespace(model="m")

    async def run(self, task):
        return {"task": task, "summary": "done", "calls": []}

    async def run_events(self, task, **kwargs):
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A CLI whose config knows one agent and two profiles, on a temp store."""
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m")},
            profiles={
                STORED_PROFILE: LLMProfile(model_ref="m"),
                AGENT_DEFAULT_PROFILE: LLMProfile(model_ref="m"),
            },
        ))
    config.plugins = PluginsConfig(servers={
        STORED_AGENT: MCPConfig(type="agent", enabled=True,
                                agent_config=AgentConfig(
                                    system_prompt="x",
                                    llm_profile=AGENT_DEFAULT_PROFILE)),
        "config_default_agent": MCPConfig(type="agent", enabled=True,
                                          agent_config=AgentConfig(system_prompt="y")),
    })
    config.default_agent = "config_default_agent"
    monkeypatch.setattr(cli, "load_settings", lambda path=None: config)

    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    service = SessionService(manager)

    from agent_system.mcp.base import MCPRegistry
    from agent_system.services.initialization_service import InitializationService

    def fake_init(self):
        # session_manager is a lazy property over _session_manager; setting the
        # backing field is how the real initialize_for_cli fills it too.
        self._session_manager = manager
        return MCPRegistry(), service

    monkeypatch.setattr(InitializationService, "initialize_for_cli", fake_init)
    monkeypatch.setattr("agent_system.servers.agent.server.Agent", _DummyAgent)
    monkeypatch.setattr("agent_system.agent_cli.Agent", _DummyAgent)

    saved = {}

    async def fake_save(**kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(service, "save_session", fake_save)

    # The session on disk, exactly as a `--agent X --llm Y` run leaves it.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(manager.create_session(
            user_id="cli_user", session_id="s1",
            agent_name=STORED_AGENT, llm_profile=STORED_PROFILE))
    finally:
        loop.close()
    manager.clear_cache()

    return SimpleNamespace(config=config, saved=saved, manager=manager, service=service)


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    cli.main()


HOLDER = """
import sys, time
from agent_system.core.session_presence import SessionPresence
print(SessionPresence(sys.argv[1]).hold(sys.argv[2], sys.argv[3], "other_agent"), flush=True)
time.sleep(120)
"""


@pytest.fixture
def other_process():
    """A second process with the session in hand -- a woken run, or a chat."""
    processes = []

    def hold(root, session_id, user_id="cli_user"):
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(root), session_id, user_id],
            stdout=subprocess.PIPE, text=True)
        processes.append(process)
        assert process.stdout.readline().strip() == "True", "the other process could not hold it"
        return process

    yield hold
    for process in processes:
        process.kill()
        process.wait()


class TestBareResume:
    """`agent-cli "weiter" --session s1` with no --agent and no --llm."""

    def test_the_session_keeps_its_agent(self, cli_env, monkeypatch):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])
        assert cli_env.saved.get("agent_name") == STORED_AGENT, (
            "the run fell back to the config default agent")

    def test_the_record_is_not_overwritten_with_the_agents_default(
            self, cli_env, monkeypatch):
        # The bug this catches: the save read the raw --llm flag (absent here)
        # and wrote the agent's default over the session's own profile, so the
        # choice survived exactly one resume.
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])
        assert cli_env.saved.get("llm_profile") == STORED_PROFILE

    def test_an_agent_the_config_no_longer_defines_is_ignored(self, cli_env,
                                                              monkeypatch):
        # 707 of 2914 cli_user sessions name an agent that no longer exists.
        # Handing that name on reaches a gate that can only abort -- the run
        # would die over a name the user never typed.
        del cli_env.config.plugins.servers[STORED_AGENT]

        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])

        assert cli_env.saved.get("agent_name") == "config_default_agent"

    def test_an_explicit_agent_still_wins(self, cli_env, monkeypatch):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1",
                           "--agent", "config_default_agent"])
        assert cli_env.saved.get("agent_name") == "config_default_agent"
        # And the stored profile is NOT forced onto the other agent.
        assert cli_env.saved.get("llm_profile") == AGENT_DEFAULT_PROFILE


class TestSessionPresence:
    """core/session_presence.py through the real main()."""

    def test_the_wake_command_continues_the_session_of_its_user_on_its_agent_and_profile(
            self, cli_env, monkeypatch):
        from agent_system.core.session_presence import wake_command

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cli_env.manager.create_session(
                user_id="other_user", session_id="s9",
                agent_name=STORED_AGENT, llm_profile=STORED_PROFILE))
        finally:
            loop.close()
        cli_env.manager.clear_cache()

        command = wake_command("s9", "other_user")
        _run(monkeypatch, ["agent-cli", *command[command.index("--raw"):]])

        assert cli_env.saved.get("session_id") == "s9"
        assert cli_env.saved.get("user_id") == "other_user"
        assert cli_env.saved.get("agent_name") == STORED_AGENT
        assert cli_env.saved.get("llm_profile") == STORED_PROFILE

    def test_a_run_holds_its_session_through_the_save_after_it(self, cli_env, monkeypatch, tmp_path):
        from agent_system.config.models import SessionPresenceConfig
        from agent_system.core.session_presence import presence_for

        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
        cli_env.config.session_presence = SessionPresenceConfig(enabled=True)
        presence = presence_for(cli_env.config)
        at_save = []

        async def save(**kwargs):
            at_save.append(presence.get("s1", "cli_user")["status"])
            return True

        monkeypatch.setattr(cli_env.service, "save_session", save)
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])

        assert at_save == ["running"], "the session was let go before the save after the run"
        assert presence.get("s1", "cli_user")["status"] == "idle"

    def _presence_on(self, cli_env, monkeypatch, tmp_path):
        """Presence on, over the store this CLI writes its sessions to."""
        from agent_system.config.models import SessionPresenceConfig

        sessions = tmp_path / "sessions"
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(sessions))
        cli_env.config.session_presence = SessionPresenceConfig(enabled=True)
        return sessions

    def test_the_session_is_held_before_it_is_loaded(self, cli_env, monkeypatch, tmp_path):
        # The other way round the copy in memory can already be one run behind
        # when it is written back: whoever holds it may be saving right now.
        from agent_system.core.session_presence import presence_for

        self._presence_on(cli_env, monkeypatch, tmp_path)
        presence = presence_for(cli_env.config)
        at_load = []
        load = cli_env.service.load_and_restore_session

        async def recording_load(agent, user_id, session_id):
            at_load.append((presence.get(session_id, user_id) or {}).get("status"))
            return await load(agent, user_id, session_id)

        monkeypatch.setattr(cli_env.service, "load_and_restore_session", recording_load)
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])

        assert at_load == ["running"], "the session was loaded before it was held"

    def test_a_session_another_process_runs_is_refused(
            self, cli_env, monkeypatch, tmp_path, other_process, capsys):
        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        other_process(sessions, "s1")

        with pytest.raises(SystemExit) as refused:
            _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])

        assert refused.value.code == 1
        assert "another process" in capsys.readouterr().err
        assert cli_env.saved == {}, "it ran the session anyway"

    @pytest.mark.parametrize("subcommand", ["run", "chat"])
    @pytest.mark.parametrize("failure, said", [
        (SessionPermissionError("owned by u2"), "belongs to a different user"),
        (OSError("disk gone"), "Error loading session"),
    ])
    def test_a_session_that_cannot_be_loaded_exits_1_and_lets_go(
            self, cli_env, monkeypatch, tmp_path, capsys, failure, said, subcommand):
        # It used to return with 0: a caller checking the exit code read a
        # run that never started as a successful one with empty output. And
        # chat kept the hold: it is handed over only once the REPL runs.
        from agent_system.core.session_presence import presence_for

        self._presence_on(cli_env, monkeypatch, tmp_path)
        shut = self._records_the_shutdown(monkeypatch)

        async def failing_load(agent, user_id, session_id):
            raise failure

        monkeypatch.setattr(cli_env.service, "load_and_restore_session", failing_load)

        with pytest.raises(SystemExit) as failed:
            _run(monkeypatch, ["agent-cli", "--raw", subcommand, "weiter", "--session", "s1"])

        assert failed.value.code == 1
        assert said in capsys.readouterr().err
        assert cli_env.saved == {}, "it ran the session anyway"
        assert shut == ["batch", "mcp"]
        assert presence_for(cli_env.config).get("s1", "cli_user")["status"] == "idle"

    def test_force_runs_it_anyway(self, cli_env, monkeypatch, tmp_path, other_process):
        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        other_process(sessions, "s1")

        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1", "--force"])

        assert cli_env.saved.get("session_id") == "s1"

    def _wake(self, monkeypatch, session_id="s1", user_id="cli_user"):
        from agent_system.core.session_presence import wake_command

        command = wake_command(session_id, user_id)
        _run(monkeypatch, ["agent-cli", *command[command.index("--raw"):]])

    def test_a_woken_run_steps_aside_for_the_process_that_has_the_session(
            self, cli_env, monkeypatch, tmp_path, other_process, capsys):
        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        other_process(sessions, "s1")

        self._wake(monkeypatch)  # no exit code: nobody typed this run

        assert cli_env.saved == {}
        assert "Error" not in capsys.readouterr().err

    @staticmethod
    def _records_the_shutdown(monkeypatch):
        """What main() shuts down before it returns. MCP is up from the
        bootstrap on: a return without this leaks the stdio child process of
        every server until the interpreter exits, and stepping aside is the
        normal end of a woken run, not a rare one."""
        shut = []

        async def batch():
            shut.append("batch")

        async def mcp():
            shut.append("mcp")

        monkeypatch.setattr(cli, "shutdown_batch_system", batch)
        monkeypatch.setattr(cli, "shutdown_mcp", mcp)
        return shut

    def test_stepping_aside_for_the_holder_still_shuts_the_runtime_down(
            self, cli_env, monkeypatch, tmp_path, other_process):
        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        other_process(sessions, "s1")
        shut = self._records_the_shutdown(monkeypatch)

        self._wake(monkeypatch)

        assert shut == ["batch", "mcp"]

    def test_a_woken_run_without_input_still_shuts_the_runtime_down(
            self, cli_env, monkeypatch, tmp_path):
        self._presence_on(cli_env, monkeypatch, tmp_path)
        shut = self._records_the_shutdown(monkeypatch)

        self._wake(monkeypatch)  # no marker: a run in between handed it over

        assert shut == ["batch", "mcp"]

    def test_a_woken_run_whose_input_was_taken_does_not_start(
            self, cli_env, monkeypatch, tmp_path):
        self._presence_on(cli_env, monkeypatch, tmp_path)

        self._wake(monkeypatch)  # no marker: a run in between handed it over

        assert cli_env.saved == {}

    def test_a_woken_run_with_input_waiting_continues_the_session(
            self, cli_env, monkeypatch, tmp_path):
        from agent_system.core import session_presence as sp

        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        (sessions / "cli_user").mkdir(parents=True, exist_ok=True)
        (sessions / "cli_user" / "s1.pending").touch()
        # The dummy agent never reaches an LLM call, so the marker is still
        # there when this run lets go -- and letting go with input waiting wakes
        # the session. A test that means to wake replaces spawn_wake itself;
        # the suite-wide guard would otherwise start a real agent-cli.
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: (0, 0.0))

        self._wake(monkeypatch)

        assert cli_env.saved.get("session_id") == "s1"


class TestChatStart:
    """What `agent-cli chat` hands to the REPL."""

    def test_attachments_title_and_params_reach_the_chat(
            self, cli_env, monkeypatch, tmp_path):
        """`chat --attach` was rejected by the parser, and --session-title and
        --llm-params were accepted and then dropped."""
        import agent_system.cli_utils.chat as chat

        note = tmp_path / "notes.txt"
        note.write_text("inhalt", encoding="utf-8")
        seen = {}
        monkeypatch.setattr(chat, "run_chat_loop", lambda **kwargs: seen.update(kwargs))
        monkeypatch.setattr(
            "agent_system.llm.factory.create_llm_from_profile",
            lambda config, llm_profile, llm_params=None: object())

        _run(monkeypatch, ["agent-cli", "chat", "schau mal", "--attach", str(note),
                           "--session-title", "Mein Titel",
                           "--llm-params", "thinking_level=max"])

        assert seen["attachments"] == [str(note)]
        assert seen["session_title"] == "Mein Titel"
        assert seen["llm_params"] == {"thinking_level": "max"}
        assert seen["initial_task"] == "schau mal"

    def test_attachments_without_a_first_message_wait_for_it(
            self, cli_env, monkeypatch, tmp_path):
        """No message to put them on yet: they are the chat's to send."""
        import agent_system.cli_utils.chat as chat

        picture = tmp_path / "bild.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        seen = {}
        monkeypatch.setattr(chat, "run_chat_loop", lambda **kwargs: seen.update(kwargs))

        _run(monkeypatch, ["agent-cli", "chat", "--attach", str(picture)])

        assert seen["attachments"] == [str(picture)]
        assert seen["initial_task"] is None


    def test_a_turn_still_holding_the_session_keeps_it_past_the_chat(
            self, cli_env, monkeypatch, tmp_path):
        """A turn takes its own hold for its request; abandoned by a third
        Ctrl-C it never gives it back. The chat lets go of ITS hold, and a
        second release by the CLI dropped the turn's -- waking a session
        whose run is still unwinding."""
        import agent_system.cli_utils.chat as chat
        from agent_system.config.models import SessionPresenceConfig
        from agent_system.core.session_presence import presence_for

        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
        cli_env.config.session_presence = SessionPresenceConfig(enabled=True)
        presence = presence_for(cli_env.config)

        def chat_with_an_abandoned_turn(**kwargs):
            session_id, user = kwargs["session_id"], kwargs["session_user"]
            presence.hold(session_id, user, "x")   # the turn's own hold
            presence.release(session_id, user)     # the REPL lets go of its own

        monkeypatch.setattr(chat, "run_chat_loop", chat_with_an_abandoned_turn)
        _run(monkeypatch, ["agent-cli", "chat", "--session", "s1"])

        assert presence.get("s1", "cli_user")["status"] == "running", \
            "the CLI released the hold the abandoned turn still has"
        presence.release("s1", "cli_user")


class TestListSessions:
    """`--list-sessions [COUNT]` through main(), against the sessions on disk.

    It used to print four lines plus a blank per session over the merged index
    -- for cli_user that is 2915 top-level sessions and 31086 sub-sessions.
    """

    @staticmethod
    def _seed_more(manager, count):
        loop = asyncio.new_event_loop()
        try:
            for i in range(count):
                loop.run_until_complete(manager.create_session(
                    user_id="cli_user", session_id=f"extra{i}",
                    agent_name=STORED_AGENT, llm_profile=STORED_PROFILE))
        finally:
            loop.close()
        manager.clear_cache()

    def test_one_line_per_session_and_the_agent_never_runs(
            self, cli_env, monkeypatch, capsys):
        self._seed_more(cli_env.manager, 2)
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions"])

        out = capsys.readouterr().out
        assert "Sessions for 'cli_user' (3 of 3):" in out
        assert len([l for l in out.splitlines() if l.startswith("  ")]) == 3, out
        assert not cli_env.saved, "the run continued past the listing"

    def test_a_profile_that_cannot_be_built_does_not_hide_the_listing(
            self, cli_env, monkeypatch, capsys):
        # The LLM override is built before the attachments; the listing needs
        # neither and must not stop on a profile that fails.
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions",
                           "--llm", "no_such_profile"])

        captured = capsys.readouterr()
        assert "Sessions for 'cli_user'" in captured.out, captured.err
        assert not cli_env.saved, "the run continued past the listing"

    def test_a_count_caps_the_listing(self, cli_env, monkeypatch, capsys):
        self._seed_more(cli_env.manager, 2)
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions", "1"])

        out = capsys.readouterr().out
        assert "Sessions for 'cli_user' (1 of 3):" in out
        assert "... 2 more" in out

    def test_zero_is_a_count_not_an_off_switch(self, cli_env, monkeypatch, capsys):
        # The flag carries a number now, so every truthiness check on it is a
        # trap: `--list-sessions 0` means all of them, not "no listing".
        self._seed_more(cli_env.manager, 2)
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions", "0"])

        out = capsys.readouterr().out
        assert "Sessions for 'cli_user' (3 of 3):" in out
        assert not cli_env.saved, "0 was read as 'no listing' and the run went on"

    def test_a_task_after_the_flag_still_lists(self, cli_env, monkeypatch, capsys):
        # argparse binds the next token to the optional BEFORE converting it,
        # so with type=int this exited 2 on int("weiter") -- where the
        # store_true version printed the listing and ignored the task.
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions", "weiter"])

        out = capsys.readouterr().out
        assert "Ignoring 'weiter': --list-sessions takes a count." in out
        assert "Sessions for 'cli_user' (1 of 1):" in out
        assert not cli_env.saved, "the task ran anyway"

    def test_the_session_being_continued_is_marked_and_the_footer_is_there(
            self, cli_env, monkeypatch, capsys):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions",
                           "--session", "s1"])

        out = capsys.readouterr().out
        marked = [l for l in out.splitlines() if l.startswith(" *")]
        assert len(marked) == 1 and " s1 " in marked[0], out
        assert "Continue one with: --session <id>" in out
