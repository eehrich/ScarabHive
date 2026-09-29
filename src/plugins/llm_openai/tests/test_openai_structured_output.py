"""Structured output on the OpenAI SDK route (Chat Completions): ``response_format`` on the wire.

The real ``openai`` SDK over an httpx MockTransport: what is asserted is the JSON the SDK sends,
for the blocking path, the streaming path and ``chat()`` -- three payload builders in this client.
The realtime session has no such field and refuses the request.
"""
from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.models import ChatMessage
from agent_system.llm.structured_output import JSON_OBJECT, ResponseFormat, StructuredOutputUnsupported
from plugins.llm_openai.openai_client import OpenAIAsyncClient

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
          "additionalProperties": False}
MESSAGES = [ChatMessage(role="user", content="where to?")]
TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
ANSWER = '{"city": "Oslo"}'


def _handler(sent: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        if payload.get("stream"):
            chunks = [{"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                       "choices": [{"index": 0, "delta": {"role": "assistant", "content": ANSWER},
                                    "finish_reason": None}]},
                      {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
    return handler


def _client(sent: list[dict], **capabilities) -> OpenAIAsyncClient:
    client = OpenAIAsyncClient(model="m", api_key="k", base_url="https://gateway.test/v1",
                               capabilities=ModelCapabilitiesConfig(**capabilities), max_attempts=1)
    client._client = AsyncOpenAI(api_key="k", base_url="https://gateway.test/v1", max_retries=0,
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler(sent))))
    return client


@pytest.mark.parametrize("path", ["blocking", "streaming", "chat"])
async def test_the_schema_goes_as_response_format_on_every_path(path):
    sent: list[dict] = []
    client = _client(sent, structured_output=True)
    fmt = ResponseFormat(schema=SCHEMA, name="place", strict=True)

    if path == "blocking":
        answer = (await client.chat_tools(MESSAGES, TOOLS, response_format=fmt))["assistant"]["content"]
    elif path == "streaming":
        chunks = [c async for c in client.chat_tools_streaming(MESSAGES, TOOLS, response_format=fmt)]
        answer = chunks[-1]["assistant"]["content"]
    else:
        answer = await client.chat(MESSAGES, response_format=fmt)

    assert len(sent) == 1
    assert sent[0]["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "place", "schema": SCHEMA, "strict": True}}
    assert answer == ANSWER


async def test_json_mode_and_no_format():
    sent: list[dict] = []
    client = _client(sent, structured_output=True)

    await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    await client.chat_tools(MESSAGES, TOOLS)

    assert sent[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in sent[1]


@pytest.mark.parametrize("path", ["blocking", "streaming", "chat"])
async def test_a_model_entry_without_the_capability_sends_nothing(path):
    sent: list[dict] = []
    client = _client(sent, json_mode=True)
    fmt = ResponseFormat(schema=SCHEMA)

    with pytest.raises(StructuredOutputUnsupported):
        if path == "blocking":
            await client.chat_tools(MESSAGES, TOOLS, response_format=fmt)
        elif path == "streaming":
            async for _ in client.chat_tools_streaming(MESSAGES, TOOLS, response_format=fmt):
                pass
        else:
            await client.chat(MESSAGES, response_format=fmt)
    assert sent == []


def test_the_realtime_session_does_not_take_it():
    client = _client([], structured_output=True, default_api_type="realtime",
                     supported_api_types=["realtime"])
    assert not client.supports_response_format(ResponseFormat(schema=SCHEMA))
    assert _client([], structured_output=True).supports_response_format(ResponseFormat(schema=SCHEMA))
