"""Tests für per-Agent LLM-Parameter-Overrides (agent_config.llm_params).

Feature: Agent-yamls können LLM-Parameter (thinking_level, max_tokens, …)
über den referenzierten llm_system.models-Eintrag legen, statt für jede
Kombination einen eigenen Model-Eintrag anzulegen. Anwendung zentral in
resolve_llm_config_for_agent(); Identitäts-Felder (provider/model/…) sind
gesperrt; Fallback-Profile laufen bewusst ohne Overrides (Call-Site-Ebene).
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
            llm_params={"thinking_level": "ultra"},
        )
        kwargs = resolve_llm_config_for_agent(cfg, agent)
        assert kwargs["thinking_level"] == "ultra"

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
