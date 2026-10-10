"""Flex->standard tier fallback in the Responses client (review finding, major).

The httpx route drops the service_tier on the first 429 (saturated flex
queue -- standard is usually free) and retries. The Responses client
instead raised immediately (HTTP 429) or retried stubbornly on flex
(body 429) -> needless model fallbacks for all flex models. Now: a single
tier drop without consuming a retry slot, in both paths; enc-heal payload
rebuilds do not restore the dropped tier.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

import httpx

from agent_system.llm.models import ChatMessage
from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient


OK_BODY = json.dumps({"output": [{"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "ok"}]}],
                      "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}})
RATE_LIMIT_429 = '{"error":{"message":"Rate limit exceeded","code":429}}'
BODY_RATE_LIMIT_200 = json.dumps({"error": {
    "message": "We're currently processing too many requests — please try again later.",
    "code": "rate_limit_exceeded"}})


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self.request = httpx.Request("POST", "https://openrouter.ai/api/v1/responses")

    def json(self):
        return json.loads(self.text)


def _make_fake(sequence):
    class _Fake:
        calls: list = []

        def __init__(self, *a, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None, **kw):
            import copy as _copy
            # Snapshot instead of reference -- the client mutates the payload dict
            # in place on the tier drop.
            _Fake.calls.append(_copy.deepcopy(json))
            status, body = sequence[min(len(_Fake.calls) - 1, len(sequence) - 1)]
            return _FakeResponse(status, body)
    _Fake.calls = []
    return _Fake


def _client():
    return OpenAIResponsesClient(
        model="google/gemini-3-flash-preview", api_key="test",
        service_tier="flex", thinking_level="low", max_retries=2, retry_backoff=0.01)


@pytest.mark.asyncio
async def test_http_429_drops_flex_tier_and_retries():
    fake = _make_fake([(429, RATE_LIMIT_429), (200, OK_BODY)])
    with patch("plugins.llm_openai_compat.openai_responses_client.httpx.AsyncClient", fake):
        result = await _client().chat_tools([ChatMessage(role="user", content="hi")], [])
    assert result["assistant"]["content"] == "ok"
    assert len(fake.calls) == 2
    assert fake.calls[0].get("service_tier") == "flex"
    assert "service_tier" not in fake.calls[1], "retry must run on standard"


@pytest.mark.asyncio
async def test_body_429_drops_flex_tier_first():
    fake = _make_fake([(200, BODY_RATE_LIMIT_200), (200, OK_BODY)])
    with patch("plugins.llm_openai_compat.openai_responses_client.httpx.AsyncClient", fake):
        result = await _client().chat_tools([ChatMessage(role="user", content="hi")], [])
    assert result["assistant"]["content"] == "ok"
    assert len(fake.calls) == 2
    assert "service_tier" not in fake.calls[1]


@pytest.mark.asyncio
async def test_second_429_after_drop_raises_typed():
    from agent_system.llm.models import LLMRateLimitError
    fake = _make_fake([(429, RATE_LIMIT_429), (429, RATE_LIMIT_429)])
    with patch("plugins.llm_openai_compat.openai_responses_client.httpx.AsyncClient", fake):
        with pytest.raises(LLMRateLimitError):
            await _client().chat_tools([ChatMessage(role="user", content="hi")], [])
    assert len(fake.calls) == 2, "no further drop loop after the tier drop"


@pytest.mark.asyncio
async def test_enc_heal_rebuild_keeps_tier_dropped():
    """A payload rebuild in the enc-heal must not bring back the dropped tier."""
    enc_400 = json.dumps({"error": {"message":
        "The encrypted content for item rs_x could not be verified.", "code": 400}})
    fake = _make_fake([(429, RATE_LIMIT_429), (400, enc_400), (200, OK_BODY)])
    msgs = [ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="", reasoning_details=[
                {"type": "reasoning.responses_items", "format": "openai-responses-items-v1",
                 "index": 0, "items": [{"type": "reasoning", "id": "rs_x",
                                        "encrypted_content": "B", "summary": []}]}])]
    with patch("plugins.llm_openai_compat.openai_responses_client.httpx.AsyncClient", fake):
        result = await _client().chat_tools(msgs, [])
    assert result["assistant"]["content"] == "ok"
    assert len(fake.calls) == 3
    assert "service_tier" not in fake.calls[2], "rebuild does not restore flex"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
