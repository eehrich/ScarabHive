"""Plumbing-Tests für reasoning_details_mode: yaml-Config → LLM-Client-Attribut.

Das Feld steuert im httpx-Client den reasoning_details-Round-Trip
(keep_last default / keep_all für OpenAI-Reasoning-Ketten / strip).
Diese Tests pinnen die komplette Kette über BEIDE Factory-Pfade —
der zweite Pfad (LLMFactory-Default-Client) hatte das Feld initial
verloren (stiller Fallback auf keep_last trotz Config).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
)
from agent_system.llm.factory import resolve_llm_config_for_agent
from agent_system.llm.httpx_client import HTTPXOpenAIClient


def _system_config() -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            profiles={"p": LLMProfile(model_ref="m-keepall"),
                      "p-default": LLMProfile(model_ref="m-default")},
            models={
                "m-keepall": LLMModelConfig(
                    provider="openai_httpx",
                    model="openai/gpt-5.6-terra",
                    reasoning_details_mode="keep_all",
                ),
                "m-default": LLMModelConfig(
                    provider="openai_httpx",
                    model="google/gemini-3.5-flash",
                ),
            },
        ),
    )


class TestResolvePlumbing:
    def test_mode_lands_in_llm_kwargs(self):
        kwargs = resolve_llm_config_for_agent(
            _system_config(), AgentConfig(llm_profile="p"))
        assert kwargs["reasoning_details_mode"] == "keep_all"

    def test_unset_mode_absent_from_llm_kwargs(self):
        kwargs = resolve_llm_config_for_agent(
            _system_config(), AgentConfig(llm_profile="p-default"))
        assert kwargs.get("reasoning_details_mode") is None


class TestClientDefault:
    def test_client_default_is_keep_last(self):
        c = HTTPXOpenAIClient(model="google/gemini-3.5-flash", api_key="x",
                              base_url="https://openrouter.ai/api/v1")
        assert c.reasoning_details_mode == "keep_last"

    def test_client_receives_configured_mode(self):
        c = HTTPXOpenAIClient(model="openai/gpt-5.6-terra", api_key="x",
                              base_url="https://openrouter.ai/api/v1",
                              reasoning_details_mode="keep_all")
        assert c.reasoning_details_mode == "keep_all"


class TestTheOneForwardingPathCarriesTheMode:
    """This used to pin that BOTH make_kwargs blocks in factory.py forward the
    field — the two copies had drifted apart once. There is now exactly ONE
    construction path (_build_client with _FORWARDED_FIELDS), so the "copy
    forgotten" failure class is structurally gone.

    The membership assertion below is deliberately weak on its own — a list
    checked against a copy of its own content. The BEHAVIOURAL guarantee lives
    in test_plugin_llm_clients_full_path.py::TestEveryResolvedFieldReachesMakeLlm,
    which records the real make_llm call and dies if the field stops arriving.
    What this file adds is the anti-drift half: no second hand-written block.
    """

    def test_the_field_is_forwarded(self):
        from agent_system.llm import factory
        assert "reasoning_details_mode" in factory._FORWARDED_FIELDS, (
            "reasoning_details_mode missing from _FORWARDED_FIELDS - silent "
            "keep_last fallback despite a keep_all config"
        )

    def test_no_second_forwarding_copy_reappears(self):
        src = (Path(__file__).parent.parent.parent /
               "src/agent_system/llm/factory.py").read_text(encoding="utf-8")
        assert src.count('make_kwargs["reasoning_details_mode"]') == 0, (
            "a hand-written forwarding block is back - new fields belong in "
            "_FORWARDED_FIELDS, not in a second copy"
        )
