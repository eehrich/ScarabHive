"""Tests for the configurable ``temperature`` parameter.

Background: until 2026-07 the sampling temperature could not be set at all on
the framework side — measured, all prose-converter requests ran with the
provider default (~1.0), i.e. full sampling variance for a mechanical copy
task. The tests secure the three edges where such a pass-through typically
breaks:

1. ``0.0`` is a VALID value and must not count as "not set".
2. ``None`` must NOT land in ``extra_params``: without a configured value
   no sampling field goes to the provider.
3. Reasoning models reject the param → with ``thinking_level``/-budget
   it is not sent (instead of risking 400s).
"""

from __future__ import annotations

import pytest

import agent_system.llm.registry as _registry
from agent_system.config.models import LLMModelConfig


def make_llm(**kwargs):
    """Real construction path — conftest.py replaces ``registry.build_client``
    globally with a fake so bootstrap code never builds real clients. THESE
    tests need the real wiring; the original is kept as
    ``_orig_build_client`` (conftest.py)."""
    fn = getattr(_registry, "_orig_build_client", None) or _registry.build_client
    return fn(LLMModelConfig(**kwargs))


class TestConfigField:
    def test_zero_is_valid(self):
        assert LLMModelConfig(provider="openai_httpx", model="m", temperature=0.0).temperature == 0.0

    def test_default_is_none(self):
        assert LLMModelConfig(provider="openai_httpx", model="m").temperature is None

    def test_non_numeric_rejected(self):
        with pytest.raises(Exception):
            LLMModelConfig(provider="openai_httpx", model="m", temperature="heiss")


class TestClientWiring:
    @pytest.mark.parametrize("value", [0.0, 0.2, 1.0])
    def test_httpx_receives_value(self, value):
        client = make_llm(provider="openai_httpx", model="m", api_key="k", temperature=value)
        assert client.temperature == value

    def test_httpx_without_value_is_none(self):
        client = make_llm(provider="openai_httpx", model="m", api_key="k")
        assert client.temperature is None

    def test_gemini_without_value_has_none(self):
        client = make_llm(provider="gemini", model="g", api_key="k")
        assert "temperature" not in client.extra_params

    def test_gemini_receives_value(self):
        client = make_llm(provider="gemini", model="g", api_key="k", temperature=0.2)
        assert client.extra_params["temperature"] == 0.2

    def test_responses_client_receives_value(self):
        client = make_llm(
            provider="openai_responses", model="gpt-5.6", api_key="k", temperature=0.2)
        assert client.temperature == 0.2

    # Each provider has its own hand-over channel — the value must not get
    # lost in any of them (and without configuration must show up in none).
    PROVIDERS = [
        ("openai_httpx", {}, "attr"),
        ("openai_responses", {}, "attr"),
        ("anthropic", {}, "extra_params"),
        ("gemini", {}, "extra_params"),
        ("gemini_sdk", {}, "extra_params"),
        ("openai", {}, "_default_extra"),
        ("ollama", {"ollama_mode": "native"}, "_options"),
        ("ollama", {"ollama_mode": "openai_compat"}, "_default_extra"),
    ]

    @staticmethod
    def _channel_value(client, channel):
        if channel == "attr":
            return getattr(client, "temperature", None)
        return (getattr(client, channel, None) or {}).get("temperature")

    @pytest.mark.parametrize("provider,extra,channel", PROVIDERS)
    def test_value_reaches_every_provider(self, provider, extra, channel):
        client = make_llm(provider=provider, model="m", api_key="k", temperature=0.2, **extra)
        assert self._channel_value(client, channel) == 0.2

    @pytest.mark.parametrize("provider,extra,channel", PROVIDERS)
    def test_no_value_means_provider_default(self, provider, extra, channel):
        client = make_llm(provider=provider, model="m", api_key="k", **extra)
        assert self._channel_value(client, channel) is None


class TestPayload:
    def _payload(self, **kwargs) -> dict:
        client = make_llm(provider="openai_httpx", model="m", api_key="k", **kwargs)
        # The payload is built inline in _make_request_non_streaming
        # (no public builder). So what is checked is the state the payload
        # branch reads, plus the reasoning suppression.
        return {
            "temperature": client.temperature,
            "thinking_level": client.thinking_level,
            "thinking_budget": client.thinking_budget,
        }

    def test_temperature_kept_for_plain_model(self):
        state = self._payload(temperature=0.0)
        assert state["temperature"] == 0.0
        assert state["thinking_level"] is None and state["thinking_budget"] is None

    def test_reasoning_model_keeps_thinking_level(self):
        state = self._payload(temperature=0.2, thinking_level="high")
        # The payload branch suppresses temperature exactly when one of the
        # reasoning fields is set.
        assert state["thinking_level"] == "high"
        assert state["temperature"] == 0.2  # stored, but not sent

    # These two tests used to check ``_build_payload_for_test`` — a method
    # that exists NOWHERE. The hasattr branch therefore always fell back to
    # "only check the state", and the actual guard (temperature NOT in the
    # payload for reasoning models) was completely untested. The real builder
    # is called ``_build_payload``.

    def test_responses_payload_omits_temperature_for_reasoning(self):
        # The o-/gpt-5.x series answers with a 400 if temperature is included.
        client = make_llm(provider="openai_responses", model="gpt-5.6", api_key="k",
                          temperature=0.2, thinking_level="high")
        payload = client._build_payload([{"role": "user", "content": "hi"}], None)
        assert "temperature" not in payload, payload
        assert payload.get("reasoning") == {"effort": "high"}

    def test_responses_payload_keeps_temperature_without_reasoning(self):
        client = make_llm(provider="openai_responses", model="gpt-4o", api_key="k",
                          temperature=0.0)
        payload = client._build_payload([{"role": "user", "content": "hi"}], None)
        # 0.0 is a VALID value — the guard checks for None, not for falsy
        assert payload["temperature"] == 0.0
        assert "reasoning" not in payload

    def test_responses_payload_omits_temperature_when_unset(self):
        client = make_llm(provider="openai_responses", model="gpt-4o", api_key="k")
        payload = client._build_payload([{"role": "user", "content": "hi"}], None)
        assert "temperature" not in payload
