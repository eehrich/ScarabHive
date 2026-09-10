"""The whole resume path through main(), against a session on disk.

The pieces are unit-tested next door, but the wiring in main() is where this
feature actually lives -- and where it broke twice: the stored values were
read into variables nobody used, and the SAVE at the end wrote the raw --llm
flag back, which put the agent's default over the session's own choice on
every bare resume. Both are invisible to a test that only calls the helpers.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
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
from agent_system.services.session_manager import SessionManager
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

    return SimpleNamespace(config=config, saved=saved, manager=manager)


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    cli.main()


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
