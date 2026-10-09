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
    ToolServerConfig,
    PluginsConfig,
)
from agent_system.services.session_manager import SessionManager, SessionPermissionError
from agent_system.services.session_service import SessionService
from agent_stand_in import stand_in_for_agent

STORED_AGENT = "stored_agent"
STORED_PROFILE = "profile_stored"
AGENT_DEFAULT_PROFILE = "profile_agent_default"


class _DummyAgent:
    """Enough Agent for main() to reach the save."""

    def __init__(self, name=STORED_AGENT, *args, **kwargs):
        self.name = name
        self.agent_config = AgentConfig(system_prompt="x",
                                        llm_profile=AGENT_DEFAULT_PROFILE)
        self.registry = None
        self.llm = SimpleNamespace(model="m")
        from agent_system.servers.agent.components.session_tracking import SessionTracker
        self._session_tracker = SessionTracker()

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
        STORED_AGENT: ToolServerConfig(type="agent", enabled=True,
                                agent_config=AgentConfig(
                                    system_prompt="x",
                                    llm_profile=AGENT_DEFAULT_PROFILE)),
        "config_default_agent": ToolServerConfig(type="agent", enabled=True,
                                          agent_config=AgentConfig(system_prompt="y")),
    })
    config.default_agent = "config_default_agent"
    monkeypatch.setattr(cli, "load_settings", lambda path=None: config)

    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    service = SessionService(manager)

    from agent_system.tools.base import ToolServerRegistry
    from agent_system.services.initialization_service import InitializationService

    def fake_init(self):
        # session_manager is a lazy property over _session_manager; setting the
        # backing field is how the real initialize_for_cli fills it too.
        self._session_manager = manager
        return ToolServerRegistry(), service

    monkeypatch.setattr(InitializationService, "initialize_for_cli", fake_init)
    stand_in_for_agent(monkeypatch, _DummyAgent)

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


