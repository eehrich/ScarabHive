"""HTTP status 400 path for Gemini "Corrupted thought signature".

Observed 2026-07-24 (gemini-3.5-flash-lite via OpenRouter): the error came
as a real HTTP status 400 -- but the signature bypass only covered the
body-level form (error inside HTTP 200), so the run crashed hard with
HTTPStatusError instead of healing. The status path now injects the bypass
token once and retries (mirrors the encrypted-reasoning backstop).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

import httpx

from agent_system.config.models import ModelCapabilitiesConfig
from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient
from agent_system.llm.models import ChatMessage


SIG_400_BODY = json.dumps({
    "error": {"message": "Provider returned error", "code": 400, "metadata": {
        "raw": '{\n  "error": {\n    "code": 400,\n    "message": "Corrupted thought signature.",\n    "status": "INVALID_ARGUMENT"\n  }\n}',
        "provider_name": "Google",
    }},
})

OK_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
})


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self.content = text.encode()
        self.request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")

    def json(self):
        return json.loads(self.text)


class _FakeAsyncClient:
    """First POST -> status 400 with a signature error, then 200."""
    calls: list

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, content=None, headers=None, **kw):
        payload = json if json is not None else __import__("json").loads(content or "{}")
        _FakeAsyncClient.calls.append(payload)
        if len(_FakeAsyncClient.calls) == 1:
            return _FakeResponse(400, SIG_400_BODY)
        return _FakeResponse(200, OK_BODY)


@pytest.mark.asyncio
async def test_status400_thought_signature_heals_with_bypass_token():
    _FakeAsyncClient.calls = []
    client = HTTPXOpenAIClient(
        model="google/gemini-3.5-flash-lite",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_retries=2,
        retry_backoff=0.01,
        capabilities=ModelCapabilitiesConfig(tools=True, streaming=False),
    )
    msgs = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            reasoning_details=[{"type": "reasoning.encrypted",
                                "format": "google-gemini-v1", "data": "SIG", "index": 0}]),
        ChatMessage(role="tool", tool_call_id="c1", content="r"),
    ]
    with patch("plugins.llm_openai_compat.httpx_client.httpx.AsyncClient", _FakeAsyncClient):
        result = await client.chat_tools(msgs, [])

    assert result["assistant"]["content"] == "ok"
    assert len(_FakeAsyncClient.calls) == 2, "exactly one bypass retry expected"
    retry_payload = _FakeAsyncClient.calls[1]
    blocks = [b for m in retry_payload["messages"]
              for b in (m.get("reasoning_details") or [])]
    assert any(b.get("data") == "skip_thought_signature_validator" for b in blocks), \
        "bypass token must be in the retry payload"


@pytest.mark.asyncio
async def test_status400_non_signature_still_raises():
    _FakeAsyncClient.calls = []

    class _Fake400Always(_FakeAsyncClient):
        async def post(self, url, **kw):
            _FakeAsyncClient.calls.append(kw)
            return _FakeResponse(400, '{"error":{"message":"Invalid request","code":400}}')

    client = HTTPXOpenAIClient(
        model="google/gemini-3.5-flash-lite",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_retries=1,
        retry_backoff=0.01,
        capabilities=ModelCapabilitiesConfig(tools=True, streaming=False),
    )
    with patch("plugins.llm_openai_compat.httpx_client.httpx.AsyncClient", _Fake400Always):
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat_tools([ChatMessage(role="user", content="hi")], [])
    assert len(_FakeAsyncClient.calls) == 1, "no retry for unrelated 400s"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


@pytest.mark.asyncio
async def test_a_heal_on_the_last_attempt_still_sends_the_healed_request():
    """A heal used to cost an attempt: on the last one it patched the request
    and then left the loop with an untyped Exception the fallback chain does
    not recognise. With a single attempt the healed retry must still go out."""
    _FakeAsyncClient.calls = []
    client = HTTPXOpenAIClient(
        model="google/gemini-3.5-flash-lite",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        max_retries=0,
        rate_limit_max_retries=0,
        retry_backoff=0.01,
        capabilities=ModelCapabilitiesConfig(tools=True, streaming=False),
    )
    msgs = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            reasoning_details=[{"type": "reasoning.encrypted",
                                "format": "google-gemini-v1", "data": "SIG", "index": 0}]),
        ChatMessage(role="tool", tool_call_id="c1", content="r"),
    ]
    with patch("plugins.llm_openai_compat.httpx_client.httpx.AsyncClient", _FakeAsyncClient):
        result = await client.chat_tools(msgs, [])

    assert result["assistant"]["content"] == "ok"
    assert len(_FakeAsyncClient.calls) == 2


@pytest.mark.asyncio
async def test_a_tool_results_images_follow_the_whole_run_of_parallel_results(monkeypatch):
    """tool, user, tool: the injected image of a non-last parallel result sat
    between the tool messages and the API refused the request."""
    from agent_system.llm.models import MultimodalToolContent

    _FakeAsyncClient.calls = []

    class _Ok(_FakeAsyncClient):
        async def post(self, url, json=None, **kw):
            _FakeAsyncClient.calls.append(json)
            return _FakeResponse(200, OK_BODY)

    client = HTTPXOpenAIClient(
        model="m", api_key="test-key", base_url="https://api.openai.com/v1",
        max_retries=0, capabilities=ModelCapabilitiesConfig(tools=True, streaming=False))
    monkeypatch.setattr(client, "_create_multimodal_injection",
                        lambda msg: {"role": "user", "content": "IMG"})
    shot = [MultimodalToolContent(type="image", path="/x.png", mime_type="image/png")]
    msgs = [
        ChatMessage(role="user", content="look"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="a", content="1", multimodal_content=shot),
        ChatMessage(role="tool", tool_call_id="b", content="2"),
    ]
    with patch("plugins.llm_openai_compat.httpx_client.httpx.AsyncClient", _Ok):
        await client.chat_tools(msgs, [])

    roles = [m["role"] for m in _FakeAsyncClient.calls[0]["messages"]]
    assert roles[-3:] == ["tool", "tool", "user"], roles
