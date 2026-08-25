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
from agent_system.llm.factory import resolve_llm_config_for_agent


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
        kwargs = resolve_llm_config_for_agent(cfg, agent)
        assert kwargs["thinking_level"] == "low"
        assert kwargs["max_tokens"] == 8000
        # Nicht überschriebene Felder bleiben vom Basis-Modell
        assert kwargs["service_tier"] == "flex"
        assert kwargs["model"] == "gpt-test"

    def test_can_set_field_unset_in_base(self):
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile="advanced-profile",
            llm_params={"thinking_level": "xhigh"},
        )
        kwargs = resolve_llm_config_for_agent(cfg, agent)
        assert kwargs["thinking_level"] == "xhigh"

    def test_no_params_is_noop(self):
        cfg = _system_config()
        agent = AgentConfig(llm_profile="test-profile")
        kwargs = resolve_llm_config_for_agent(cfg, agent)
        assert kwargs["thinking_level"] == "high"
        assert kwargs["max_tokens"] == 32000

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
        kwargs = resolve_llm_config_for_agent(cfg, other)
        assert kwargs["thinking_level"] == "high"


class TestAgentConfigValidation:
    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError, match="nicht erlaubte Keys"):
            AgentConfig(llm_profile="x", llm_params={"totally_unknown": 1})

    def test_identity_fields_protected(self):
        for key in ("provider", "model", "api_key", "base_url", "batch_provider", "ollama_mode"):
            with pytest.raises(ValidationError, match="nicht erlaubte Keys"):
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
        kwargs = resolve_llm_config_for_agent(cfg, agent)
        assert kwargs["thinking_level"] == "high"  # Basis-Modell unverändert

    def test_keyed_star_applies_to_default(self):
        cfg = _system_config()
        agent = AgentConfig(
            llm_profile=["test-profile"],
            llm_profile_advanced=["advanced-profile"],
            llm_params=self.KEYED,
        )
        kwargs = resolve_llm_config_for_agent(cfg, agent)
        assert kwargs["thinking_level"] == "low"
        assert kwargs["max_tokens"] == 8000

    def test_resolved_keyed_valid_as_temp_config(self):
        # create_llm_from_profile reduziert gekeyte Params VOR der temp-
        # AgentConfig — das Ergebnis muss als Flat-Form validieren.
        flat = resolve_llm_params(self.KEYED, "advanced-profile")
        a = AgentConfig(llm_profile="advanced-profile", llm_params=flat)
        assert a.llm_params["thinking_level"] == "xhigh"


class TestKeyedLlmParamsValidation:
    def test_mixed_form_rejected(self):
        with pytest.raises(ValidationError, match="Mischform"):
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
        with pytest.raises(ValidationError, match="gesperrt"):
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
        with pytest.raises(ValidationError, match="nicht erlaubte Keys"):
            AgentConfig(llm_profile="x", llm_params={"totally_unknown": 1})

    def test_flat_typo_next_to_valid_param_gets_precise_message(self):
        # Tippfehler neben gültigem Param darf NICHT als "Mischform"
        # fehldiagnostiziert werden — präzise Unknown-Key-Meldung
        with pytest.raises(ValidationError, match="nicht erlaubte Keys.*max_toknes"):
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
                models={"test-model": LLMModelConfig(provider="mock", model="m")},
            )
