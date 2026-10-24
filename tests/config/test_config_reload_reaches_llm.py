"""A config reload reaches what an agent runs on, not only seven scalars.

Until 2026-10-24 ``Agent.reload_config`` copied max_steps and the escalation
knobs; the client of ``llm_profile`` was built at start and kept. So a reload
after editing an agent's ``llm_profile`` / ``llm_params`` or the model behind a
profile in llm.yaml reported "unchanged" and the agent ran on -- while a run
switched to a profile already resolved against the fresh config. Seen on the
live system the same day: an agent YAML's model chain edited, reloaded,
``refreshed=0``.

Clients are now held by what they are built from (llm/factory.py
ProfileClients) and rebuilt once, on the first use after that changed.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    PluginsConfig,
    ToolServerConfig,
)
from agent_system.config.models.agent import HooksConfig
from agent_system.llm import factory
from agent_system.llm.factory import ProfileClients, chat_fingerprint, config_reloaded
from agent_system.runtime import Runtime, ServerDecl
from agent_system.servers.agent.server import Agent
from agent_system.services.config_reload import reload_plugin_configs
from agent_system.tools.base import ToolServerRegistry


def _system(normal_model: str = "gpt-4", servers: dict | None = None) -> AgentSystemConfig:
    """A REAL config: profile "normal" on *normal_model*, "big" on gpt-5."""
    models = {name: LLMModelConfig(provider="openai", model=name, api_key="fake-key-123")
              for name in ("gpt-4", "gpt-5")}
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models=models,
            profiles={"normal": LLMProfile(model_ref=normal_model), "big": LLMProfile(model_ref="gpt-5")},
            default_profile="normal"),
        plugins=PluginsConfig(servers=servers or {}),
    )


def _entry(**agent_kw) -> ToolServerConfig:
    return ToolServerConfig(type="agent", enabled=True,
                            agent_config=AgentConfig(llm_profile=agent_kw.pop("llm_profile", ["normal", "big"]),
                                                     **agent_kw))


def _agent(**agent_kw) -> Agent:
    return Agent("worker", _system(), _entry(**agent_kw), ToolServerRegistry())


class TestAgentFollowsTheReload:
    def test_a_changed_model_chain_reaches_the_next_run_not_the_running_one(self):
        agent = _agent()
        running = agent.llm
        assert running.model == "gpt-4", "fixture: the agent starts on gpt-4"

        changes = agent.reload_config(_entry(llm_profile=["big"]), system_config=_system())

        assert "llm_profile" in changes
        assert agent.llm.model == "gpt-5"
        assert running.model == "gpt-4", "the client a run took was changed under it"
        assert agent.llm_profile_info.startswith("big:")

    def test_a_model_changed_in_llm_yaml_reaches_the_agent(self):
        """The agent's own entry is unchanged; the model behind its profile is not."""
        agent = _agent()
        assert agent.llm.model == "gpt-4", "fixture"
        fresh = _system(normal_model="gpt-5")

        changes = agent.reload_config(_entry(), system_config=fresh)

        assert agent.system_config is fresh
        assert agent.llm.model == "gpt-5"
        # no field of the entry changed: without this the report says "unchanged" for an agent on another model
        assert changes["llm"]["old"].endswith("/gpt-4") and changes["llm"]["new"].endswith("/gpt-5"), changes

    def test_an_unchanged_config_keeps_the_very_same_client(self):
        """Nachschlagen, nicht neu bauen: a reload of an unchanged config hands
        back the same object (its connection pool, its wiring)."""
        agent = _agent()
        before = agent.llm
        assert agent.reload_config(_entry(), system_config=_system()) == {}
        assert agent.llm is before

    def test_an_agent_whose_profile_never_built_still_takes_its_config(self):
        """The build-first guard protects a working client; an agent that never had one (it runs on
        overrides, or its key is missing) is not locked out of every other edit."""
        agent = Agent("worker", _system(), _entry(llm_profile=["unknown"]), ToolServerRegistry())
        assert agent.llm is None, "fixture: the profile does not build"
        changes = agent.reload_config(_entry(llm_profile=["unknown"], max_steps=99), system_config=_system())
        assert agent.agent_config.max_steps == 99
        assert "max_steps" in changes

    def test_a_pinned_client_is_never_replaced(self):
        agent = _agent()
        pinned = object()
        agent.llm = pinned
        agent.reload_config(_entry(llm_profile=["big"]), system_config=_system(normal_model="gpt-5"))
        assert agent.llm is pinned

    def test_a_fallback_follows_changed_llm_params(self):
        agent = _agent()
        before = agent._fallback_client("big")
        assert before is not None and before.model == "gpt-5", "fixture"
        assert agent._fallback_client("big") is before, "fixture: the fallback is kept between steps"

        agent.reload_config(_entry(llm_params={"big": {"max_tokens": 777}}), system_config=_system())

        after = agent._fallback_client("big")
        assert after is not before

    def test_the_hook_switch_follows_the_reload(self):
        agent = _agent()
        assert agent._hook_manager.is_enabled(), "fixture"
        agent.reload_config(_entry(hooks=HooksConfig(enabled=False)), system_config=_system())
        assert not agent._hook_manager.is_enabled()

    def test_what_the_agent_offers_follows_the_reload(self):
        agent = _agent()
        assert asyncio.run(agent.list_tools()), "fixture: the agent offers itself"
        entry = _entry()
        entry.description = "Writes the chapter summaries"
        changes = agent.reload_config(entry, system_config=_system())
        assert asyncio.run(agent.list_tools())[0].description == "Writes the chapter summaries"
        assert changes["description"]["new"] == "Writes the chapter summaries", "applied, but reported as unchanged"

    def test_loop_detection_is_derived_again(self):
        agent = _agent()
        assert agent._loop_detection_config != {"enabled": False}, "fixture: detection on by default"
        fresh = _entry()
        fresh.agent_config.loop_detection.enabled = False
        agent.reload_config(fresh, system_config=_system())
        assert agent._loop_detection_config == {"enabled": False}


