"""The Realtime path against a scripted WebSocket in the shape of the GA API.

The event shapes are the ones gpt-realtime-mini sent on 2026-09-16: the tool
name arrives in ``response.output_item.added`` (never on the argument deltas),
text as ``response.output_text.delta``, the complete result and the usage in
``response.done``. The beta version of this code read other event names,
sent a header the API no longer accepts, and could wait forever.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import websockets
import websockets.exceptions

from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.models import ChatMessage, MultimodalToolContent
from plugins.llm_openai import realtime_adapter
from plugins.llm_openai.openai_client import OpenAIAsyncClient
from plugins.llm_openai.realtime_session import realtime_url

USAGE = {"total_tokens": 80, "input_tokens": 60, "output_tokens": 20,
         "input_token_details": {"text_tokens": 60, "audio_tokens": 0, "image_tokens": 0, "cached_tokens": 32},
         "output_token_details": {"text_tokens": 20, "audio_tokens": 0}}

TEXT_ANSWER = [
    {"type": "response.created"},
    {"type": "response.output_item.added", "item": {"id": "i1", "type": "message", "role": "assistant", "content": []}},
    {"type": "response.output_text.delta", "delta": "Hel"},
    {"type": "response.output_text.delta", "delta": "lo"},
    {"type": "response.done", "response": {"status": "completed", "usage": USAGE, "output": [
        {"id": "i1", "type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Hello"}]}]}},
]

TOOL_CALL = [
    {"type": "response.created"},
    {"type": "response.output_item.added", "item": {"id": "i2", "type": "function_call",
                                                     "name": "get_weather", "call_id": "call_1", "arguments": ""}},
    {"type": "response.function_call_arguments.delta", "call_id": "call_1", "delta": "{\"city\": "},
    {"type": "response.function_call_arguments.delta", "call_id": "call_1", "delta": "\"Paris\"}"},
    {"type": "response.done", "response": {"status": "completed", "usage": USAGE, "output": [
        {"id": "i2", "type": "function_call", "name": "get_weather", "call_id": "call_1",
         "arguments": "{\"city\": \"Paris\"}"}]}},
]

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather",
                                            "parameters": {"type": "object", "additionalProperties": False,
                                                           "properties": {"city": {"type": "string"}}}}}]


class FakeSocket:
    """Answers with ``session.created``, then the scripted events, then closes.

    ``hang=True`` sends nothing after the script instead of closing.
    """

    def __init__(self, script, *, first=None, hang=False):
        self.events = [first or {"type": "session.created", "session": {"type": "realtime"}}] + list(script)
        self.hang = hang
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.events:
            return json.dumps(self.events.pop(0))
        if self.hang:
            await asyncio.sleep(3600)
        raise websockets.exceptions.ConnectionClosedOK(None, None)

    async def close(self):
        self.closed = True


@pytest.fixture
def realtime(monkeypatch):
    """A realtime client whose WebSocket plays the given script."""
    connects = []

    def make(script, *, timeout=5.0, max_tokens=None, default_extra=None, verify=None, **socket_kwargs):
        socket = FakeSocket(script, **socket_kwargs)

        async def connect(url, **kwargs):
            connects.append((url, kwargs))
            return socket

        monkeypatch.setattr(websockets, "connect", connect)
        with patch("openai.AsyncOpenAI", MagicMock()):
            client = OpenAIAsyncClient(
                model="gpt-realtime-mini", api_key="sk-test", base_url="https://api.openai.com/v1",
                timeout=timeout, max_tokens=max_tokens, default_extra=default_extra, verify=verify,
                capabilities=ModelCapabilitiesConfig(default_api_type="realtime", tools=True,
                                                  image_input=True, audio_input=True))
        return client, socket, connects

    return make


def _request(socket):
    [event] = [e for e in socket.sent if e["type"] == "response.create"]
    return event["response"]


async def test_a_text_answer_comes_back_with_its_usage(realtime):
    client, socket, connects = realtime(TEXT_ANSWER)
    seen = []

    async def post(info):
        seen.append(info)

    client.set_llm_hooks(on_post_response=post)
    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert result["assistant"] == {"role": "assistant", "content": "Hello"}
    assert result["usage"] == {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80,
                               "prompt_tokens_details": {"cached_tokens": 32}}
    assert seen and seen[0]["usage"] == result["usage"], "the post-response hook never saw the usage"
    url, kwargs = connects[0]
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-mini"
    assert "OpenAI-Beta" not in kwargs["additional_headers"], "the beta API was removed on 2026-05-12"
    assert "ssl" not in kwargs, "without ssl_verify the default verification applies"
    assert socket.closed


async def test_ssl_verify_reaches_the_websocket(realtime):
    client, _, connects = realtime(TEXT_ANSWER, verify=False)

    await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert connects[0][1]["ssl"].verify_mode == ssl.CERT_NONE


async def test_the_request_is_one_response_with_the_whole_history(realtime):
    client, socket, _ = realtime(TEXT_ANSWER, max_tokens=10_000, default_extra={"temperature": 0.3})
    messages = [
        ChatMessage(role="system", content="Be terse."),
        ChatMessage(role="user", content="weather?"),
        ChatMessage(role="assistant", content="Checking.", tool_calls=[
            {"id": "call_0", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="call_0", content="sunny"),
        ChatMessage(role="system", content="Answer in German."),
    ]
    await client.chat_tools(messages, TOOLS)

    # Text output on the session, not the response: there gpt-realtime-2.1
    # reports input_tokens 0. The session default is audio.
    assert socket.sent[0] == {"type": "session.update", "session": {"type": "realtime", "output_modalities": ["text"]}}
    assert [e["type"] for e in socket.sent] == ["session.update", "response.create"], \
        "items must not be created one by one"
    request = _request(socket)
    assert request["conversation"] == "none"
    assert "output_modalities" not in request
    assert request["instructions"] == "Be terse."
    assert request["max_output_tokens"] == 10_000, "the model's ceiling is the entry's max_tokens"
    assert "temperature" not in request
    assert request["tools"] == [{"type": "function", "name": "get_weather", "description": "Weather",
                                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}]
    assert request["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking."}]},
        {"type": "function_call", "call_id": "call_0", "name": "get_weather", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_0", "output": "sunny"},
        {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "Answer in German."}]},
    ]


async def test_a_tool_call_keeps_its_name(realtime):
    client, _, _ = realtime(TOOL_CALL)
    seen = []

    async def post(info):
        seen.append(info)

    client.set_llm_hooks(on_post_response=post)
    result = await client.chat_tools([ChatMessage(role="user", content="weather in Paris?")], TOOLS)

    assert result["assistant"]["tool_calls"] == [
        {"id": "call_1", "type": "function",
         "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}]
    assert seen[0]["finish_reason"] == "tool_calls"
    assert seen[0]["is_streaming"] is False


async def test_streaming_names_the_tool_before_its_arguments_arrive(realtime):
    client, _, _ = realtime(TOOL_CALL)

    events = [e async for e in client.chat_tools_streaming([ChatMessage(role="user", content="?")], TOOLS)]

    deltas = [e for e in events if e["type"] == "tool_call_delta"]
    # The Chat Completions shape: id and name first, then argument pieces.
    assert deltas[0]["delta"] == {"id": "call_1", "function": {"name": "get_weather", "arguments": ""}}
    assert "".join(d["delta"]["function"]["arguments"] for d in deltas) == "{\"city\": \"Paris\"}"
    assert {d["index"] for d in deltas} == {0}
    assert deltas[-1]["accumulated"]["function"] == {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}
    assert events[-1]["type"] == "final" and events[-1]["usage"]["completion_tokens"] == 20


def test_reasoning_tokens_are_kept():
    """gpt-realtime-2.1-mini reports them under output_token_details."""
    usage = realtime_adapter.to_openai_usage({**USAGE, "output_token_details": {
        "text_tokens": 20, "audio_tokens": 0, "reasoning_tokens": 15}})

    assert usage["completion_tokens_details"] == {"reasoning_tokens": 15}


async def test_streaming_text_arrives_as_deltas(realtime):
    client, _, _ = realtime(TEXT_ANSWER)

    events = [e async for e in client.chat_tools_streaming([ChatMessage(role="user", content="hi")], [])]

    assert [e["delta"] for e in events if e["type"] == "content_delta"] == ["Hel", "lo"]
    assert events[-1]["assistant"]["content"] == "Hello"


async def test_a_response_cut_at_the_output_cap_keeps_its_text(realtime):
    """As Chat Completions: finish_reason "length", the agent loop decides."""
    client, _, _ = realtime([{"type": "response.done", "response": {
        "status": "incomplete", "status_details": {"type": "incomplete", "reason": "max_output_tokens"},
        "usage": USAGE, "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "a long ans"}]}]}}])

    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert result["assistant"] == {"role": "assistant", "content": "a long ans"}
    assert result["finish_reason"] == "length"
    assert result["usage"]["prompt_tokens"] == 60


async def test_a_response_the_content_filter_cut_keeps_its_text(realtime):
    client, _, _ = realtime([{"type": "response.done", "response": {
        "status": "incomplete", "status_details": {"type": "incomplete", "reason": "content_filter"},
        "usage": USAGE, "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "partial"}]}]}}])

    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert result["assistant"] == {"role": "assistant", "content": "partial"}
    assert result["finish_reason"] == "content_filter"


async def test_a_failed_response_is_an_error_the_server_can_read(realtime):
    client, _, _ = realtime([{"type": "response.done", "response": {
        "status": "failed", "output": [], "usage": USAGE,
        "status_details": {"type": "failed", "error": {"type": "server_error", "code": "overloaded"}}}}])

    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    # agent_system/servers/agent/server.py reads error.message and error.type
    error = result["assistant"]["error"]
    assert error["type"] == "upstream_error_server_error"
    assert "failed" in error["message"] and "overloaded" in error["message"]
    assert result["usage"]["prompt_tokens"] == 60, "a failed response is billed too"


async def test_an_error_event_is_an_error(realtime):
    client, _, _ = realtime([{"type": "error", "error": {"type": "invalid_request_error", "message": "bad item"}}])

    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert result["assistant"]["error"] == {"error": True, "type": "upstream_error_invalid_request_error",
                                            "message": "bad item"}


@pytest.mark.parametrize("kind", ["agent_role_gate", "foreign_session", "session_locked"])
async def test_a_server_error_type_never_passes_for_one_of_the_frameworks_own(realtime, kind):
    """The agent yields the LLM's error type as its run's error_type. Passed through as the server sent it, a
    type that happens to be a refusal of the framework's (REFUSED_BEFORE_THE_RUN) made a run that did run
    count as refused -- and its callers saved nothing of it."""
    from agent_system.servers.agent.server import REFUSED_BEFORE_THE_RUN

    client, _, _ = realtime([{"type": "error", "error": {"type": kind, "message": "odd"}}])

    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert result["assistant"]["error"]["type"] not in REFUSED_BEFORE_THE_RUN, result


async def test_a_closed_socket_ends_the_call(realtime):
    client, _, _ = realtime(TEXT_ANSWER[:3])

    result = await asyncio.wait_for(
        client.chat_tools([ChatMessage(role="user", content="hi")], []), timeout=5)

    assert "closed" in result["assistant"]["error"]["message"]


async def test_a_silent_socket_ends_the_call_after_the_timeout(realtime):
    client, _, _ = realtime(TEXT_ANSWER[:3], hang=True, timeout=0.2)

    result = await asyncio.wait_for(
        client.chat_tools([ChatMessage(role="user", content="hi")], []), timeout=5)

    assert "no event" in result["assistant"]["error"]["message"]


@pytest.mark.parametrize("script", [
    [{"type": "error", "error": {"type": "invalid_request_error", "message": "bad item"}}],
    TEXT_ANSWER[:3],
], ids=["error event", "closed socket"])
async def test_a_failed_call_reaches_the_post_response_hook(realtime, script):
    client, _, _ = realtime(script)
    seen = []

    async def post(info):
        seen.append(info)

    client.set_llm_hooks(on_post_response=post)
    await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert len(seen) == 1 and seen[0]["error"], "the debugger kept a request without its response"


class CancelAfter:
    """A cancellation token that turns after ``checks`` looks."""

    def __init__(self, checks):
        self.checks = checks

    @property
    def is_cancelled(self):
        self.checks -= 1
        return self.checks < 0


@pytest.mark.parametrize("checks, connected", [(0, 0), (1, 1)], ids=["before connecting", "while connecting"])
async def test_a_cancelled_request_is_not_sent(realtime, checks, connected):
    """CancelledError is what server.py reports as a cancel."""
    client, socket, connects = realtime(TEXT_ANSWER)

    with pytest.raises(asyncio.CancelledError):
        await client.chat_tools([ChatMessage(role="user", content="hi")], [], CancelAfter(checks))

    assert len(connects) == connected
    assert socket.sent == [], "a cancelled request was still sent, and billed"
    assert socket.closed or not connected


async def test_a_cancel_ends_a_silent_wait(realtime):
    client, socket, _ = realtime(TEXT_ANSWER[:3], hang=True, timeout=30)
    token = SimpleNamespace(is_cancelled=False)
    asyncio.get_running_loop().call_later(0.2, setattr, token, "is_cancelled", True)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(client.chat_tools([ChatMessage(role="user", content="hi")], [], token), timeout=5)

    assert socket.closed


async def test_attachments_are_encoded_off_the_event_loop(realtime, monkeypatch):
    """A tool attachment is read from disk and encoded, up to 25 MB."""
    client, _, _ = realtime(TEXT_ANSWER)
    threads = []
    convert = realtime_adapter.to_request_input

    def spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return convert(*args, **kwargs)

    monkeypatch.setattr(realtime_adapter, "to_request_input", spy)
    await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert threads and threads[0] != threading.get_ident()


def test_user_audio_becomes_a_note():
    _, items = realtime_adapter.to_request_input([ChatMessage(role="user", content=[
        {"type": "text", "text": "transcribe this"},
        {"type": "audio", "source": {"type": "base64", "media_type": "audio/wav", "data": "AAAA"}},
    ])])

    assert items == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "transcribe this"},
        {"type": "input_text", "text": realtime_adapter.AUDIO_NOTE}]}]


async def test_a_refused_session_closes_the_socket(realtime):
    client, socket, _ = realtime([], first={"type": "error", "error": {"message": "invalid model"}})

    result = await client.chat_tools([ChatMessage(role="user", content="hi")], [])

    assert "session.created" in result["assistant"]["error"]["message"]
    assert socket.closed, "a failed connect left the socket open"


async def test_chat_uses_the_realtime_path(realtime):
    client, socket, _ = realtime(TEXT_ANSWER)

    assert await client.chat([ChatMessage(role="user", content="hi")]) == "Hello"
    assert socket.sent, "chat() went to /v1/chat/completions, which the Realtime models do not serve"


def test_user_images_go_as_input_image():
    _, items = realtime_adapter.to_request_input([ChatMessage(role="user", content=[
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])])

    assert items == [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "What is this?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]


def test_the_websocket_url_follows_the_base_url():
    assert realtime_url(None) == "wss://api.openai.com/v1/realtime"
    assert realtime_url("https://gateway.example/v1/") == "wss://gateway.example/v1/realtime"
    assert realtime_url("http://localhost:8080/v1") == "ws://localhost:8080/v1/realtime"


def test_the_text_parts_of_a_tool_result_stay_apart():
    _, items = realtime_adapter.to_request_input([ChatMessage(role="tool", tool_call_id="c1", content=[
        {"type": "text", "text": "first result"}, {"type": "text", "text": "second result"}])])

    assert items == [{"type": "function_call_output", "call_id": "c1", "output": "first result\nsecond result"}]


async def test_an_image_a_tool_returned_is_sent_after_its_output(realtime, tmp_path):
    """The chat path injects it as a user message; so does this one."""
    from PIL import Image

    image = tmp_path / "render.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(image)
    client, socket, _ = realtime(TEXT_ANSWER)
    messages = [
        ChatMessage(role="user", content="render it"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "render", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="c1", name="render", content="rendered",
                    multimodal_content=[MultimodalToolContent(type="image", path=str(image), mime_type="image/png")]),
    ]

    await client.chat_tools(messages, [])

    items = _request(socket)["input"]
    assert items[-2] == {"type": "function_call_output", "call_id": "c1", "output": "rendered"}
    assert items[-1]["role"] == "user"
    assert any(part["type"] == "input_image" and part["image_url"].startswith("data:image/png;base64,")
               for part in items[-1]["content"]), items[-1]


async def test_chat_reports_an_error_with_its_message(realtime):
    client, _, _ = realtime([{"type": "error", "error": {"type": "invalid_request_error", "message": "bad item"}}])

    answer = json.loads(await client.chat([ChatMessage(role="user", content="hi")]))

    assert answer == {"_llm_error": {"error": True, "message": "bad item"}}


async def test_an_audio_attachment_becomes_a_note_not_a_silent_gap(realtime, tmp_path):
    """The model reports audio_input, but this path sends none: it is told so."""
    import wave

    take = tmp_path / "take.wav"
    with wave.open(str(take), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(2) * 80)
    client, socket, _ = realtime(TEXT_ANSWER)
    messages = [
        ChatMessage(role="user", content="speak"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "tts", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="c1", name="tts", content="the take",
                    multimodal_content=[MultimodalToolContent(type="audio", path=str(take), mime_type="audio/wav",
                                                              description="the take")]),
    ]

    await client.chat_tools(messages, [])

    note = " ".join(part.get("text", "") for part in _request(socket)["input"][-1]["content"])
    # The tool note names the attachment; the adapter's fallback note could not.
    assert "Audio file: the take - audio input not supported" in note, _request(socket)["input"][-1]
