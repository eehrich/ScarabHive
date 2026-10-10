"""The gateway fields OpenRouter offers and we did not use until 09/2026.

Three things are recorded here:

* The opt-in header ``X-OpenRouter-Metadata``. Without it no response names
  the provider that actually delivered — on the Responses route there is no
  other field for it at all.
* ``session_id`` as a sticky-routing key. OpenRouter's prompt cache is
  backend-local; calls with the same prefix only hit it as long as they land
  on the same backend (measured 01.09.2026: 6/6 calls on one provider with
  session_id, 4 different ones without).
* The pass-through fields ``plugins``, ``prompt_cache_options`` and
  ``safety_identifier`` — all unset by default, because each of them changes
  what the model sees or what it costs.
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

#: As OpenRouter really sends it (live response 01.09.2026, shortened).
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
        """The chat response carries `provider` even without a metadata block."""
        assert openrouter_routing_info({"provider": "Google"}) == {"selected": "Google"}

    def test_an_empty_metadata_block_reports_nothing(self):
        """A block that yields nothing usable must not report an empty
        dict — otherwise the hook always looks like it got an answer."""
        assert openrouter_routing_info({"openrouter_metadata": {}}) is None

    def test_nothing_reported_when_nothing_is_there(self):
        """Important: no empty dict but None — a field that always contains
        something cannot be told apart from a working one."""
        assert openrouter_routing_info({"id": "x", "output": []}) is None

    def test_metadata_without_a_selection_falls_back(self):
        body = {"provider": "Novita",
                "openrouter_metadata": {"endpoints": {"available": [
                    {"provider": "Novita", "selected": False}]}}}
        assert openrouter_routing_info(body)["selected"] == "Novita"


class TestConfiguredButNotSent:
    def test_plugins_at_a_foreign_endpoint_are_reported(self, caplog):
        """Configured and yet dropped is the silent drift this field is
        meant to make visible."""
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
        """A foreign endpoint gets no gateway header."""
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
        """`session_id` is gateway vocabulary; OpenAI rejects unknown
        parameters with a 400."""
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
        """`prompt_cache_options` is an OpenAI field (GPT-5.6+), not a
        gateway field — it may also go directly to OpenAI."""
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
            assert key not in payload, f"{key} must not travel along without configuration"


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
        """The gateway attaches the metadata to the LAST chunk — whoever reads
        only the first never sees anything."""
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