class TestTheReloadServiceHandsOverTheSystemConfig:
    def test_the_agent_gets_the_fresh_system_config(self, monkeypatch):
        """The real path: merged entry and fresh system config through
        reload_plugin_configs, to an agent whose reload_config takes both."""
        agent = _agent()
        assert agent.llm.model == "gpt-4", "fixture"

        class _Adapter:
            plugin_server = agent

        class _Registry:
            def list_servers(self):
                return ["worker"]

            def get_server(self, name):
                return _Adapter()

        monkeypatch.setattr("agent_system.plugins.tool_adapter.plugin_tool_registry", _Registry())
        monkeypatch.setattr(Runtime, "last_started", None)
        fresh = _system(normal_model="gpt-5", servers={"worker": _entry()})

        report = reload_plugin_configs(fresh)

        assert report["errors"] == []
        assert agent.system_config is fresh
        assert agent.llm.model == "gpt-5"

    def test_a_reload_retries_what_did_not_build(self, monkeypatch):
        """The Setup panel writes a key into the environment and reloads: the
        spec is the same, so only the reload itself can free the failure."""
        builds = []

        def build(config, profile, params):
            builds.append(profile)
            raise RuntimeError("OPENROUTER_API_KEY is not set")

        clients = ProfileClients(build=build, fingerprint=lambda config, profile, params: "same")
        clients.get(None, "p")

        class _Empty:
            def list_servers(self):
                return []

        monkeypatch.setattr("agent_system.plugins.tool_adapter.plugin_tool_registry", _Empty())
        monkeypatch.setattr(Runtime, "last_started", None)
        reload_plugin_configs(_system())

        clients.get(None, "p")
        assert builds == ["p", "p"]


    def test_the_declaration_takes_only_what_the_reload_took(self, monkeypatch):
        """The agent editor reads the running side from the declaration: a changed type still needs a
        restart and must keep reading as changed, the description the reload applied must not."""
        agent = _agent()
        old_entry = _entry()
        decl = ServerDecl(name="worker", type="agent", server_config=old_entry)

        class _Runtime:
            registry = ToolServerRegistry()

            def carry_offered(self, config):
                pass

            def describe(self, name):
                return decl if name == "worker" else None

        class _Adapter:
            plugin_server = agent

        class _Registry:
            def list_servers(self):
                return ["worker"]

            def get_server(self, name):
                return _Adapter()

        monkeypatch.setattr("agent_system.plugins.tool_adapter.plugin_tool_registry", _Registry())
        monkeypatch.setattr(Runtime, "last_started", _Runtime())
        fresh_entry = _entry(max_steps=77)
        fresh_entry.type = "basic_agent"
        fresh_entry.description = "new text"

        report = reload_plugin_configs(_system(servers={"worker": fresh_entry}))

        assert report["errors"] == []
        assert decl.server_config.type == "agent", "a type change would read as in sync"
        assert decl.server_config.description == "new text"
        assert decl.server_config.agent_config.max_steps == 77


