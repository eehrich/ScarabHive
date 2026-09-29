"""Structured output on native Ollama (/api/chat ``format``): "json", or the schema itself.

Real httpx behind a MockTransport, as in test_llm_ollama_wire.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.models import ChatMessage
from agent_system.llm.structured_output import JSON_OBJECT, ResponseFormat, StructuredOutputUnsupported
from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
MESSAGES = [ChatMessage(role="user", content="where to?")]
TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
ANSWER = '{"city": "Oslo"}'


def _client(sent: list[dict], **capabilities) -> OllamaNativeAsyncClient:
    client = OllamaNativeAsyncClient(model="qwen3:4b", base_url="http://ollama.test",
                                     capabilities=ModelCapabilitiesConfig(**capabilities))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        message = {"role": "assistant", "content": ANSWER}
        if body.get("stream"):
            lines = [{"message": message, "done": False}, {"message": {"role": "assistant", "content": ""},
                                                           "done": True, "done_reason": "stop"}]
            return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines).encode())
        return httpx.Response(200, json={"message": message, "done": True, "done_reason": "stop"})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    client._httpx = SimpleNamespace(
        AsyncClient=lambda **kw: real(transport=transport, **kw),
        HTTPStatusError=httpx.HTTPStatusError, RemoteProtocolError=httpx.RemoteProtocolError,
        NetworkError=httpx.NetworkError, ConnectError=httpx.ConnectError)
    return client


@pytest.mark.parametrize("streaming", [True, False])
async def test_the_schema_is_the_format(streaming):
    sent: list[dict] = []
    client = _client(sent, structured_output=True)
    fmt = ResponseFormat(schema=SCHEMA)

    if streaming:
        final = [c async for c in client.chat_tools_streaming(MESSAGES, TOOLS, response_format=fmt)][-1]
    else:
        final = await client.chat_tools(MESSAGES, TOOLS, response_format=fmt)

    assert sent[-1]["format"] == SCHEMA and sent[-1]["tools"]
    assert final["assistant"]["content"] == ANSWER


async def test_json_mode_is_the_word_json_and_no_format_sends_none():
    sent: list[dict] = []
    client = _client(sent, structured_output=True)

    await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    await client.chat(MESSAGES)

    assert sent[0]["format"] == "json"
    assert "format" not in sent[1]


async def test_an_undeclared_model_sends_nothing():
    sent: list[dict] = []
    with pytest.raises(StructuredOutputUnsupported):
        await _client(sent, json_mode=True).chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(schema=SCHEMA))
    assert sent == []
