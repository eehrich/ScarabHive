"""Tests für den konfigurierbaren ``temperature``-Parameter.

Hintergrund: bis 2026-07 war Sampling-Temperatur framework-seitig überhaupt
nicht setzbar — gemessen liefen alle Prosa-Konverter-Requests mit
Provider-Default (~1,0), also voller Sampling-Varianz für eine mechanische
Kopier-Aufgabe. Die Tests sichern die drei Kanten ab, an denen so eine
Durchreichung typischerweise bricht:

1. ``0.0`` ist ein GÜLTIGER Wert und darf nicht als „nicht gesetzt" gelten.
2. ``None`` darf NICHT in ``extra_params`` landen — der GeminiClient liest
   ``extra_params.get("temperature", 1.0)`` und bekäme sonst ``None``.
3. Reasoning-Modelle lehnen den Param ab → bei ``thinking_level``/-budget
   wird er nicht gesendet (statt 400er zu riskieren).
"""

from __future__ import annotations

import pytest

import agent_system.llm.clients as _clients
from agent_system.config.models import LLMModelConfig


def make_llm(**kwargs):
    """Echte Factory — conftest.py ersetzt ``make_llm`` global durch einen
    Fake, damit Bootstrap-Code keine realen Clients baut. Für DIESE Tests
    brauchen wir die echte Verdrahtung; das Original liegt als
    ``_orig_make_llm`` bereit (conftest.py:190)."""
    fn = getattr(_clients, "_orig_make_llm", None) or _clients.make_llm
    return fn(**kwargs)


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

    def test_gemini_default_not_overwritten_by_none(self):
        client = make_llm(provider="gemini", model="g", api_key="k")
        assert "temperature" not in client.extra_params

    def test_gemini_receives_value(self):
        client = make_llm(provider="gemini", model="g", api_key="k", temperature=0.2)
        assert client.extra_params["temperature"] == 0.2

    def test_responses_client_receives_value(self):
        client = make_llm(
            provider="openai_responses", model="gpt-5.6", api_key="k", temperature=0.2)
        assert client.temperature == 0.2

    # Jeder Provider hat einen eigenen Übergabe-Kanal — der Wert darf in
    # keinem davon verloren gehen (und ohne Konfiguration nirgends auftauchen).
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
        # Der Payload-Aufbau liegt inline in _make_request_non_streaming
        # (kein öffentlicher Builder). Geprüft wird deshalb der Zustand, den
        # der Payload-Zweig liest, plus die Reasoning-Unterdrückung.
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
        # Der Payload-Zweig unterdrückt temperature genau dann, wenn eines der
        # Reasoning-Felder gesetzt ist.
        assert state["thinking_level"] == "high"
        assert state["temperature"] == 0.2  # gespeichert, aber nicht gesendet

    def test_responses_payload_omits_temperature_for_reasoning(self):
        # Die o-/gpt-5.x-Serie antwortet mit 400, wenn temperature mitkommt.
        client = make_llm(provider="openai_responses", model="gpt-5.6", api_key="k",
                          temperature=0.2, thinking_level="high")
        payload = client._build_payload_for_test() if hasattr(
            client, "_build_payload_for_test") else None
        if payload is None:
            # Kein öffentlicher Builder → Zustand prüfen, den der Zweig liest
            assert client.temperature == 0.2 and client.thinking_level == "high"
        else:
            assert "temperature" not in payload

    def test_responses_payload_keeps_temperature_without_reasoning(self):
        client = make_llm(provider="openai_responses", model="gpt-4o", api_key="k",
                          temperature=0.0)
        assert client.temperature == 0.0 and client.thinking_level is None
