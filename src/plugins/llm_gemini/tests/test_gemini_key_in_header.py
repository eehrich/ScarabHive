"""The REST client sends its key in the x-goog-api-key header, never in the URL.

httpx logs every request at INFO with its whole URL: a key in the query
(``?key=...``) went into api.log on every call.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent_system.llm.models import ChatMessage
from plugins.llm_gemini.gemini_client import GeminiClient

KEY = "AIza" + "Sy-only-its-holder-may-use-0123456789"  # split: the export masks the whole literal
BODY = {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4}}


@pytest.mark.parametrize("streaming", [False, True], ids=["generate", "stream"])
async def test_the_key_goes_in_a_header(monkeypatch, streaming):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "stream" in str(request.url):
            return httpx.Response(200, content=f"data: {json.dumps(BODY)}\r\n\r\n".encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=BODY)

    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    client = GeminiClient(model="gemini-3-flash", api_key=KEY, base_url="https://gemini.test/v1beta", max_retries=0)
    messages = [ChatMessage(role="user", content="hi")]
    if streaming:
        [chunk async for chunk in client.chat_tools_streaming(messages, [])]
    else:
        await client.chat_tools(messages, [])

    assert seen, "no request was sent"
    assert all(KEY not in str(request.url) for request in seen), [str(r.url) for r in seen]
    assert all(request.headers.get("x-goog-api-key") == KEY for request in seen)
    if streaming:
        assert seen[0].url.params.get("alt") == "sse"
