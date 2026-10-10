"""The deliberate config reload has to actually reach the plugins.

Measured 2026-09-01 on the live system: `agent-cli reload` reported
``status: ok`` and refreshed **0 of 286** servers. Two independent holes:

1. The registry holds hybrid plugins (tools + hooks + web UI in one class)
   that keep the real tool server behind ``self.server``. Looking for
   ``reload_config`` only on the registered object missed the only
   implementation there was — ``sub_agent_manager`` itself was reported as
   unsupported.
2. No agent type implemented ``reload_config`` at all, although the feature's
   own docstring advertises "an agent's template vars" as covered. Raising an
   agent's ``max_steps`` therefore needed a full API restart, which drops
   in-flight book runs.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
    PluginsConfig,
)
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.agent.server import Agent
from agent_system.services.config_reload import _reload_target, reload_plugin_configs


class _Reloadable:
    def __init__(self):
        self.seen = None

    def reload_config(self, cfg):
        self.seen = cfg
        return {"knob": {"old": 1, "new": 2}}


class _HybridWithServer:
    """The shape that broke it: registered wrapper, real server inside."""

    def __init__(self):
        self.server = _Reloadable()


class _HybridWithMcpServer:
    def __init__(self):
        self.tool_server = _Reloadable()


class _Plain:
    pass


class TestReloadTargetResolution:
    def test_direct_implementation_is_found(self):
        plugin = _Reloadable()
        assert _reload_target(plugin) == plugin.reload_config

    def test_hybrid_inner_server_is_found(self):
        plugin = _HybridWithServer()
        fn = _reload_target(plugin)
        assert fn is not None, "the hybrid wrapper was overlooked again"
        assert fn == plugin.server.reload_config

    def test_hybrid_mcp_server_attribute_is_found(self):
        plugin = _HybridWithMcpServer()
        assert _reload_target(plugin) == plugin.tool_server.reload_config

    def test_plugin_without_implementation_stays_unsupported(self):
        assert _reload_target(_Plain()) is None

    def test_self_referencing_plugin_stays_unsupported(self):
        """A plugin whose ``server`` points back at itself resolves to
        nothing — there is no recursion here, the direct lookup has already
        answered. (An explicit ``inner is plugin`` guard survived its own
        removal in the mutation run: dead code, so it is gone.)"""
        plugin = _Plain()
        plugin.server = plugin
        assert _reload_target(plugin) is None


def _registry_of(**plugins_by_name):
    """A stand-in for the process-wide registry holding LIVE instances."""

    class _Adapter:
        def __init__(self, p):
            self.plugin_server = p

    class _Registry:
        def list_servers(self):
            return list(plugins_by_name)

        def get_server(self, name):
            return _Adapter(plugins_by_name[name])

    return _Registry()


def _fresh_config(servers, default_agent_cfg=None):
    """A REAL config object, so the reload runs the real merge path."""
    default_cfg = ToolServerConfig(type="basic_agent", enabled=True,
                            agent_config=default_agent_cfg)
    return AgentSystemConfig(
        plugins=PluginsConfig(default_config=default_cfg, servers=servers),
    )


class TestReloadReport:
    def test_hybrid_lands_in_refreshed_not_unsupported(self, monkeypatch):
        """The end-to-end shape of the bug: before the fix this plugin was
        reported as unsupported and nothing happened."""
        plugin = _HybridWithServer()
        monkeypatch.setattr(
            "agent_system.plugins.tool_adapter.plugin_tool_registry",
            _registry_of(hybrid=plugin),
        )

        report = reload_plugin_configs(_fresh_config(
            {"hybrid": ToolServerConfig(type="basic_agent", enabled=True)}))

        assert [r["server"] for r in report["refreshed"]] == ["hybrid"]
        assert report["unsupported"] == []
        assert plugin.server.seen is not None, "reload_config was never called"

    def test_inherited_value_is_not_downgraded(self, monkeypatch):
        """The reload has to hand over the MERGED config, the same one
        bootstrap built the live agent from.

        Measured 2026-09-01 against the real config: 133 of 203 agents carry a
        raw ``max_steps`` of 20 in their own ``plugins.servers`` entry while
        their merged value is 100 or 30. Passing the raw entry would have fed
        those base values back and silently downgraded them — the reverse of
        what a reload is for.
        """
        agent = _agent(max_steps=100)
        monkeypatch.setattr(
            "agent_system.plugins.tool_adapter.plugin_tool_registry",
            _registry_of(worker=agent),
        )
        # max_steps lives on the BASE only; the server entry overrides
        # something else entirely — exactly the live shape.
        report = reload_plugin_configs(_fresh_config(
            servers={"worker": ToolServerConfig(
                type="basic_agent", enabled=True,
                agent_config=AgentConfig(escalate_rounds=3))},
            default_agent_cfg=AgentConfig(max_steps=100),
        ))

        assert agent.agent_config.max_steps == 100, "the reload downgraded it"
        assert report["errors"] == []
        assert [c for r in report["refreshed"] for c in r["changes"]] \
            == ["escalate_rounds"]

    def test_broken_entry_does_not_abort_the_others(self, monkeypatch):
        """One unresolvable config must not take the whole reload down."""
        good = _Reloadable()
        monkeypatch.setattr(
            "agent_system.plugins.tool_adapter.plugin_tool_registry",
            _registry_of(broken=_Reloadable(), good=good),
        )

        def _boom(name, cfg):
            if name == "broken":
                raise ValueError("kaputt")
            return ToolServerConfig(type="basic_agent", enabled=True)

        # config_reload imports the resolver at call time, so patching it at
        # its source is what the production path actually picks up.
        monkeypatch.setattr(
            "agent_system.config.settings.get_tool_server_config", _boom)

        report = reload_plugin_configs(_fresh_config(
            {"broken": ToolServerConfig(enabled=True), "good": ToolServerConfig(enabled=True)}))

        assert [e["server"] for e in report["errors"]] == ["broken"]
        assert [r["server"] for r in report["refreshed"]] == ["good"]
        assert good.seen is not None


def _agent(max_steps=20, **kw):
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4",
                                        api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )
    agent_config = AgentConfig(max_steps=max_steps, **kw)
    system_config = AgentSystemConfig(llm_system=llm_system)
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", system_config, server_config, ToolServerRegistry())


def _mcp(**kw):
    return ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(**kw))


class TestAgentReloadConfig:
    def test_max_steps_takes_effect_without_restart(self):
        agent = _agent(max_steps=20)
        changes = agent.reload_config(_mcp(max_steps=50))
        assert agent.agent_config.max_steps == 50
        assert changes["max_steps"] == {"old": 20, "new": 50}

    def test_unchanged_value_is_not_reported(self):
        agent = _agent(max_steps=20)
        assert agent.reload_config(_mcp(max_steps=20)) == {}

    def test_startup_wired_fields_are_left_alone(self):
        """``self.llm`` was built from the profile at startup. Refreshing the
        config alone would desync the two, so these need a restart — and the
        reload must not pretend otherwise."""
        agent = _agent(max_steps=20)
        vorher = agent.agent_config.default_llm_profile
        changes = agent.reload_config(_mcp(max_steps=20, llm_profile=["anders"]))
        assert "llm_profile" not in changes
        assert agent.agent_config.default_llm_profile == vorher

    def test_escalation_knobs_are_refreshed(self):
        """The escalator is rebuilt per request, so its knobs are safe."""
        agent = _agent(max_steps=20, escalate_rounds=2)
        changes = agent.reload_config(_mcp(max_steps=20, escalate_rounds=4))
        assert agent.agent_config.escalate_rounds == 4
        assert changes["escalate_rounds"] == {"old": 2, "new": 4}

    def test_config_without_agent_section_changes_nothing(self):
        agent = _agent(max_steps=20)
        assert agent.reload_config(ToolServerConfig(type="agent", enabled=True)) == {}
        assert agent.agent_config.max_steps == 20

    def test_agent_without_own_config_does_not_blow_up(self):
        """The load-bearing half of the guard: with ``self.agent_config``
        None, a differing incoming value would run into
        ``setattr(None, ...)``. The mutation that drops only the other half
        stays green — this is the one that matters."""
        agent = _agent(max_steps=20)
        agent.agent_config = None
        assert agent.reload_config(_mcp(max_steps=50)) == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
