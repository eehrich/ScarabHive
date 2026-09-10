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

    def test_an_explicit_agent_still_wins(self, cli_env, monkeypatch):
        _run(monkeypatch, ["agent-cli", "--raw", "run", "weiter", "--session", "s1",
                           "--agent", "config_default_agent"])
        assert cli_env.saved.get("agent_name") == "config_default_agent"
        # And the stored profile is NOT forced onto the other agent.
        assert cli_env.saved.get("llm_profile") == AGENT_DEFAULT_PROFILE
