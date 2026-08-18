"""The retry paths of the Responses client.

These branches decide what happens when a provider misbehaves, and they were
entirely uncovered: a coverage run over the client never executed the
transport-retry, tier-drop, 5xx-retry or exhaustion arms. They are also where
a live UnboundLocalError came from (`_tier_dropped` read before assignment on
a retry that followed a 429), so "never executed" was not the same as "fine".

Everything here drives the real `chat_tools()` against a fake transport;
nothing reimplements the loop.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_system.llm.models import (
    ChatMessage,
    LLMQuotaExhaustedError,
    LLMRateLimitError,
    LLMServerError,
)
from agent_system.llm.openai_responses_client import OpenAIResponsesClient

OK_BODY = {"output": [{"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "done"}]}]}
MESSAGES = [ChatMessage(role="user", content="hi")]


def _client(**kw) -> OpenAIResponsesClient:
    defaults = dict(model="openai/gpt-5.6-terra", api_key="sk-test",
                    base_url="https://openrouter.ai/api/v1",
                    service_tier="flex", max_retries=2, retry_backoff=0.0)
    defaults.update(kw)
    return OpenAIResponsesClient(**defaults)


class _Transport:
    """Replays a scripted list of responses/exceptions and records payloads."""

    def __init__(self, script):
        self.script = list(script)
        self.payloads: list[dict] = []

    async def post(self, url, json=None, headers=None):
        # A COPY: the client pops service_tier out of the very dict it sent, so
        # recording the reference would rewrite history and the tier-drop
        # assertion would compare a payload against its own later state.
        self.payloads.append(copy.deepcopy(json))
        item = self.script.pop(0) if self.script else httpx.Response(200, json=OK_BODY)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def calls(self) -> int:
        return len(self.payloads)


@pytest.fixture
def transport(monkeypatch):
    """Replace the AsyncClient so no socket is opened and sleeps are free."""
    holder = {}

    def install(script):
        t = _Transport(script)
        holder["t"] = t

        class _FakeAsyncClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return t
            async def __aexit__(self, *a): return False

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
        return t

    return install


class TestTransportErrors:
    async def test_a_timeout_is_retried_and_then_succeeds(self, transport):
        t = transport([httpx.TimeoutException("boom"), httpx.Response(200, json=OK_BODY)])
        result = await _client().chat_tools(MESSAGES, [])
        assert t.calls == 2
        assert result["assistant"]["content"] == "done"

    async def test_transport_errors_are_raised_once_exhausted(self, transport):
        t = transport([httpx.TransportError("x")] * 5)
        with pytest.raises(httpx.TransportError):
            await _client(max_retries=2).chat_tools(MESSAGES, [])
        assert t.calls == 3, "max_retries=2 means three attempts in total"


class TestServerErrors:
    async def test_a_500_is_retried(self, transport):
        t = transport([httpx.Response(500, text="upstream"), httpx.Response(200, json=OK_BODY)])
        await _client().chat_tools(MESSAGES, [])
        assert t.calls == 2

    async def test_a_500_becomes_a_typed_error_once_exhausted(self, transport):
        transport([httpx.Response(503, text="down")] * 5)
        with pytest.raises(LLMServerError) as exc:
            await _client(max_retries=1).chat_tools(MESSAGES, [])
        assert exc.value.status_code == 503


class TestRateLimitAndTierDrop:
    async def test_a_429_drops_the_flex_tier_and_retries(self, transport):
        t = transport([httpx.Response(429, text="slow down"), httpx.Response(200, json=OK_BODY)])
        await _client(service_tier="flex").chat_tools(MESSAGES, [])

        assert t.calls == 2
        assert t.payloads[0].get("service_tier") == "flex"
        assert "service_tier" not in t.payloads[1], \
            "the retry went out on the flex tier again"

    async def test_the_tier_drop_does_not_consume_a_retry_slot(self, transport):
        """The request changed substantially, so it is not a repeat."""
        t = transport([httpx.Response(429, text="a"),   # drops the tier, free
                       httpx.Response(500, text="b"),   # slot 1
                       httpx.Response(500, text="c"),   # slot 2
                       httpx.Response(200, json=OK_BODY)])
        await _client(max_retries=2, service_tier="flex").chat_tools(MESSAGES, [])
        assert t.calls == 4

    async def test_a_429_without_a_tier_is_a_typed_rate_limit(self, transport):
        transport([httpx.Response(429, text="too many")])
        with pytest.raises(LLMRateLimitError):
            await _client(service_tier=None).chat_tools(MESSAGES, [])

    async def test_an_exhausted_quota_is_told_apart_from_a_rate_limit(self, transport):
        """Different remedy: waiting helps for one, never for the other."""
        transport([httpx.Response(429, text="quota exceeded for this project")])
        with pytest.raises(LLMQuotaExhaustedError):
            await _client(service_tier=None).chat_tools(MESSAGES, [])

    async def test_retry_after_is_carried_into_the_error(self, transport):
        transport([httpx.Response(429, text="slow", headers={"retry-after": "42"})])
        with pytest.raises(LLMRateLimitError) as exc:
            await _client(service_tier=None).chat_tools(MESSAGES, [])
        assert exc.value.retry_after == 42.0