class TestTheSessionsOwnLlmParams:
    """A session continued with `--session` runs with the llm_params it was left with (a /think, the web
    chat's thinking level), on the same terms as its profile: --llm-params wins, and only for its agent."""

    def _store(self, cli_env, params):
        loop = asyncio.new_event_loop()
        try:
            record = loop.run_until_complete(cli_env.manager.load_session("cli_user", "s1"))
            record["llm_params"] = params
            loop.run_until_complete(cli_env.manager.save_session(record))
        finally:
            loop.close()
        cli_env.manager.clear_cache()

    def _chat(self, monkeypatch, *extra):
        import agent_system.cli_utils.chat as chat

        seen = {}
        monkeypatch.setattr(chat, "run_chat_loop", lambda **kwargs: seen.update(kwargs))
        monkeypatch.setattr("agent_system.llm.factory.create_llm_from_profile",
                            lambda config, llm_profile, llm_params=None: object())
        _run(monkeypatch, ["agent-cli", "chat", "--session", "s1", *extra])
        return seen

    def test_they_come_back(self, cli_env, monkeypatch):
        self._store(cli_env, {"thinking_level": "high"})
        assert self._chat(monkeypatch)["llm_params"] == {"thinking_level": "high"}

    def test_typed_ones_win(self, cli_env, monkeypatch):
        self._store(cli_env, {"thinking_level": "high"})
        assert self._chat(monkeypatch, "--llm-params", "thinking_level=low")["llm_params"] == {
            "thinking_level": "low"}

    def test_a_one_shot_run_writes_what_it_ran_with_back(self, cli_env, monkeypatch):
        """It wrote the profile and left the params: the next bare --session went back to the old ones."""
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1",
                           "--llm-params", "thinking_level=low"])
        assert cli_env.saved.get("llm_choice") == {"profile": STORED_PROFILE,
                                                   "params": {"thinking_level": "low"}}

    def test_another_agent_does_not_take_them(self, cli_env, monkeypatch):
        self._store(cli_env, {"thinking_level": "high"})
        assert not self._chat(monkeypatch, "--agent", "config_default_agent")["llm_params"]


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
    """core/session_presence/ through the real main()."""

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
        monkeypatch.setattr(cli, "shutdown_tools", mcp)
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

    @staticmethod
    def _records_the_task(monkeypatch):
        """What actually reaches the agent -- the one thing main() decides here
        and nothing downstream can put back."""
        from agent_system.servers.agent.server import Agent

        seen = []
        original = Agent.run_events  # the stand-in's, which cli_env gave the real class

        def recording(self, task, **kwargs):
            seen.append(task)
            return original(self, task, **kwargs)

        monkeypatch.setattr(Agent, "run_events", recording)
        return seen

    def test_a_woken_run_speaks_as_the_run_not_as_a_person(
            self, cli_env, monkeypatch, tmp_path):
        """The wake is that run's whole input. As a `user` turn it claimed a
        person had typed it -- in the stored transcript and in front of the
        model, whose first job is to report which of the two happened."""
        from agent_system.core import session_presence as sp
        from agent_system.llm.message_roles import DEVELOPER

        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        (sessions / "cli_user").mkdir(parents=True, exist_ok=True)
        (sessions / "cli_user" / "s1.pending").touch()
        monkeypatch.setattr(sp.presence, "spawn_wake", lambda session_id, user_id, depth: (0, 0.0))
        seen = self._records_the_task(monkeypatch)

        self._wake(monkeypatch)

        task, = seen
        assert task.content == sp.WAKE_TASK
        assert task.role == DEVELOPER

    def test_a_typed_run_is_still_a_person_talking(
            self, cli_env, monkeypatch, tmp_path):
        """The counter-proof: without it the test above passes just as well on
        a CLI that turns EVERY task into a note from the run."""
        self._presence_on(cli_env, monkeypatch, tmp_path)
        seen = self._records_the_task(monkeypatch)

        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])

        assert seen == ["weiter"]

    def test_a_woken_run_with_input_waiting_continues_the_session(
            self, cli_env, monkeypatch, tmp_path):
        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        (sessions / "cli_user").mkdir(parents=True, exist_ok=True)
        (sessions / "cli_user" / "s1.pending").touch()
        # spawn_wake stays the suite's guard: the run takes the input, so
        # letting go wakes nobody, and a wake here fails the test.

        self._wake(monkeypatch)

        assert cli_env.saved.get("session_id") == "s1"

    def test_a_woken_run_takes_the_input_with_its_stamp_and_does_not_wake_itself(
            self, cli_env, monkeypatch, tmp_path):
        """A woken run is told that input waits, so what rang for it is
        delivered: it takes the marker at once, with the stamp that ends a
        ringer still ringing (wake_session). Left to its first LLM call, a
        ringer that found the session held rang on into a woken run per ring,
        up to max_wake_depth -- three woken turns in a row in session
        gx953bf9y3 -- and a run that never got to a call (this dummy agent)
        left the marker and woke itself again as it let go."""
        from agent_system.core import session_presence as sp

        sessions = self._presence_on(cli_env, monkeypatch, tmp_path)
        (sessions / "cli_user").mkdir(parents=True, exist_ok=True)
        (sessions / "cli_user" / "s1.pending").touch()
        spawned = []
        monkeypatch.setattr(sp.presence, "spawn_wake", lambda *args: spawned.append(args) or (0, 0.0))

        self._wake(monkeypatch)

        assert cli_env.saved.get("session_id") == "s1", "the woken run did not run"
        assert sp.presence_for(cli_env.config).wake_stamp("s1", "cli_user"), "it took the input without a stamp"
        assert spawned == [], "the woken run woke its session again"


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
        assert "Ignoring 'weiter': --list-sessions takes a count or 'all'." in out
        assert "Sessions for 'cli_user' (1 of 1):" in out
        assert not cli_env.saved, "the task ran anyway"

    def test_the_session_being_continued_is_marked_and_the_footer_is_there(
            self, cli_env, monkeypatch, capsys):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions",
                           "--session", "s1"])

        out = capsys.readouterr().out
        marked = [l for l in out.splitlines() if l.startswith(" *")]
        assert len(marked) == 1 and " s1 " in marked[0], out
        assert "Continue one with: --session <id or title>" in out


