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


class TestBothFactoryMakeKwargsPaths:
    """Beide make_kwargs-Blöcke in factory.py müssen das Feld forwarden.
    Statt die (netzwerkbehafteten) Factory-Funktionen end-to-end zu bauen,
    pinnen wir die Quelltext-Invariante: JEDER make_kwargs-Block, der
    provider_routing forwarded, forwarded auch reasoning_details_mode."""

    def test_every_make_kwargs_block_forwards_mode(self):
        src = (Path(__file__).parent.parent.parent /
               "src/agent_system/llm/factory.py").read_text(encoding="utf-8")
        assert src.count('make_kwargs["provider_routing"]') >= 2, \
            "Vorbedingung: zwei Factory-Pfade erwartet"
        assert (src.count('make_kwargs["reasoning_details_mode"]')
                == src.count('make_kwargs["provider_routing"]')), (
            "Ein make_kwargs-Block forwarded provider_routing aber nicht "
            "reasoning_details_mode — das Feld geht auf diesem Factory-Pfad "
            "verloren (stiller keep_last-Fallback trotz keep_all-Config)."
        )
