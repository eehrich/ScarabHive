"""A fallback profile that does not exist is a resilience that never was.

Only the PRIMARY profile of an agent is resolved at startup. Fallback links
are built lazily, inside the failure path — so a typo in one stays invisible
until the primary model rate-limits, i.e. exactly the moment the fallback was
configured for. Then the run dies with "Profile 'x' not found".

Loud but not fatal, same rule as the stale llm_params check next door: one
typo in ONE agent must not keep every server down.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMProfile, LLMModelConfig, LLMSystemConfig,
    MCPConfig, PluginsConfig,
)
from agent_system.config.settings import _report_unknown_llm_profiles


def _config(chain, advanced=None):
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="ollama", model="q")},
            profiles={"good": LLMProfile(model_ref="m"),
                      "also_good": LLMProfile(model_ref="m")},
        ),
        plugins=PluginsConfig(servers={"an_agent": MCPConfig(
            agent_config=AgentConfig(llm_profile=chain,
                                     llm_profile_advanced=advanced))}),
    )


def _errors(caplog, config):
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        _report_unknown_llm_profiles(config)
    return [r.getMessage() for r in caplog.records]


class TestUnknownChainMembersAreReported:
    def test_a_typo_in_a_fallback_link_is_named(self, caplog):
        messages = _errors(caplog, _config(["good", "tpyo"]))
        assert messages, "an unknown fallback profile went unreported"
        assert "tpyo" in messages[0]
        assert "an_agent" in messages[0]
        assert "fallback" in messages[0]

    def test_a_typo_in_the_primary_says_the_agent_will_not_start(self, caplog):
        messages = _errors(caplog, _config(["tpyo", "good"]))
        assert messages and "WILL NOT START" in messages[0]

    def test_the_advanced_chain_is_checked_too(self, caplog):
        messages = _errors(caplog, _config(["good"], advanced=["adv_tpyo"]))
        assert messages and "adv_tpyo" in messages[0]

    def test_a_healthy_config_is_silent(self, caplog):
        """Counter-check: a guard that always fires is noise, and noise is
        how the real line gets missed."""
        assert _errors(caplog, _config(["good", "also_good"])) == []

    def test_a_config_without_profiles_is_silent(self, caplog):
        """Partial configs (tests, fragments) have nothing to compare
        against — reporting every agent there would be a false alarm."""
        config = _config(["good"])
        config.llm_system.profiles = {}
        assert _errors(caplog, config) == []

    def test_load_settings_actually_runs_the_check(self, monkeypatch):
        """The tests above call the pass directly, so they would all stay
        green if the call in load_settings disappeared — measured: dropping
        that one line left this file passing. A guard nobody invokes is not
        a guard."""
        from agent_system.config import settings as settings_module

        called = []
        monkeypatch.setattr(settings_module, "_report_unknown_llm_profiles",
                            lambda cfg: called.append(cfg))
        settings_module.load_settings()
        assert called, "load_settings no longer checks the LLM profile chains"

    def test_the_shipped_config_is_clean(self):
        """The measurement behind the guard: 206 agent configs, 77 profiles,
        zero broken links today. If this fails, a real config regressed."""
        from agent_system.config.settings import load_settings

        config = load_settings()
        profiles = set(config.llm_system.profiles)
        # Anchors: an empty config would make `broken` empty too, and this
        # test would pass having looked at nothing.
        assert len(profiles) > 50, f"only {len(profiles)} profiles loaded"
        assert len(config.plugins.servers or {}) > 100, "agent configs missing"
        broken = {}
        for name, server in (config.plugins.servers or {}).items():
            agent_cfg = getattr(server, "agent_config", None)
            if agent_cfg is None:
                continue
            unknown = [p for p in agent_cfg.available_llm_profiles
                       if p not in profiles]
            if unknown:
                broken[name] = unknown
        assert not broken, f"agents point at nonexistent LLM profiles: {broken}"
