"""Tests für per-Agent LLM-Parameter-Overrides (agent_config.llm_params).

Feature: Agent-yamls können LLM-Parameter (thinking_level, max_tokens, …)
über den referenzierten llm_system.models-Eintrag legen, statt für jede
Kombination einen eigenen Model-Eintrag anzulegen. Anwendung zentral in
resolve_llm_config_for_agent(); Identitäts-Felder (provider/model/…) sind
gesperrt. Fallbacks laufen mit DERSELBEN Semantik wie das Primärmodell:
"*"/Flat gilt für die ganze Kette, exakter Eintrag gewinnt
(_create_fallback_llm reicht llm_params unverändert an
create_llm_from_profile durch — eine Auflösungsstelle, kein Doppel-Code).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    resolve_llm_params,
)
from agent_system.llm.factory import agent_params_for_profile, resolve_llm_config_for_agent


def _system_config() -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            profiles={
                "test-profile": LLMProfile(model_ref="test-model"),
                "advanced-profile": LLMProfile(model_ref="advanced-model"),
            },
            models={
                "test-model": LLMModelConfig(
                    provider="openai_httpx",
                    model="gpt-test",
                    thinking_level="high",
                    max_tokens=32000,
                    service_tier="flex",
                ),
                "advanced-model": LLMModelConfig(
                    provider="openai_httpx",
                    model="gpt-advanced",
                ),
            },
        ),
    )


class TestResolveAppliesLlmParams:
    def test_overrides_applied(self):
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile="test-profile",
            llm_params={"thinking_level": "low", "max_tokens": 8000},
        )
        resolved = resolve_llm_config_for_agent(cfg, agent)
        assert resolved.spec.thinking_level == "low"
        assert resolved.spec.max_tokens == 8000
        # Nicht überschriebene Felder bleiben vom Basis-Modell
        assert resolved.spec.service_tier == "flex"
        assert resolved.spec.model == "gpt-test"

    def test_can_set_field_unset_in_base(self):
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile="advanced-profile",
            llm_params={"thinking_level": "xhigh"},
        )
        resolved = resolve_llm_config_for_agent(cfg, agent)
        assert resolved.spec.thinking_level == "xhigh"

    def test_no_params_is_noop(self):
        cfg = _system_config()
        agent = AgentConfig(llm_profile="test-profile")
        resolved = resolve_llm_config_for_agent(cfg, agent)
        assert resolved.spec.thinking_level == "high"
        assert resolved.spec.max_tokens == 32000

    def test_shared_model_registry_not_mutated(self):
        """Der Override darf NIE in den geteilten models-Eintrag zurückschreiben —
        sonst erbt der nächste Agent mit demselben model_ref die Fremd-Params."""
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile="test-profile",
            llm_params={"thinking_level": "minimal", "max_tokens": 500},
        )
        resolve_llm_config_for_agent(cfg, agent)
        base = cfg.llm_system.models["test-model"]
        assert base.thinking_level == "high"
        assert base.max_tokens == 32000

        # Zweiter Agent ohne Params sieht das Original
        other = AgentConfig(llm_profile="test-profile")
        resolved = resolve_llm_config_for_agent(cfg, other)
        assert resolved.spec.thinking_level == "high"


class TestAgentConfigValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError, match="keys not allowed"):
            AgentConfig(llm_profile="x", llm_params={"totally_unknown": 1})

    def test_identity_fields_protected(self):
        for key in ("provider", "model", "api_key", "base_url", "batch_provider", "ollama_mode"):
            with pytest.raises(ValidationError, match="identity fields .* are locked"):
                AgentConfig(llm_profile="x", llm_params={key: "hijack"})

    def test_value_validated_against_model_schema(self):
        # thinking_level ist ein Literal — ungültiger Wert muss beim
        # Config-Load knallen, nicht erst beim ersten LLM-Call.
        with pytest.raises(ValidationError):
            AgentConfig(llm_profile="x", llm_params={"thinking_level": "mega"})
        with pytest.raises(ValidationError):
            AgentConfig(llm_profile="x", llm_params={"max_tokens": "viele"})

    def test_valid_params_accepted(self):
        a = AgentConfig(
            llm_profile="x",
            llm_params={
                "thinking_level": "max",
                "max_tokens": 12000,
                "service_tier": "flex",
                "include_thoughts": True,
                "request_timeout": 300,
            },
        )
        assert a.llm_params["thinking_level"] == "max"

    def test_empty_dict_normalized_to_none(self):
        a = AgentConfig(llm_profile="x", llm_params={})
        assert a.llm_params is None


class TestEveryRunOverrideCarriesThem:
    """Anti-drift: the same line was missing at five call sites at once.

    Every place in the core that builds the client a RUN goes out on --
    the agent's own, its fallback, its advanced model, and the overrides
    (API, agent-cli, agent-run, /model, a caller's switch) -- has to hand
    create_llm_from_profile the agent's llm_params. The overrides go through
    llm.factory.override_for_profile, which takes the agent's config by
    construction; a call to it has to pass one. Listing the call sites is
    the point: the hole was that one of them forgot, and a scan finds the
    next one that does.

    Not in here: the helper models plugins build for themselves (a
    judge, a summarizer, an evaluator). Those do not run the agent's
    conversation, so the agent's params are none of their business.
    """

    RUN_CLIENT_MODULES = (
        "agent_system/app.py",
        "agent_system/agent_cli.py",
        "agent_system/agent_run.py",
        "agent_system/cli_utils/chat/agent_setup.py",
        "agent_system/servers/agent/server.py",
        "agent_system/llm/factory.py",
    )

    #: What the value has to be built from. A bare ``llm_params=None`` or the
    #: caller's typed params alone is the bug this test exists for -- the
    #: keyword being PRESENT says nothing.
    FROM_THE_AGENT = ("agent_params_for_profile", "agent_config")

    def test_no_run_client_is_built_without_the_agents_llm_params(self):
        import ast

        src = Path(__file__).parent.parent.parent / "src"
        without = []
        per_module = {}
        for relative in self.RUN_CLIENT_MODULES:
            path = src / relative
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name == "override_for_profile":
                    per_module[relative] = per_module.get(relative, 0) + 1
                    # (config, agent_config, profile, ...): the agent's config
                    # is the second argument, and None would carry nothing.
                    passed = (node.args[1] if len(node.args) > 1 else
                              next((kw.value for kw in node.keywords if kw.arg == "agent_config"), None))
                    given = ast.get_source_segment(source, passed) if passed is not None else ""
                    if not given or given == "None" or "agent_config" not in given:
                        without.append(f"{relative}:{node.lineno} ({given or 'no agent_config'})")
                    continue
                if name != "create_llm_from_profile":
                    continue
                per_module[relative] = per_module.get(relative, 0) + 1
                passed = next((kw for kw in node.keywords if kw.arg == "llm_params"), None)
                given = ast.get_source_segment(source, passed.value) if passed else ""
                if not any(mark in (given or "") for mark in self.FROM_THE_AGENT):
                    without.append(f"{relative}:{node.lineno} ({given or 'no llm_params'})")
        # Every module in the list has to carry a call -- a stale entry would
        # otherwise make the scan look wider than it is.
        empty = [m for m in self.RUN_CLIENT_MODULES if not per_module.get(m)]
        assert not empty, f"fixture: no run client built in {empty} -- the list went stale"
        assert not without, ("these build a run's client without the agent's llm_params: "
                             + ", ".join(without))


class TestParamsOfAnOverriddenProfile:
    """Ein Override waehlt ein anderes MODELL, nicht einen anderen Agenten.

    Was der Agent ueber jedes Modell sagt ("*"/flach), muss ihn deshalb auch
    auf ein Profil begleiten, das der Nutzer im Panel oder per /model waehlt
    — so wie _create_fallback_llm es in den Fallback traegt. Ohne das fiel
    der context_window-Deckel des coder lautlos weg, und seine Aufrufe
    wurden gegen die 272000 des Modells gezaehlt statt gegen seine 200000.
    """

    def test_the_agents_star_params_reach_the_chosen_profile(self):
        agent = AgentConfig(llm_profile=["test-profile"],
                            llm_params={"*": {"context_window": 200000}})
        assert agent_params_for_profile(agent, "fremdes-profil") == {"context_window": 200000}

    def test_the_entry_of_that_profile_wins_over_the_star(self):
        agent = AgentConfig(
            llm_profile=["test-profile"], llm_profile_advanced=["advanced-profile"],
            llm_params={"*": {"max_tokens": 8000}, "advanced-profile": {"max_tokens": 99}})
        assert agent_params_for_profile(agent, "advanced-profile") == {"max_tokens": 99}

    def test_what_the_caller_typed_wins_over_the_agents(self):
        agent = AgentConfig(llm_profile=["test-profile"],
                            llm_params={"*": {"max_tokens": 8000, "context_window": 200000}})
        assert agent_params_for_profile(agent, "test-profile", {"max_tokens": 16384}) == {
            "max_tokens": 16384, "context_window": 200000,
        }

    def test_an_agent_without_params_keeps_the_callers(self):
        assert agent_params_for_profile(None, "test-profile", {"max_tokens": 7}) == {"max_tokens": 7}
        assert agent_params_for_profile(None, "test-profile") is None


class TestKeyedLlmParams:
    """Profil-gekeyte Form: {profil: {param: wert}} — Params kleben am
    Modell, nicht am Slot. "*" gilt für beide Ketten-Primärmodelle,
    der spezifische Eintrag gewinnt."""

    KEYED = {
        "*": {"max_tokens": 8000, "thinking_level": "low"},
        "advanced-profile": {"thinking_level": "xhigh"},
    }

    def test_resolve_star_merges_specific_wins(self):
        assert resolve_llm_params(self.KEYED, "advanced-profile") == {
            "max_tokens": 8000, "thinking_level": "xhigh",
        }
        assert resolve_llm_params(self.KEYED, "test-profile") == {
            "max_tokens": 8000, "thinking_level": "low",
        }

    def test_resolve_no_entry_no_star_is_none(self):
        params = {"advanced-profile": {"thinking_level": "xhigh"}}
        assert resolve_llm_params(params, "test-profile") is None

    def test_resolve_flat_form_passthrough(self):
        flat = {"thinking_level": "low", "max_tokens": 8000}
        assert resolve_llm_params(flat, "irgendein-profil") == flat
        assert resolve_llm_params(None, "x") is None

    def test_keyed_applies_only_to_matching_primary(self):
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile=["test-profile"],
            llm_profile_advanced=["advanced-profile"],
            llm_params={"advanced-profile": {"thinking_level": "xhigh"}},
        )
        # resolve löst das Default-Profil auf → Advanced-Params greifen NICHT
        resolved = resolve_llm_config_for_agent(cfg, agent)
        assert resolved.spec.thinking_level == "high"  # Basis-Modell unverändert

    def test_keyed_star_applies_to_default(self):
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile=["test-profile"],
            llm_profile_advanced=["advanced-profile"],
            llm_params=self.KEYED,
        )
        resolved = resolve_llm_config_for_agent(cfg, agent)
        assert resolved.spec.thinking_level == "low"
        assert resolved.spec.max_tokens == 8000

    def test_resolved_keyed_valid_as_temp_config(self):
        # create_llm_from_profile reduziert gekeyte Params VOR der temp-
        # AgentConfig — das Ergebnis muss als Flat-Form validieren.
        flat = resolve_llm_params(self.KEYED, "advanced-profile")
        a = AgentConfig(llm_profile="advanced-profile", llm_params=flat)
        assert a.llm_params["thinking_level"] == "xhigh"


class TestKeyedLlmParamsValidation:
    def test_mixed_form_rejected(self):
        with pytest.raises(ValidationError, match="mixes params"):
            AgentConfig(
                llm_profile="x",
                llm_params={"thinking_level": "low", "x": {"max_tokens": 1}},
            )

    def test_unknown_profile_key_rejected(self):
        # Keys that appear in NO chain (typo, stale entry after a chain
        # rewrite) raise here. The real config load drops them and logs
        # instead — see TestStaleLlmParamKeysAreDroppedLoudly.
        with pytest.raises(ValidationError, match="no.*LLM chain"):
            AgentConfig(
                llm_profile=["test-profile", "fallback-profile"],
                llm_profile_advanced=["advanced-profile"],
                llm_params={"typo-profile": {"max_tokens": 100}},
            )

    def test_fallback_profile_key_accepted(self):
        # Gekeyte Einträge für Fallback-Profile sind gültig — Fallbacks
        # laufen mit derselben llm_params-Semantik wie das Primärmodell.
        a = AgentConfig(
            llm_profile=["test-profile", "fallback-profile"],
            llm_profile_advanced=["advanced-profile", "adv-fallback"],
            llm_params={
                "fallback-profile": {"max_tokens": 100},
                "adv-fallback": {"thinking_level": "high"},
            },
        )
        assert set(a.llm_params) == {"fallback-profile", "adv-fallback"}

    def test_fallback_resolution_same_semantics_as_primary(self):
        # Fallback-Auflösung = identische resolve_llm_params-Semantik:
        # "*" gilt auch für Fallback-Profile, exakter Eintrag gewinnt.
        params = {
            "*": {"max_tokens": 8000, "thinking_level": "max"},
            "fallback-profile": {"thinking_level": "high"},
        }
        assert resolve_llm_params(params, "fallback-profile") == {
            "max_tokens": 8000, "thinking_level": "high",
        }
        assert resolve_llm_params(params, "anderes-fallback") == {
            "max_tokens": 8000, "thinking_level": "max",
        }

    def test_primary_and_star_keys_accepted(self):
        a = AgentConfig(
            llm_profile=["test-profile", "fallback-profile"],
            llm_profile_advanced=["advanced-profile"],
            llm_params={
                "*": {"max_tokens": 8000},
                "test-profile": {"thinking_level": "low"},
                "advanced-profile": {"thinking_level": "xhigh"},
            },
        )
        assert set(a.llm_params) == {"*", "test-profile", "advanced-profile"}

    def test_keyed_subdict_protected_field_rejected(self):
        with pytest.raises(ValidationError, match="identity fields"):
            AgentConfig(
                llm_profile=["test-profile"],
                llm_params={"*": {"provider": "hijack"}},
            )

    def test_keyed_subdict_bad_value_rejected(self):
        with pytest.raises(ValidationError):
            AgentConfig(
                llm_profile=["test-profile"],
                llm_params={"test-profile": {"thinking_level": "mega"}},
            )

    def test_keyed_scalar_value_rejected(self):
        with pytest.raises(ValidationError, match="keys not allowed"):
            AgentConfig(llm_profile="x", llm_params={"totally_unknown": 1})

    def test_flat_typo_next_to_valid_param_gets_precise_message(self):
        # Tippfehler neben gültigem Param darf NICHT als "Mischform"
        # fehldiagnostiziert werden — präzise Unknown-Key-Meldung
        with pytest.raises(ValidationError, match="keys not allowed.*max_toknes"):
            AgentConfig(
                llm_profile="x",
                llm_params={"thinking_level": "low", "max_toknes": 8000},
            )

    def test_profile_names_must_not_shadow_model_fields(self):
        # Profilnamen sind llm_params-Keys — Kollision mit Feldnamen wäre
        # dort unadressierbar → an der Wurzel (llm_system.profiles) verboten
        with pytest.raises(ValidationError, match="kollidieren"):
            LLMSystemConfig(
                profiles={"max_tokens": LLMProfile(model_ref="test-model")},
                models={"test-model": LLMModelConfig(provider="ollama", model="m")},
            )