class TestProfileClients:
    def test_a_failure_is_built_once_until_a_reload(self):
        builds = []

        def build(config, profile, params):
            builds.append(profile)
            raise RuntimeError("no key")

        clients = ProfileClients(build=build, fingerprint=lambda config, profile, params: "same")
        assert clients.get(None, "p") is None
        assert clients.get(None, "p") is None
        assert builds == ["p"], "a failure was built again although nothing changed"

        config_reloaded()
        assert clients.get(None, "p") is None
        assert builds == ["p", "p"], "a reload did not retry the failed build"

    def test_the_fingerprint_holds_no_key(self):
        """A resolved spec carries the API key in clear text; the fingerprint
        is a hash of it, never the spec itself."""
        fingerprint = chat_fingerprint(_system(), "normal")
        assert "fake-key-123" not in fingerprint
        assert fingerprint != chat_fingerprint(_system(normal_model="gpt-5"), "normal")
        assert fingerprint == chat_fingerprint(_system(), "normal"), "two equal configs must not rebuild"


class TestOfferedServersAreOfferedAnew:
    def _runtime(self, offer):
        runtime = Runtime.__new__(Runtime)
        old = ToolServerConfig(type="agent", enabled=True, description="old block",
                               agent_config=AgentConfig(llm_profile="normal"))
        runtime.config = _system(servers={"machine_agent": old})
        runtime._decls = {"machine_agent": ServerDecl(name="machine_agent", type="agent", server_config=old,
                                                      offered_by="stategraph_machine")}

        class _Factory:
            offered_servers = staticmethod(offer)

        runtime._plugins = {"stategraph_machine": _Factory}
        return runtime

    def test_an_edited_agent_block_reaches_the_fresh_config(self):
        runtime = self._runtime(lambda config: {"machine_agent": {
            "type": "agent", "enabled": True, "description": "new block",
            "agent_config": {"llm_profile": "big"}}})
        fresh = _system()
        runtime.carry_offered(fresh)
        assert fresh.plugins.servers["machine_agent"].description == "new block"

    def test_a_failing_offer_keeps_the_entry_the_last_reload_took(self):
        """Reload 1 takes an edited block, then the machine file breaks: reload 2 must keep reload 1's entry,
        not fall back to the one from the start."""
        offers = [{"machine_agent": {"type": "agent", "enabled": True, "description": "new block",
                                     "agent_config": {"llm_profile": "big"}}}]

        def offer(config):
            if not offers:
                raise RuntimeError("machine file broken")
            return offers.pop()

        runtime = self._runtime(offer)
        runtime.carry_offered(_system())
        fresh = _system()
        runtime.carry_offered(fresh)
        assert fresh.plugins.servers["machine_agent"].description == "new block"

    def test_a_failing_offer_keeps_the_running_entry(self):
        def offer(config):
            raise RuntimeError("machine file broken")

        runtime = self._runtime(offer)
        fresh = _system()
        runtime.carry_offered(fresh)
        assert fresh.plugins.servers["machine_agent"].description == "old block"


@pytest.fixture(autouse=True)
def _fresh_generation(monkeypatch):
    """Failures held by one test must not depend on another test's reload."""
    monkeypatch.setattr(factory, "_reload_generation", factory._reload_generation)
