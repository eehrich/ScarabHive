"""Plumbing tests for reasoning_details_mode: yaml config -> LLM client attribute.

In the httpx client the field controls the reasoning_details round trip
(keep_last default / keep_all for OpenAI reasoning chains / strip).
These tests pin the complete chain across BOTH factory paths --
the second path (LLMFactory default client) had initially lost the field
(silent fallback to keep_last despite the config).
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
from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient


def _system_config() -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            profiles={"p": LLMProfile(model_ref="m-keepall"),
                      "p-default": LLMProfile(model_ref="m-default")},
            models={
                "m-keepall": LLMModelConfig(
                    provider="openai_httpx",
                    model="openai/gpt-5.6-terra",
                    api_key="x",
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
    def test_mode_lands_in_the_spec(self):
        resolved = resolve_llm_config_for_agent(
            _system_config(), AgentConfig(llm_profile="p"))
        assert resolved.spec.reasoning_details_mode == "keep_all"

    def test_unset_mode_stays_unset(self):
        resolved = resolve_llm_config_for_agent(
            _system_config(), AgentConfig(llm_profile="p-default"))
        assert resolved.spec.reasoning_details_mode is None


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


class TestTheWholePathCarriesTheMode:
    """The flattened forwarding list (_FORWARDED_FIELDS) that this class used
    to guard is gone: the resolver hands the WHOLE model config to the
    provider factory, so a field can no longer be lost between resolver and
    client. What remains worth pinning is the end-to-end behaviour."""

    def test_the_mode_survives_resolver_and_factory(self):
        from agent_system.llm import registry
        resolved = resolve_llm_config_for_agent(
            _system_config(), AgentConfig(llm_profile="p"))
        build = getattr(registry, "_orig_build_client", registry.build_client)
        client = build(resolved.spec)
        assert client.reasoning_details_mode == "keep_all", (
            "reasoning_details_mode lost between resolver and client - "
            "silent keep_last fallback despite a keep_all config"
        )
