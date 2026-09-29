"""Structured output on both Gemini routes: generationConfig.responseMimeType (+ responseJsonSchema).

Both clients run against an httpx MockTransport -- the REST client's own requests, and the
google-genai SDK's through ``HttpOptions(httpx_async_client=...)`` -- so what is asserted is the
request body the API would get, as the SDK serialises it.
"""
from __future__ import annotations

import json
import warnings

import httpx
import pytest

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.models import ChatMessage
from agent_system.llm.structured_output import JSON_OBJECT, ResponseFormat, StructuredOutputUnsupported
from plugins.llm_gemini.gemini_client import GeminiClient
from plugins.llm_gemini.gemini_sdk_client import GeminiSDKClient

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "note": {"anyOf": [
    {"type": "string"}, {"type": "null"}]}}, "required": ["city"], "additionalProperties": False}
MESSAGES = [ChatMessage(role="user", content="where to?")]
TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
ANSWER = '{"city": "Oslo"}'
BODY = {"candidates": [{"content": {"role": "model", "parts": [{"text": ANSWER}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7}}


def _handler(sent: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if "stream" in str(request.url):
            return httpx.Response(200, content=f"data: {json.dumps(BODY)}\r\n\r\n".encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=BODY)
    return handler


def _rest_client(capabilities, sent: list[dict], monkeypatch) -> GeminiClient:
    transport = httpx.MockTransport(_handler(sent))
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return GeminiClient(model="gemini-3-flash", api_key="k", base_url="https://gemini.test/v1beta",
                        capabilities=capabilities, max_retries=0)


def _sdk_client(capabilities, sent: list[dict]) -> GeminiSDKClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from google import genai
        from google.genai import types

    client = GeminiSDKClient(model="gemini-3-flash", api_key="k", base_url="https://gemini.test/v1beta",
                             capabilities=capabilities, max_retries=0)
    client._client = genai.Client(api_key="k", http_options=types.HttpOptions(
        httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler(sent)))))
    return client


async def _ask(client, streaming: bool, **kwargs):
    if streaming:
        chunks = [c async for c in client.chat_tools_streaming(MESSAGES, TOOLS, **kwargs)]
        return chunks[-1]
    return await client.chat_tools(MESSAGES, TOOLS, **kwargs)


@pytest.mark.parametrize("route", ["rest", "sdk"])
@pytest.mark.parametrize("streaming", [True, False])
async def test_the_schema_goes_as_response_json_schema_unsanitised(route, streaming, monkeypatch):
    sent: list[dict] = []
    caps = ModelCapabilitiesConfig(structured_output=True, streaming=streaming)
    client = _rest_client(caps, sent, monkeypatch) if route == "rest" else _sdk_client(caps, sent)

    final = await _ask(client, streaming, response_format=ResponseFormat(schema=SCHEMA))

    config = sent[-1]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    # JSON Schema as it is: anyOf and additionalProperties survive (the function-declaration
    # sanitiser would have dropped both).
    assert config["responseJsonSchema"] == SCHEMA
    assert sent[-1]["tools"], "the schema goes beside the tools"
    assert final["assistant"]["content"] == ANSWER


@pytest.mark.parametrize("route", ["rest", "sdk"])
async def test_json_mode_is_the_mime_type_alone_and_no_format_sends_neither(route, monkeypatch):
    sent: list[dict] = []
    caps = ModelCapabilitiesConfig(structured_output=True, streaming=False)
    client = _rest_client(caps, sent, monkeypatch) if route == "rest" else _sdk_client(caps, sent)

    await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    await client.chat_tools(MESSAGES, TOOLS)

    assert sent[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in sent[0]["generationConfig"]
    assert not {"responseMimeType", "responseJsonSchema"} & set(sent[1]["generationConfig"])


@pytest.mark.parametrize("route", ["rest", "sdk"])
@pytest.mark.parametrize("streaming", [True, False])
async def test_a_model_entry_without_the_capability_sends_nothing(route, streaming, monkeypatch):
    sent: list[dict] = []
    caps = ModelCapabilitiesConfig(json_mode=True, streaming=streaming)
    client = _rest_client(caps, sent, monkeypatch) if route == "rest" else _sdk_client(caps, sent)

    with pytest.raises(StructuredOutputUnsupported):
        await _ask(client, streaming, response_format=ResponseFormat(schema=SCHEMA))
    assert sent == []
