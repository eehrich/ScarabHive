"""Die Gateway-Felder, die OpenRouter kann und wir bis 09/2026 nicht nutzten.

Drei Dinge werden hier festgehalten:

* Der Opt-in-Header ``X-OpenRouter-Metadata``. Ohne ihn nennt keine Antwort
  den Anbieter, der wirklich geliefert hat — auf der Responses-Route gibt es
  gar kein anderes Feld dafuer.
* ``session_id`` als Sticky-Routing-Schluessel. OpenRouters Prompt-Cache ist
  backend-lokal; Aufrufe mit gleichem Praefix treffen ihn nur, solange sie
  beim selben Backend landen (gemessen 01.09.2026: 6/6 Aufrufe auf einem
  Anbieter mit session_id, 4 verschiedene ohne).
* Die Durchreichen-Felder ``plugins``, ``prompt_cache_options`` und
  ``safety_identifier`` — alle unbelegt per Default, weil jedes davon
  aendert, was das Modell sieht oder kostet.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.models import ChatMessage
from plugins.llm_openai_compat.httpx_client import (
    HTTPXOpenAIClient,
    openrouter_routing_info,
)
from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

OR_URL = "https://openrouter.ai/api/v1"
OPENAI_URL = "https://api.openai.com/v1"

#: Wie OpenRouter es wirklich schickt (Live-Antwort 01.09.2026, gekuerzt).
META = {
    "requested": "deepseek/deepseek-v4-flash-latest", "strategy": "latest",
    "region": "FRA", "attempt": 1, "is_byok": False,
    "summary": "available=2, selected=DeepInfra",
    "endpoints": {"total": 2, "available": [
        {"provider": "DeepInfra", "selected": True},
        {"provider": "StreamLake", "selected": False}]},
}


def _httpx(base_url=OR_URL, **kw) -> HTTPXOpenAIClient:
    return HTTPXOpenAIClient(model="deepseek/x", api_key="k",
                             base_url=base_url, **kw)


def _responses(base_url=OR_URL, **kw) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(model="openai/gpt-5.6", api_key="k",
                                 base_url=base_url, **kw)


class TestTheRoutingReader:
    def test_it_names_the_backend_that_answered(self):
        info = openrouter_routing_info({"openrouter_metadata": META})
        assert info["selected"] == "DeepInfra"
        assert info["available"] == ["DeepInfra", "StreamLake"]
        assert info["attempt"] == 1
        assert info["strategy"] == "latest"
        assert info["region"] == "FRA"

    def test_the_chat_routes_plain_provider_field_is_enough(self):
        """Die Chat-Antwort traegt `provider` auch ohne Metadaten-Block."""
        assert openrouter_routing_info({"provider": "Google"}) == {"selected": "Google"}

    def test_an_empty_metadata_block_reports_nothing(self):
        """Ein Block, aus dem nichts Brauchbares faellt, darf kein leeres
        Dict melden — sonst sieht der Hook immer nach Antwort aus."""
        assert openrouter_routing_info({"openrouter_metadata": {}}) is None

    def test_nothing_reported_when_nothing_is_there(self):
        """Wichtig: kein leeres Dict, sondern None — ein Feld, das immer
        etwas enthaelt, ist von einem funktionierenden nicht zu
        unterscheiden."""
        assert openrouter_routing_info({"id": "x", "output": []}) is None

    def test_metadata_without_a_selection_falls_back(self):
        body = {"provider": "Novita",
                "openrouter_metadata": {"endpoints": {"available": [
                    {"provider": "Novita", "selected": False}]}}}
        assert openrouter_routing_info(body)["selected"] == "Novita"


class TestConfiguredButNotSent:
    def test_plugins_at_a_foreign_endpoint_are_reported(self, caplog):
        """Konfiguriert und trotzdem verworfen ist die stille Drift, die
        dieses Feld sichtbar machen soll."""
        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            _httpx(base_url=OPENAI_URL, plugins=[{"id": "moderation"}])
        assert "NOT sent" in caplog.text
        caplog.clear()
        with caplog.at_level(_logging.WARNING):
            _responses(base_url=OPENAI_URL, plugins=[{"id": "moderation"}])
        assert "NOT sent" in caplog.text

    def test_the_ordinary_case_stays_quiet(self, caplog):
        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            _httpx(plugins=[{"id": "moderation"}])
            _responses(plugins=[{"id": "moderation"}])
        assert "NOT sent" not in caplog.text


class TestTheOptInHeader:
    def test_httpx_asks_openrouter_for_metadata(self):
        assert _httpx()._headers["X-OpenRouter-Metadata"] == "enabled"

    def test_httpx_does_not_ask_openai(self):
        """Ein fremder Endpunkt bekommt keinen Gateway-Header."""
        assert "X-OpenRouter-Metadata" not in _httpx(base_url=OPENAI_URL)._headers

    def test_responses_asks_openrouter_for_metadata(self):
        assert _responses()._headers()["X-OpenRouter-Metadata"] == "enabled"

    def test_responses_does_not_ask_a_foreign_endpoint(self):
        assert "X-OpenRouter-Metadata" not in _responses(base_url=OPENAI_URL)._headers()


class TestStickyRoutingOnTheChatRoute:
    def test_the_cache_key_doubles_as_the_session(self):
        payload: dict = {}
        _httpx()._apply_gateway_extras(payload, "abc123")
        assert payload["prompt_cache_key"] == "abc123"
        assert payload["session_id"] == "abc123"

    def test_no_key_no_session(self):
        payload: dict = {}
        _httpx()._apply_gateway_extras(payload, None)
        assert "session_id" not in payload and "prompt_cache_key" not in payload

    def test_a_foreign_endpoint_gets_no_session_id(self):
        """`session_id` ist Gateway-Vokabular; OpenAI lehnt unbekannte
        Parameter mit 400 ab."""
        payload: dict = {}
        _httpx(base_url=OPENAI_URL)._apply_gateway_extras(payload, "abc123")
        assert payload["prompt_cache_key"] == "abc123"
        assert "session_id" not in payload

    def test_plugins_only_go_to_the_gateway(self):
        plugins = [{"id": "context-compression", "engine": "middle-out"}]
        here: dict = {}
        _httpx(plugins=plugins)._apply_gateway_extras(here, None)
        assert here["plugins"] == plugins
        elsewhere: dict = {}
        _httpx(base_url=OPENAI_URL, plugins=plugins)._apply_gateway_extras(elsewhere, None)
        assert "plugins" not in elsewhere

    def test_cache_options_travel_to_both(self):
        """`prompt_cache_options` ist ein OpenAI-Feld (GPT-5.6+), kein
        Gateway-Feld — es darf auch direkt zu OpenAI."""
        for url in (OR_URL, OPENAI_URL):
            payload: dict = {}
            _httpx(base_url=url,
                   prompt_cache_options={"mode": "explicit"})._apply_gateway_extras(
                payload, None)
            assert payload["prompt_cache_options"] == {"mode": "explicit"}

    def test_nothing_is_added_when_nothing_is_configured(self):
        payload: dict = {}
        _httpx()._apply_gateway_extras(payload, None)
        assert payload == {}


class TestTheResponsesPayload:
    def _payload(self, **kw) -> dict:
        client = _responses(prompt_cache_key="auto", **kw)
        return client._build_payload([ChatMessage(role="user", content="hi")], None)

    def test_session_id_matches_the_cache_key(self):
        payload = self._payload()
        assert payload["session_id"] == payload["prompt_cache_key"]

    def test_without_a_cache_key_there_is_no_session(self):
        client = _responses()
        payload = client._build_payload([ChatMessage(role="user", content="hi")], None)
        assert "session_id" not in payload

    def test_the_passthrough_fields_arrive(self):
        payload = self._payload(
            plugins=[{"id": "response-healing"}],
            prompt_cache_options={"mode": "explicit"},
            safety_identifier="book-42")
        assert payload["plugins"] == [{"id": "response-healing"}]
        assert payload["prompt_cache_options"] == {"mode": "explicit"}
        assert payload["safety_identifier"] == "book-42"

    def test_unset_means_absent(self):
        payload = self._payload()
        for key in ("plugins", "prompt_cache_options", "safety_identifier"):
            assert key not in payload, f"{key} darf ohne Konfiguration nicht mitreisen"


class _Transport:
    def __init__(self, body):
        self.body = body

    async def post(self, url, json=None, headers=None):
        return httpx.Response(200, json=self.body)


@pytest.fixture
def responses_transport(monkeypatch):
    def install(body):
        transport = _Transport(body)

        class _Fake:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return transport

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(httpx, "AsyncClient", _Fake)
    return install


class TestTheHookSeesTheRouting:
    @pytest.mark.asyncio
    async def test_responses_route_reports_the_backend(self, responses_transport):
        responses_transport({"output": [], "openrouter_metadata": META})
        seen: list[dict] = []
        client = _responses()

        async def record(info):
            seen.append(info)

        client.set_llm_hooks(on_post_response=record)
        await client.chat_tools([ChatMessage(role="user", content="hi")], [])
        assert seen[-1]["routing"]["selected"] == "DeepInfra"

    @pytest.mark.asyncio
    async def test_a_response_without_metadata_reports_none(self, responses_transport):
        responses_transport({"output": []})
        seen: list[dict] = []
        client = _responses()

        async def record(info):
            seen.append(info)

        client.set_llm_hooks(on_post_response=record)
        await client.chat_tools([ChatMessage(role="user", content="hi")], [])
        assert seen[-1]["routing"] is None

    @pytest.mark.asyncio
    async def test_the_stream_reports_it_from_the_last_chunk(self):
        """Der Gateway haengt die Metadaten an den LETZTEN Chunk — wer nur den
        ersten liest, sieht nie etwas."""
        import json as _json

        lines = [
            'data: {"provider":"DeepInfra","choices":[{"delta":{"content":"Hi"}}]}',
            "data: " + _json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                                    "openrouter_metadata": META}),
            "data: [DONE]",
        ]
        payload = ("\n".join(lines) + "\n").encode("utf-8")

        async def chunks():
            yield payload

        response = MagicMock()
        response.aiter_bytes = chunks
        response.status_code = 200
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)

        seen: list[dict] = []
        client = _httpx()

        async def record(info):
            seen.append(info)

        client.set_llm_hooks(on_post_response=record)
        with patch("httpx.AsyncClient.stream", return_value=response):
            async for _ in client.chat_tools_streaming(
                    [ChatMessage(role="user", content="hi")], []):
                pass
        assert seen[-1]["routing"]["selected"] == "DeepInfra"
        assert seen[-1]["routing"]["available"] == ["DeepInfra", "StreamLake"]