class TestListingLeavesPipelineRunsOut:
    """A person's chats, not the runs pipelines started under the same user.

    Measured for cli_user on 25.09.2026: 4562 of 5054 top-level sessions ran
    on agents the chat does not offer, and 18 of the newest 20 lines were
    benchmark scorer runs. Which agents the chat offers is the runtime's
    answer (visibility ui/both) -- here a stand-in that offers STORED_AGENT.
    """

    @pytest.fixture
    def offered(self, monkeypatch, cli_env):
        from agent_system.services.initialization_service import InitializationService

        class _Runtime:
            def describe(self, name):
                return SimpleNamespace(visibility="ui") if name == STORED_AGENT else None

        runtime = _Runtime()
        monkeypatch.setattr(InitializationService, "runtime", property(lambda self: runtime))
        loop = asyncio.new_event_loop()
        try:
            for i in range(3):
                loop.run_until_complete(cli_env.manager.create_session(
                    user_id="cli_user", session_id=f"bench{i}",
                    agent_name="v4_scorer_bench_42", llm_profile=STORED_PROFILE))
        finally:
            loop.close()
        cli_env.manager.clear_cache()
        return runtime

    def test_the_chat_filters_with_the_runtime_this_process_bootstrapped(
            self, cli_env, offered, monkeypatch):
        """Handed in -- without it the terminal's /sessions lists every
        pipeline run and says nothing about it."""
        import agent_system.cli_utils.chat as chat

        seen = {}
        monkeypatch.setattr(chat, "run_chat_loop", lambda **kwargs: seen.update(kwargs))
        monkeypatch.setattr(
            "agent_system.llm.factory.create_llm_from_profile",
            lambda config, llm_profile, llm_params=None: object())

        _run(monkeypatch, ["agent-cli", "chat", "hallo"])

        assert seen["runtime"] is offered

    def test_only_the_chats_are_listed_and_the_rest_is_counted(
            self, cli_env, offered, monkeypatch, capsys):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions"])

        out = capsys.readouterr().out
        assert "Sessions for 'cli_user' (1 of 1):" in out, out
        assert " s1 " in out and "bench" not in out.split("(3 more")[0], out
        # dropped rows are said, with the way to see them -- or it reads as "that is all"
        assert "(3 more on agents not meant for chat, most v4_scorer_bench_42 3" in out
        assert "--list-sessions all" in out

    def test_the_session_being_continued_stays_whatever_its_agent(
            self, cli_env, offered, monkeypatch, capsys):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions",
                           "--session", "bench1"])

        out = capsys.readouterr().out
        marked = [line for line in out.splitlines() if line.startswith(" *")]
        assert len(marked) == 1 and " bench1 " in marked[0], out
        assert "(2 more on agents not meant for chat" in out, out

    def test_all_lists_every_session(self, cli_env, offered, monkeypatch, capsys):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "--list-sessions", "all"])

        out = capsys.readouterr().out
        assert "Sessions for 'cli_user' (4 of 4):" in out, out
        assert "not meant for chat" not in out, out


class TestResumeByTitle:
    """`--session` takes the name a person gave the session, not just its id.

    A session id is machine-made (`2332j2kj22k`) and cannot be renamed -- it
    is the key the usage tracker, the message debugger, the context stores,
    the sub-session indexes and the presence locks file their rows under. So
    the title is the name, resolved in SessionManager.resolve_session_ref.
    """

    def _titled(self, cli_env, title, session_id):
        loop = asyncio.new_event_loop()
        try:
            session = loop.run_until_complete(cli_env.manager.create_session(
                user_id="cli_user", session_id=session_id, title=title,
                agent_name=STORED_AGENT, llm_profile=STORED_PROFILE))
            loop.run_until_complete(cli_env.manager.save_session(session))
        finally:
            loop.close()
        cli_env.manager.clear_cache()

    def _stamped(self, cli_env, title, session_id, updated_at):
        """A record with the stamp it is given (reinstate_session keeps it)."""
        session = {"session_id": session_id, "user_id": "cli_user", "title": title,
                   "created_at": updated_at, "updated_at": updated_at,
                   "agent_name": STORED_AGENT, "llm_profile": STORED_PROFILE,
                   "messages": [], "metadata": {}}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cli_env.manager.reinstate_session(session))
        finally:
            loop.close()
        cli_env.manager.clear_cache()

    def test_a_title_continues_the_session_it_belongs_to(self, cli_env, monkeypatch):
        self._titled(cli_env, "FPGA Quartus", "2332j2kj22k")

        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter",
                           "--session", "FPGA Quartus"])

        assert cli_env.saved.get("session_id") == "2332j2kj22k", (
            "the title was taken for an id of its own")
        # Resolved too late, the run takes the config default agent, holds a
        # lock under the typed name and writes that agent over the record --
        # the "survived exactly one resume" bug, by another door.
        assert cli_env.saved.get("agent_name") == STORED_AGENT, (
            "the session was continued with another agent")
        assert cli_env.saved.get("llm_profile") == STORED_PROFILE

    def test_an_id_still_wins_over_a_title_that_looks_like_one(self, cli_env, monkeypatch):
        self._titled(cli_env, "s1", "9kk9kk9kk9")

        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1"])

        assert cli_env.saved.get("session_id") == "s1"

    def test_a_title_several_sessions_share_says_it_took_the_newest(
            self, cli_env, monkeypatch, capsys):
        # Fixed stamps, the newer one written FIRST: save_session stamps "now",
        # and two saves within one Windows clock tick would make "newest" a
        # coin toss -- reinstate_session keeps the stamp it is given.
        self._stamped(cli_env, "FPGA Quartus", "new2new2", "2026-09-24T10:00:00+00:00")
        self._stamped(cli_env, "FPGA Quartus", "old1old1", "2026-09-01T10:00:00+00:00")

        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter",
                           "--session", "FPGA Quartus"])

        assert cli_env.saved.get("session_id") == "new2new2"
        assert "the newest of 2 with this title" in capsys.readouterr().err

    def test_a_name_nobody_gave_still_starts_a_session_under_it(self, cli_env, monkeypatch):
        """Unchanged behaviour: `--session fpga` is how a readable id is made."""
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "fpga"])

        assert cli_env.saved.get("session_id") == "fpga"
