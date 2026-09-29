"""Structured output on the two httpx routes: which field the request carries, and when none may go out.

The Chat Completions client runs against an httpx MockTransport (both its streaming and its
blocking path build their own payload -- the two drifted before), the Responses client against
its own transport seams (``_post`` / ``_stream``). What is asserted is the JSON that would leave.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.models import ChatMessage
from agent_system.llm.structured_output import JSON_OBJECT, ResponseFormat, StructuredOutputUnsupported
from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient
from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
          "additionalProperties": False}
MESSAGES = [ChatMessage(role="user", content="where to?")]
TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
ANSWER = '{"city": "Oslo"}'


def _caps(**kw) -> ModelCapabilitiesConfig:
    return ModelCapabilitiesConfig(**kw)


# ------------------------------------------------------------------ Chat Completions (httpx)

def _chat_client(capabilities, sent: list[dict], monkeypatch) -> HTTPXOpenAIClient:
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

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return HTTPXOpenAIClient(model="m", api_key="k", base_url="https://gateway.test/v1",
                             capabilities=capabilities, max_retries=0)


@pytest.mark.parametrize("streaming", [True, False])
async def test_chat_completions_carries_the_schema_as_response_format(streaming, monkeypatch):
    sent: list[dict] = []
    client = _chat_client(_caps(structured_output=True, streaming=streaming), sent, monkeypatch)
    fmt = ResponseFormat(schema=SCHEMA, name="place", strict=True, description="where")

    if streaming:
        chunks = [c async for c in client.chat_tools_streaming(MESSAGES, TOOLS, response_format=fmt)]
        final = chunks[-1]
    else:
        final = await client.chat_tools(MESSAGES, TOOLS, response_format=fmt)

    assert len(sent) == 1 and bool(sent[0].get("stream")) is streaming
    assert sent[0]["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "place", "schema": SCHEMA, "strict": True, "description": "where"}}
    assert sent[0]["tools"], "the schema goes beside the tools, not instead of them"
    assert final["assistant"]["content"] == ANSWER


async def test_chat_completions_json_mode_and_no_format_at_all(monkeypatch):
    sent: list[dict] = []
    client = _chat_client(_caps(structured_output=True, streaming=False), sent, monkeypatch)

    await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    await client.chat_tools(MESSAGES, TOOLS)

    assert sent[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in sent[1], "a call without a format must stay the call it was"


async def test_json_mode_alone_in_the_catalogue_sends_no_native_json_mode(monkeypatch):
    """json_mode values were never verified: only structured_output unlocks the native field."""
    sent: list[dict] = []
    client = _chat_client(_caps(json_mode=True, streaming=False), sent, monkeypatch)

    assert not client.supports_response_format(ResponseFormat(type=JSON_OBJECT))
    with pytest.raises(StructuredOutputUnsupported, match="capabilities.structured_output"):
        await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    assert sent == []


@pytest.mark.parametrize("streaming", [True, False])
async def test_chat_completions_refuses_what_the_model_entry_does_not_declare(streaming, monkeypatch):
    sent: list[dict] = []
    client = _chat_client(_caps(json_mode=True, streaming=streaming), sent, monkeypatch)

    with pytest.raises(StructuredOutputUnsupported, match="capabilities.structured_output"):
        if streaming:
            async for _ in client.chat_tools_streaming(MESSAGES, TOOLS, response_format=ResponseFormat(schema=SCHEMA)):
                pass
        else:
            await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(schema=SCHEMA))
    assert sent == [], "the request went out without the field"


# ------------------------------------------------------------------ Responses

class _Stream:
    """The ``_stream`` seam's context manager: one output_text answer, then completed."""

    status_code = 200
    headers = {"content-type": "text/event-stream"}
    text = ""
    request = None

    def __init__(self, completed: dict):
        self._events = [{"type": "response.output_text.delta", "delta": ANSWER}, completed]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}\n\n".encode()
        yield b"data: [DONE]\n\n"


def _responses_client(capabilities, sent: list[dict], first: httpx.Response | None = None) -> OpenAIResponsesClient:
    body = {"status": "completed", "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            "output": [{"type": "message", "id": "m1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": ANSWER, "annotations": []}]}]}
    client = OpenAIResponsesClient(model="openai/gpt-x", api_key="k", base_url="https://gateway.test/v1",
                                   capabilities=capabilities)

    async def post(_client, _url, payload):
        sent.append(json.loads(json.dumps(payload)))  # a copy: the loop edits its payload for retries
        if first is not None and len(sent) == 1:
            return first
        return httpx.Response(200, json=body)

    def stream(_client, _url, payload):
        sent.append(json.loads(json.dumps(payload)))
        return _Stream({"type": "response.completed", "response": body})

    client._post = post
    client._stream = stream
    return client


@pytest.mark.parametrize("streaming", [True, False])
async def test_responses_carries_the_schema_as_text_format(streaming):
    sent: list[dict] = []
    client = _responses_client(_caps(structured_output=True, streaming=streaming), sent)
    fmt = ResponseFormat(schema=SCHEMA, name="place", strict=False)

    chunks = [c async for c in client.chat_tools_streaming(MESSAGES, TOOLS, response_format=fmt)]

    assert len(sent) == 1
    assert sent[0]["text"] == {"format": {"type": "json_schema", "name": "place", "schema": SCHEMA,
                                          "strict": False}}
    assert sent[0]["tools"]
    assert chunks[-1]["type"] == "final" and chunks[-1]["assistant"]["content"] == ANSWER


async def test_responses_json_mode_and_the_refusal():
    sent: list[dict] = []
    client = _responses_client(_caps(structured_output=True, streaming=False), sent)

    await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    assert sent[-1]["text"] == {"format": {"type": "json_object"}}

    await client.chat_tools(MESSAGES, TOOLS)
    assert "text" not in sent[-1]

    before = len(sent)
    with pytest.raises(StructuredOutputUnsupported):
        await _responses_client(_caps(json_mode=True, streaming=False), sent).chat_tools(
            MESSAGES, TOOLS, response_format=ResponseFormat(type=JSON_OBJECT))
    assert len(sent) == before, "the request went out without the field"



async def test_responses_keeps_the_field_when_a_heal_rebuilds_the_payload():
    """A defective reasoning item (400) makes the loop strip the artifacts and build the payload anew:
    the rebuilt request carries text.format as the first did."""
    sent: list[dict] = []
    rejected = httpx.Response(400, text="invalid_encrypted_content: reasoning item rs_abc is not usable")
    client = _responses_client(_caps(structured_output=True, streaming=False), sent, first=rejected)

    result = await client.chat_tools(MESSAGES, TOOLS, response_format=ResponseFormat(schema=SCHEMA, name="place"))

    assert len(sent) == 2, "fixture: the heal did not rebuild and resend"
    assert sent[0]["text"] == sent[1]["text"] == {"format": {"type": "json_schema", "name": "place",
                                                             "schema": SCHEMA}}
    assert result["assistant"]["content"] == ANSWER
