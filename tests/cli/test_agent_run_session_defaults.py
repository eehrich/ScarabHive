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
    MCPConfig,
    PluginsConfig,
)
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

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
        # The real Agent carries one, and agent_run writes the session
        # metadata through it before the run.
        self._session_tracker = SimpleNamespace(
            set_session_messages=lambda sid, messages: None,
            set_session_metadata=lambda sid, meta: None)


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
        STORED_AGENT: MCPConfig(type="agent", enabled=True,
                                agent_config=AgentConfig(
                                    system_prompt="x",
                                    llm_profile=AGENT_DEFAULT_PROFILE)),
        "config_default_agent": MCPConfig(type="agent", enabled=True,
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

    return SimpleNamespace(seen=seen, config=config)


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
        from agent_system.mcp.base import MCPRegistry

        async def degraded(cfg):
            return MCPRegistry(), None

        monkeypatch.setattr(agent_run, "initialize_system", degraded)

        _run()

        assert run_env.seen["agents"] == ["config_default_agent"], (
            "the degraded path never reached the agent")
