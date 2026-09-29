"""agent-run continues a session the same way agent-cli does.

The two entry points share sessions, so answering "which agent, which model"
differently is not a cosmetic inconsistency: agent-run defaulted the values
and then WROTE them back, so one one-shot run erased what agent-cli had
stored. These drive the real main_async against a real session on disk.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agent_system.agent_run as agent_run
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
    PluginsConfig,
)
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

#: The real one, before the fixture replaces it with a fake.
REAL_RUN_AGENT_REQUEST = agent_run.run_agent_request

STORED_AGENT = "stored_agent"
STORED_PROFILE = "profile_stored"
AGENT_DEFAULT_PROFILE = "profile_agent_default"


class _DummyAgent:
    def __init__(self, name, agent_config):
        self.name = name
        # Taken FROM the config, the way the real create_agent builds it. A
        # fake that invents its own default profile would make every
        # assertion about the fake instead of about the decision under test.
        self.agent_config = agent_config
        # The real Agent carries one, and agent_run opens the session through
        # it before the run.
        from agent_system.servers.agent.components.session_tracking import SessionTracker
        self._session_tracker = SessionTracker()


@pytest.fixture
def run_env(tmp_path, monkeypatch):
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
    monkeypatch.setattr(agent_run, "load_settings", lambda path=None: config)

    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    service = SessionService(manager)
    seen = {"agents": [], "saved": {}}

    async def fake_initialize_system(cfg):
        return object(), service

    async def fake_create_agent(cfg, registry, agent_name, session_service=None):
        seen["agents"].append(agent_name)
        return _DummyAgent(agent_name, cfg.plugins.servers[agent_name].agent_config)

    async def fake_request(agent, request, session_id, llm_override=None,
                           llm_profile_info=None):
        seen["profile_info"] = llm_profile_info
        return {"summary": "done", "calls": []}

    async def fake_save(**kwargs):
        seen["saved"].update(kwargs)
        return True

    monkeypatch.setattr(agent_run, "initialize_system", fake_initialize_system)
    monkeypatch.setattr(agent_run, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_run, "run_agent_request", fake_request)
    monkeypatch.setattr(service, "save_session", fake_save)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(manager.create_session(
            user_id="cli_user", session_id="s1",
            agent_name=STORED_AGENT, llm_profile=STORED_PROFILE))
    finally:
        loop.close()
    # The writing manager caches what it just created; the run under test has
    # to read the FILE, or "against a real session on disk" is a claim rather
    # than a fact.
    manager.clear_cache()

    return SimpleNamespace(seen=seen, config=config, service=service)


HOLDER = """
import sys, time
from agent_system.core.session_presence import SessionPresence
print(SessionPresence(sys.argv[1]).hold(sys.argv[2], sys.argv[3], "other_agent"), flush=True)
time.sleep(120)
"""


def _run(**kwargs):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(agent_run.main_async("weiter", **kwargs))
    finally:
        loop.close()


class TestAgentRunContinuesLikeAgentCli:
    def test_the_session_agent_is_used(self, run_env):
        _run(session_id="s1")
        assert run_env.seen["agents"] == [STORED_AGENT], (
            "agent-run fell back to the config default on a stored session")

    def test_the_session_profile_is_used(self, run_env):
        _run(session_id="s1")
        assert run_env.seen["profile_info"], "no LLM override was built"
        assert run_env.seen["profile_info"].startswith(STORED_PROFILE + ":")

    def test_the_record_is_not_overwritten_with_the_defaults(self, run_env):
        # The defect this exists for: agent-run wrote config.default_agent and
        # the agent's default profile back, erasing what agent-cli stored.
        _run(session_id="s1")
        assert run_env.seen["saved"].get("agent_name") == STORED_AGENT
        assert run_env.seen["saved"].get("llm_profile") == STORED_PROFILE

    def test_an_explicit_agent_still_wins(self, run_env):
        _run(session_id="s1", agent_name="config_default_agent")
        assert run_env.seen["agents"] == ["config_default_agent"]
        # Its OWN default, from the config -- not the profile the session
        # stored for the other agent.
        own_default = (run_env.config.plugins.servers["config_default_agent"]
                       .agent_config.default_llm_profile)
        assert run_env.seen["saved"].get("llm_profile") == own_default
        assert own_default != STORED_PROFILE, "fixture cannot tell them apart"

    def test_without_a_session_the_defaults_stand(self, run_env):
        _run(session_id="fresh-one")
        assert run_env.seen["agents"] == ["config_default_agent"]
        assert run_env.seen["saved"].get("agent_name") == "config_default_agent"

    def test_an_explicit_profile_still_wins(self, run_env):
        _run(session_id="s1", llm_profile=AGENT_DEFAULT_PROFILE)
        assert run_env.seen["profile_info"].startswith(AGENT_DEFAULT_PROFILE + ":")
        assert run_env.seen["saved"].get("llm_profile") == AGENT_DEFAULT_PROFILE

    def test_an_agent_the_config_no_longer_defines_is_ignored(self, run_env,
                                                              monkeypatch):
        # Sessions outlive the config that made them. Handing a stale name to
        # create_agent would abort a run over something the user never typed.
        del run_env.config.plugins.servers[STORED_AGENT]

        _run(session_id="s1")

        assert run_env.seen["agents"] == ["config_default_agent"]


class TestDegradedBootstrap:
    """initialize_system returns session_service=None when bootstrap fails.

    Its own comment calls that path "agent can still work". Reaching through
    the None to find a session manager would break it before the agent is even
    built -- a run that used to answer would exit 1 instead.
    """

    def test_a_run_without_a_session_service_still_answers(self, run_env,
                                                           monkeypatch):
        from agent_system.tools.base import ToolServerRegistry

        async def degraded(cfg):
            return ToolServerRegistry(), None

        monkeypatch.setattr(agent_run, "initialize_system", degraded)

        _run()

        assert run_env.seen["agents"] == ["config_default_agent"], (
            "the degraded path never reached the agent")


class TestSessionPresence:
    def test_the_run_holds_its_session_through_the_save_after_it(self, run_env, monkeypatch, tmp_path):
        from agent_system.config.models import SessionPresenceConfig
        from agent_system.core.session_presence import presence_for

        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
        run_env.config.session_presence = SessionPresenceConfig(enabled=True)
        presence = presence_for(run_env.config)
        at_save = []

        async def save(**kwargs):
            at_save.append(presence.get("s1", "cli_user")["status"])
            return True

        monkeypatch.setattr(run_env.service, "save_session", save)
        _run(session_id="s1")

        assert at_save == ["running"], "the session was let go before the save after the run"
        assert presence.get("s1", "cli_user")["status"] == "idle"

    def test_the_session_is_held_before_it_is_loaded(self, run_env, monkeypatch, tmp_path):
        from agent_system.config.models import SessionPresenceConfig
        from agent_system.core.session_presence import presence_for

        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
        run_env.config.session_presence = SessionPresenceConfig(enabled=True)
        presence = presence_for(run_env.config)
        at_load = []
        load = run_env.service.load_and_restore_session

        async def recording_load(agent, user_id, session_id):
            at_load.append((presence.get(session_id, user_id) or {}).get("status"))
            return await load(agent, user_id, session_id)

        monkeypatch.setattr(run_env.service, "load_and_restore_session", recording_load)
        _run(session_id="s1")

        assert at_load == ["running"], "the session was loaded before it was held"

    def test_a_session_another_process_runs_is_refused_and_force_runs_it(
            self, run_env, monkeypatch, tmp_path, capsys):
        import subprocess
        import sys

        from agent_system.config.models import SessionPresenceConfig

        sessions = tmp_path / "sessions"
        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(sessions))
        run_env.config.session_presence = SessionPresenceConfig(enabled=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(sessions), "s1", "cli_user"],
            stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "True", "the other process could not hold it"

            with pytest.raises(SystemExit) as refused:
                _run(session_id="s1")

            assert refused.value.code == 1
            assert "another process" in capsys.readouterr().err
            assert run_env.seen["saved"] == {}, "it ran the session anyway"

            _run(session_id="s1", force=True)

            assert run_env.seen["saved"].get("session_id") == "s1"
        finally:
            holder.kill()
            holder.wait()

    @pytest.mark.parametrize("stopped", [True, False])
    def test_a_run_its_user_stopped_is_not_woken_by_input_that_came_meanwhile(
            self, run_env, monkeypatch, tmp_path, stopped):
        # Ctrl-C cancels agent-run's task, not the run: the run lets go of its own hold
        # knowing nothing of it, and the hold of agent-run goes last and says it.
        from agent_system.config.models import SessionPresenceConfig
        from agent_system.core import session_presence as sp

        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
        run_env.config.session_presence = SessionPresenceConfig(enabled=True)
        presence = sp.presence_for(run_env.config)
        monkeypatch.setattr(sp, "_stops", set())
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))

        async def run(agent, request, session_id, llm_override=None, llm_profile_info=None):
            # holds and lets go of the session as the agent loop does, never told of the stop
            assert presence.hold(session_id, "cli_user", "agent", run="r-run")
            assert presence.notify(session_id, "cli_user")[0] == "delivered_next_step", "fixture: not held"
            presence.release(session_id, "cli_user")
            return {"cancelled": True} if stopped else {"summary": "done", "calls": []}

        monkeypatch.setattr(agent_run, "run_agent_request", run)
        _run(session_id="s1")

        assert woken == ([] if stopped else ["s1"])


    def test_a_ctrl_c_while_the_session_loads_is_a_stop(self, run_env, monkeypatch, tmp_path):
        from agent_system.config.models import SessionPresenceConfig
        from agent_system.core import session_presence as sp

        monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
        run_env.config.session_presence = SessionPresenceConfig(enabled=True)
        presence = sp.presence_for(run_env.config)
        monkeypatch.setattr(sp, "_stops", set())
        woken = []
        monkeypatch.setattr(sp, "spawn_wake", lambda session_id, user_id, depth: woken.append(session_id) or (0, 0.0))

        async def interrupted_load(agent, user_id, session_id):
            assert sp.SessionPresence(tmp_path / "sessions").notify(session_id, user_id)[0] == "delivered_next_step"
            raise asyncio.CancelledError   # asyncio.run's answer to Ctrl-C

        monkeypatch.setattr(run_env.service, "load_and_restore_session", interrupted_load)
        with pytest.raises(asyncio.CancelledError):
            _run(session_id="s1")

        assert woken == []
        assert presence.notify("s1", "cli_user")[0] == "queued"


class TestAgentRunOwnsTheToolIntegration:
    """agent-run is a process entry point, so the tool integration is its to end.

    The agent only sets up the module-level singleton because it asked first;
    it does not own it (tests/agent/test_agent_stop_plugin.py). agent-cli and
    the app lifespan already called shutdown_tools() on their way out --
    agent-run was the one entry point that left every plugin to the
    interpreter's exit: a terminal's background processes, an SSH channel,
    file_ops' indexer. A wake run is an agent-run process too.
    """

    @pytest.fixture
    def ended(self, run_env, monkeypatch):
        calls = []

        async def fake_shutdown_tools():
            calls.append("shutdown_tools")

        monkeypatch.setattr(agent_run, "shutdown_tools", fake_shutdown_tools)
        return calls

    def test_a_run_that_finishes_takes_it_down(self, run_env, ended):
        _run(session_id="s1")
        assert ended == ["shutdown_tools"]

    def test_a_run_that_fails_takes_it_down_too(self, run_env, ended, monkeypatch):
        async def broken(*args, **kwargs):
            raise RuntimeError("the model went away")

        monkeypatch.setattr(agent_run, "run_agent_request", broken)
        with pytest.raises(SystemExit):
            _run(session_id="s1")
        assert ended == ["shutdown_tools"], "a failed run left the plugins running"


class TestAnUnknownProfile:
    def test_it_is_refused_with_the_profiles_there_are(self, run_env, capsys):
        """The message is the configuration's answer, not wrapped in a second
        "failed to apply" around it -- it lists what --llm can take instead."""
        with pytest.raises(SystemExit) as stopped:
            _run(llm_profile="gone")

        assert stopped.value.code == 1
        err = capsys.readouterr().err
        assert "LLM profile 'gone' not found" in err, err
        assert "Failed to apply" not in err, err
        assert STORED_PROFILE in err and AGENT_DEFAULT_PROFILE in err, err


class TestARefusedRunExits1:
    """The callers read the code (publish_pipeline, the writer runners), and
    agent-run left with 0 on a session it could not open."""

    def test_another_users_session(self, run_env, monkeypatch, capsys):
        from agent_system.services.session_manager import SessionPermissionError

        async def refused(agent, user_id, session_id):
            raise SessionPermissionError("owned by u2")

        monkeypatch.setattr(run_env.service, "load_and_restore_session", refused)

        with pytest.raises(SystemExit) as failed:
            _run(session_id="s1")

        assert failed.value.code == 1
        assert "belongs to a different user" in capsys.readouterr().err
        assert run_env.seen["saved"] == {}, "it ran the session anyway"

    def test_a_session_to_continue_without_a_store(self, run_env, monkeypatch, capsys):
        """The degraded bootstrap has no session service: the run would go on
        without the session's history and save nothing."""
        from agent_system.tools.base import ToolServerRegistry

        async def degraded(cfg):
            return ToolServerRegistry(), None

        monkeypatch.setattr(agent_run, "initialize_system", degraded)

        with pytest.raises(SystemExit) as failed:
            _run(session_id="s1")

        assert failed.value.code == 1
        assert "cannot continue session 's1'" in capsys.readouterr().err


def test_a_run_refused_before_it_ran_is_not_saved(run_env, monkeypatch):
    """The agent refused the run before it started (Agent.run_events: its role gate, another user's session,
    another run's lock). It ran nothing; saved, the session's record was rewritten with this run's agent and
    profile and its updated_at moved."""
    async def refused(self, task, request_id=None, session_id=None, **kwargs):
        yield {"type": "error", "message": "refused", "request_id": request_id, "error_type": "agent_role_gate"}
        yield {"type": "end"}

    monkeypatch.setattr(agent_run, "run_agent_request", REAL_RUN_AGENT_REQUEST)
    monkeypatch.setattr(_DummyAgent, "run_events", refused, raising=False)

    _run(session_id="s1")

    assert run_env.seen["saved"] == {}, f"a refused run was saved: {run_env.seen['saved']}"

