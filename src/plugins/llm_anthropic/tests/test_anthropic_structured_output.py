"""Structured output on the native Anthropic route: ``output_config.format`` on the wire.

The real ``anthropic`` SDK over an httpx MockTransport, streaming (what chat_tools uses too) and
``chat()``. The Messages API has no schema-less JSON mode, so json_object is refused here.
"""
from __future__ import annotations

import json

import httpx
import pytest
from anthropic import AsyncAnthropic

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.models import ChatMessage
from agent_system.llm.structured_output import JSON_OBJECT, ResponseFormat, StructuredOutputUnsupported
from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
          "additionalProperties": False}
MESSAGES = [ChatMessage(role="user", content="where to?")]
TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
ANSWER = '{"city": "Oslo"}'


def _events() -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": ANSWER}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 4}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _client(sent: list[dict], **capabilities) -> AnthropicAsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        if payload.get("stream"):
            return httpx.Response(200, content=_events(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": ANSWER}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 4}})

    client = AnthropicAsyncClient(model="claude-x", api_key="k", max_retries=0, rate_limit_max_retries=0,
                                  capabilities=ModelCapabilitiesConfig(**capabilities))
    client._client = AsyncAnthropic(api_key="k", max_retries=0,
                                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client


async def test_the_schema_goes_as_output_config_format_beside_the_tools():
    sent: list[dict] = []
    client = _client(sent, structured_output=True)

    result = await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(schema=SCHEMA))

    assert len(sent) == 1
    assert sent[0]["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert sent[0]["tools"]
    assert result["assistant"]["content"] == ANSWER


async def test_chat_carries_it_too_and_no_format_sends_none():
    sent: list[dict] = []
    client = _client(sent, structured_output=True)

    assert await client.chat(MESSAGES, response_format=ResponseFormat(schema=SCHEMA)) == ANSWER
    await client.chat_tools(MESSAGES, TOOLS)

    assert sent[0]["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert "output_config" not in sent[1]


async def test_json_mode_and_an_undeclared_model_are_refused_before_the_wire():
    sent: list[dict] = []
    with pytest.raises(StructuredOutputUnsupported, match="no wire field for json_object"):
        await _client(sent, structured_output=True, json_mode=True).chat_tools(
            MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    with pytest.raises(StructuredOutputUnsupported, match="capabilities.structured_output"):
        await _client(sent).chat(MESSAGES, response_format=ResponseFormat(schema=SCHEMA))
    assert sent == []
