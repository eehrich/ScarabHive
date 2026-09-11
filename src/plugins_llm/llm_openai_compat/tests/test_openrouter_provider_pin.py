"""The OpenRouter backend that served the last turn goes first in provider.order.

Measured 2026-09-11 on Gemini: Vertex refused a history whose last turn AI
Studio had served 102 of 102 times and accepted its own 135 of 135. Every
later turn of such a run paid a 400 before landing on AI Studio anyway, and
each backend switch cost the prompt cache. The pin is shared by all three
OpenRouter routes (httpx chat, Responses, SDK) and derived from ``served_by``
on the history -- one client serves every parallel session of an agent type.

The metadata names a backend by display name, provider.order takes slugs. The
translation is the gateway's own provider list, never a table in code.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.llm.models import ChatMessage
from agent_system.llm.tls import httpx_verify
from plugins_llm.llm_openai_compat import httpx_client
from plugins_llm.llm_openai_compat.httpx_client import (
    HTTPXOpenAIClient,
    openrouter_routing_info,
    routing_pinned_to_last_backend,
)
from plugins_llm.llm_openai_compat.openai_responses_client import OpenAIResponsesClient
from plugins_llm.llm_openrouter.openrouter_sdk_client import OpenRouterSDKClient

OPENROUTER = "https://openrouter.ai/api/v1"
VERTEX_FIRST = {"order": ["google-vertex", "google-ai-studio"], "allow_fallbacks": False}
AI_STUDIO_FIRST = ["google-ai-studio", "google-vertex"]
# GET /providers, excerpt as measured 2026-09-11. Vertex's display name is plain
# "Google": nothing in code could have derived that from the slug.
PROVIDERS = {"data": [
    {"name": "Google", "slug": "google-vertex"},
    {"name": "Google AI Studio", "slug": "google-ai-studio"},
    {"name": "DeepInfra", "slug": "deepinfra"},
    {"name": "StreamLake", "slug": "streamlake"},
]}


def _meta(backend):
    return {"endpoints": {"available": [{"provider": backend, "selected": True}]}}


def _history(backend):
    return [ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="ok", served_by=backend),
            ChatMessage(role="user", content="next")]


@pytest.fixture(autouse=True)
def provider_list(monkeypatch):
    """The list is process-wide state: every test starts with none loaded."""
    monkeypatch.setattr(httpx_client, "_provider_slugs", {})
    monkeypatch.setattr(httpx_client, "_provider_slugs_loaded_at", float("-inf"))


@pytest.fixture
def published(provider_list, monkeypatch):
    """The list as if loaded just now -- for tests about the order, not the load."""
    monkeypatch.setattr(httpx_client, "_provider_slugs",
                        {p["name"]: p["slug"] for p in PROVIDERS["data"]})
    monkeypatch.setattr(httpx_client, "_provider_slugs_loaded_at", time.monotonic())


@pytest.fixture
def lookup(monkeypatch):
    """The routes' call into the provider list, recorded: each must hand over
    its own TLS setting."""
    table = {p["name"]: p["slug"] for p in PROVIDERS["data"]}
    fake = AsyncMock(side_effect=lambda base_url, name, verify: table.get(name))
    monkeypatch.setattr(httpx_client, "_provider_slug", fake)
    return fake


@pytest.mark.parametrize("body", [
    {"openrouter_metadata": {"endpoints": {"available": [{"provider": {"id": 7}, "selected": True}]}}},
    {"openrouter_metadata": {"endpoints": {}}, "provider": ["Google"]},
], ids=["selected-entry", "top-level-fallback"])
def test_a_backend_that_is_not_a_name_is_not_recorded(body):
    """served_by is a str field, filled after the model has answered."""
    assert "selected" not in (openrouter_routing_info(body) or {})


@pytest.mark.usefixtures("published")
class TestTheOrder:
    @pytest.mark.parametrize("order, backend, expected", [
        (["google-vertex", "google-ai-studio"], "Google AI Studio",
         ["google-ai-studio", "google-vertex"]),
        (["google-ai-studio", "google-vertex"], "Google",
         ["google-vertex", "google-ai-studio"]),
        (["deepinfra/fp8", "streamlake/fp8"], "StreamLake",       # the suffix stays
         ["streamlake/fp8", "deepinfra/fp8"]),
    ])
    async def test_the_last_backend_moves_to_the_front(self, order, backend, expected):
        routing = {"order": list(order), "allow_fallbacks": False}
        sent = await routing_pinned_to_last_backend(routing, _history(backend), OPENROUTER, True)
        assert sent["order"] == expected
        assert sorted(sent["order"]) == sorted(order)  # never narrowed, never widened
        assert sent["allow_fallbacks"] is False
        assert routing == {"order": order, "allow_fallbacks": False}  # config untouched

    async def test_the_latest_backend_wins(self):
        messages = [ChatMessage(role="assistant", content="1", served_by="Google"),
                    ChatMessage(role="assistant", content="2", served_by="Google AI Studio")]
        sent = await routing_pinned_to_last_backend(dict(VERTEX_FIRST), messages, OPENROUTER, True)
        assert sent["order"] == AI_STUDIO_FIRST

    async def test_dict_messages_work_like_objects(self):
        messages = [{"role": "assistant", "content": "", "served_by": "Google AI Studio"}]
        sent = await routing_pinned_to_last_backend(dict(VERTEX_FIRST), messages, OPENROUTER, True)
        assert sent["order"] == AI_STUDIO_FIRST

    @pytest.mark.parametrize("routing, messages", [
        (VERTEX_FIRST, [ChatMessage(role="user", content="first turn")]),
        (VERTEX_FIRST, _history("Google")),                    # already in front
        ({"order": ["deepinfra/fp8"]}, _history("StreamLake")),  # not configured: not added
        (VERTEX_FIRST, _history("Some New Host")),             # not in the gateway's list
        ({"allow_fallbacks": False}, _history("Google AI Studio")),
        (None, _history("Google AI Studio")),
    ])
    async def test_nothing_to_pin_returns_the_configuration(self, routing, messages):
        assert await routing_pinned_to_last_backend(routing, messages, OPENROUTER, True) is routing


class _Gateway:
    """Stands in for httpx.AsyncClient on the provider-list GET."""

    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def __call__(self, *args, **kwargs):
        self.client_kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.urls.append(url)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return httpx.Response(200, json=self.answer, request=httpx.Request("GET", url))


class TestTheProviderList:
    async def test_the_translation_comes_from_the_gateway(self, monkeypatch):
        gateway = _Gateway(PROVIDERS)
        monkeypatch.setattr(httpx, "AsyncClient", gateway)
        sent = await routing_pinned_to_last_backend(
            {"order": list(AI_STUDIO_FIRST)}, _history("Google"), OPENROUTER, True)
        assert sent["order"] == ["google-vertex", "google-ai-studio"]
        assert gateway.urls == [OPENROUTER + "/providers"]

    async def test_a_known_backend_is_not_looked_up_again_a_new_one_is(self, monkeypatch):
        gateway = _Gateway(PROVIDERS)
        monkeypatch.setattr(httpx, "AsyncClient", gateway)
        await routing_pinned_to_last_backend(dict(VERTEX_FIRST), _history("Google AI Studio"), OPENROUTER, True)
        monkeypatch.setattr(httpx_client, "_provider_slugs_loaded_at", float("-inf"))
        await routing_pinned_to_last_backend(dict(VERTEX_FIRST), _history("Google AI Studio"), OPENROUTER, True)
        assert len(gateway.urls) == 1
        await routing_pinned_to_last_backend(dict(VERTEX_FIRST), _history("New Host"), OPENROUTER, True)
        assert len(gateway.urls) == 2

    async def test_the_list_is_fetched_with_the_callers_tls_setting(self, monkeypatch):
        gateway = _Gateway(PROVIDERS)
        monkeypatch.setattr(httpx, "AsyncClient", gateway)
        await routing_pinned_to_last_backend(
            dict(VERTEX_FIRST), _history("Google AI Studio"), OPENROUTER, "tls-context")
        assert gateway.client_kwargs["verify"] == "tls-context"

    async def test_a_malformed_entry_does_not_cost_the_others(self, monkeypatch):
        answer = {"data": [{"name": "Broken"}, {"slug": "orphan"}, "not a record",
                           *PROVIDERS["data"]]}
        monkeypatch.setattr(httpx, "AsyncClient", _Gateway(answer))
        sent = await routing_pinned_to_last_backend(
            dict(VERTEX_FIRST), _history("Google AI Studio"), OPENROUTER, True)
        assert sent["order"] == AI_STUDIO_FIRST

    async def test_a_cancelled_load_is_retried_by_the_next_request(self, monkeypatch):
        gateway = _Gateway(asyncio.CancelledError())
        monkeypatch.setattr(httpx, "AsyncClient", gateway)
        with pytest.raises(asyncio.CancelledError):
            await routing_pinned_to_last_backend(
                dict(VERTEX_FIRST), _history("Google AI Studio"), OPENROUTER, True)
        gateway.answer = PROVIDERS
        sent = await routing_pinned_to_last_backend(
            dict(VERTEX_FIRST), _history("Google AI Studio"), OPENROUTER, True)
        assert sent["order"] == AI_STUDIO_FIRST
        assert len(gateway.urls) == 2

    async def test_an_unreachable_list_pins_nothing_and_is_not_asked_at_once_again(self, monkeypatch):
        gateway = _Gateway(httpx.ConnectError("down"))
        monkeypatch.setattr(httpx, "AsyncClient", gateway)
        for _ in range(2):
            sent = await routing_pinned_to_last_backend(
                VERTEX_FIRST, _history("Google AI Studio"), OPENROUTER, True)
            assert sent is VERTEX_FIRST
        assert len(gateway.urls) == 1


def _ok():
    return httpx.Response(200, json={"output": []})


@pytest.mark.usefixtures("published")
class TestTheResponsesRoutes:
    def _client(self, cls=OpenAIResponsesClient):
        # Not the default TLS setting: the route asserts must be able to tell
        # the client's own setting from the one a call site would use anyway.
        client = cls(model="~google/gemini-flash-latest", api_key="k",
                     base_url=OPENROUTER, provider_routing=dict(VERTEX_FIRST),
                     ssl_verify=False, max_retries=1, retry_backoff=0.0)
        client._post = AsyncMock(return_value=_ok())
        return client

    def test_format_response_records_the_serving_backend(self):
        c = self._client()
        data = {"output": [], "openrouter_metadata": _meta("Google AI Studio")}
        assert c._format_response(data)["assistant"]["served_by"] == "Google AI Studio"
        assert "served_by" not in c._format_response({"output": []})["assistant"]

    async def test_the_request_is_pinned(self, lookup):
        c = self._client()
        await c.chat_tools(_history("Google AI Studio"), [])
        assert c._post.call_args.args[2]["provider"]["order"] == AI_STUDIO_FIRST
        assert lookup.call_args.args == (OPENROUTER, "Google AI Studio", httpx_verify(c.ssl_verify))

    async def test_the_sdk_route_sends_the_pinned_order(self):
        c = self._client(OpenRouterSDKClient)
        await c.chat_tools(_history("Google AI Studio"), [])
        kwargs = c._to_sdk_kwargs(c._post.call_args.args[2])
        assert kwargs["provider"]["order"] == AI_STUDIO_FIRST

    async def test_off_openrouter_the_list_is_never_asked(self, lookup):
        c = self._client()
        c.base_url = "https://llm-proxy.internal/v1"
        await c.chat_tools(_history("Google AI Studio"), [])
        assert lookup.call_count == 0
        assert c._post.call_args.args[2]["provider"] == VERTEX_FIRST

    @pytest.mark.parametrize("rejection", [
        httpx.Response(400, text="encrypted reasoning produced under a different model"),
        httpx.Response(200, json={"error": {"code": 400, "message": "Thought signature is not valid"}}),
    ], ids=["http-400", "body-error"])
    async def test_a_healed_retry_keeps_the_pin(self, rejection):
        c = self._client()
        c._post = AsyncMock(side_effect=[rejection, _ok()])
        await c.chat_tools(_history("Google AI Studio"), [])
        sent = [call.args[2] for call in c._post.call_args_list]
        assert len(sent) == 2
        assert all(p["provider"]["order"] == AI_STUDIO_FIRST for p in sent)


def _httpx(base_url=OPENROUTER):
    # Not the default TLS setting, for the same reason as in the Responses tests.
    client = HTTPXOpenAIClient(model="~google/gemini-flash-latest", api_key="k",
                               base_url=base_url, verify=False,
                               max_retries=1, retry_backoff=0.01)
    client.provider_routing = dict(VERTEX_FIRST)
    return client


@pytest.mark.usefixtures("published")
class TestTheChatRoute:
    def test_format_response_records_the_serving_backend(self):
        data = {"choices": [{"message": {"role": "assistant", "content": "x"},
                             "finish_reason": "stop"}],
                "openrouter_metadata": _meta("Google AI Studio")}
        assert _httpx()._format_response(data)["assistant"]["served_by"] == "Google AI Studio"

    async def test_the_non_streaming_payload_is_pinned(self, lookup):
        client = _httpx()
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "x"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            request=httpx.Request("POST", OPENROUTER + "/chat/completions"),
        )
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=response)
            mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)
            await client._make_request_non_streaming(_history("Google AI Studio"), tools=[])
        sent = mock_client.post.call_args.kwargs["json"]
        assert sent["provider"]["order"] == AI_STUDIO_FIRST
        assert all("served_by" not in m for m in sent["messages"])
        assert lookup.call_args.args == (OPENROUTER, "Google AI Studio", client._verify)

    async def test_off_openrouter_the_list_is_never_asked(self, lookup):
        proxy = "https://llm-proxy.internal/v1"
        client = _httpx(base_url=proxy)
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "x"},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            request=httpx.Request("POST", proxy + "/chat/completions"),
        )
        with patch("httpx.AsyncClient") as mock_async_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=response)
            mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)
            await client._make_request_non_streaming(_history("Google AI Studio"), tools=[])
        assert "provider" not in mock_client.post.call_args.kwargs["json"]
        assert lookup.call_count == 0

    async def test_off_openrouter_the_stream_does_not_ask_either(self, lookup):
        client = _httpx(base_url="https://llm-proxy.internal/v1")
        chunk = {"id": "c", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "x"},
                                         "finish_reason": "stop"}]}
        body = "data: " + json.dumps(chunk) + "\ndata: [DONE]\n"
        with patch("httpx.AsyncClient") as mock_async_client:
            stream_response = AsyncMock()
            stream_response.status_code = 200
            stream_response.headers = {}

            async def aiter_bytes():
                yield body.encode("utf-8")
            stream_response.aiter_bytes = aiter_bytes
            stream_response.__aenter__.return_value = stream_response
            stream_response.__aexit__.return_value = None
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=stream_response)
            mock_async_client.return_value = mock_client

            await client.chat_tools(_history("Google AI Studio"), [])

        assert "provider" not in mock_client.stream.call_args.kwargs["json"]
        assert lookup.call_count == 0

    @pytest.mark.parametrize("ending", ["done", "done-in-tail-buffer", "no-done"])
    async def test_the_stream_is_pinned_and_records_the_backend(self, ending, lookup):
        """Three places assemble the streamed assistant message: [DONE] in the
        chunk loop, [DONE] left in the tail buffer, a stream that just ends."""
        client = _httpx()
        chunks = [
            {"id": "c", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "x"},
                                     "finish_reason": None}]},
            {"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "openrouter_metadata": _meta("StreamLake")},
        ]
        lines = ["data: " + json.dumps(c) for c in chunks]
        if ending == "done":
            body = "\n".join(lines + ["data: [DONE]"]) + "\n"
        elif ending == "done-in-tail-buffer":
            body = "\n".join(lines + ["data: [DONE]"])
        else:
            body = "\n".join(lines) + "\n"

        with patch("httpx.AsyncClient") as mock_async_client:
            stream_response = AsyncMock()
            stream_response.status_code = 200
            stream_response.headers = {}

            async def aiter_bytes():
                yield body.encode("utf-8")
            stream_response.aiter_bytes = aiter_bytes
            stream_response.__aenter__.return_value = stream_response
            stream_response.__aexit__.return_value = None
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.stream = Mock(return_value=stream_response)
            mock_async_client.return_value = mock_client

            result = await client.chat_tools(_history("Google AI Studio"), [])

        sent = mock_client.stream.call_args.kwargs["json"]
        assert sent["provider"]["order"] == AI_STUDIO_FIRST
        assert result["assistant"]["served_by"] == "StreamLake"
        assert lookup.call_args.args == (OPENROUTER, "Google AI Studio", client._verify)
