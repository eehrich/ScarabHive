"""Every way an Anthropic request ends reaches post_llm_response exactly once,
and a failure reaches the agent server as a type its profile fallback catches.

Driven through the real SDK against a scripted HTTP server on 127.0.0.1 -- no
mocks of the client or of the SDK.
"""
import asyncio
import json

import httpx
import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.core.cancellation import CancellationToken
from agent_system.llm.models import (
    ChatMessage, LLMConnectionError, LLMRateLimitError, LLMServerError,
)
from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
from plugins.llm_anthropic.provider import build_anthropic

MESSAGES = [ChatMessage(role="user", content="hi")]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def _event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


START = _event("message_start", {"type": "message_start", "message": {
    "id": "msg_1", "type": "message", "role": "assistant", "content": [], "model": "m",
    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 1}}})
TEXT_START = _event("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}})
PING = _event("ping", {"type": "ping"})


def text(delta: str) -> bytes:
    return _event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": delta}})


END = [_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
       _event("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": 3}}),
       _event("message_stop", {"type": "message_stop"})]


def error_event(kind: str, message: str) -> bytes:
    return _event("error", {"type": "error", "error": {"type": kind, "message": message}})


def api_error(code: int, kind: str, message: str = "nope", headers: str = ""):
    return status(code, json.dumps({"type": "error", "error": {"type": kind, "message": message}}),
                  headers)


def status(code: int, body: str, headers: str = ""):
    async def script(writer):
        raw = body.encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n{headers}"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
    return script


def sse(*events: bytes, then: str = "end"):
    """A chunked event stream; after the events: end it, hang, or drop the connection."""
    async def script(writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        for event in events:
            writer.write(f"{len(event):X}\r\n".encode() + event + b"\r\n")
            await writer.drain()
        if then == "hang":
            await asyncio.sleep(3600)
        elif then == "end":
            writer.write(b"0\r\n\r\n")
            await writer.drain()
    return script


ANSWER = sse(START, TEXT_START, text("da"), *END)


def silent():
    async def script(writer):
        await asyncio.sleep(3600)
    return script


class Upstream:
    """One script per connection, the last one repeated."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.hits = 0
        self._tasks: set = set()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        for task in self._tasks:
            task.cancel()
        self._server.close()

    async def _serve(self, reader, writer):
        self._tasks.add(asyncio.current_task())
        head = await reader.readuntil(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")), 0)
        await reader.readexactly(length)
        script = self.scripts[min(self.hits, len(self.scripts) - 1)]
        self.hits += 1
        try:
            await script(writer)
        finally:
            writer.close()


class Hooks:
    def __init__(self, client, on_post=None):
        self.pre: list = []
        self.post: list = []

        async def pre(info):
            self.pre.append(info)

        async def post(info):
            self.post.append(info)
            if on_post:
                on_post(info)

        client.set_llm_hooks(on_pre_request=pre, on_post_response=post)


def client_for(url: str, *, timeout: float = 5.0, retries: int = 0, rate_retries: int = 0):
    return AnthropicAsyncClient(model="m", api_key="k", base_url=url, request_timeout=timeout,
                                max_retries=retries, rate_limit_max_retries=rate_retries)


async def _drain(gen, on_chunk=None):
    async for chunk in gen:
        if on_chunk:
            on_chunk(chunk)


def _types(chunks):
    return [chunk["type"] for chunk in chunks]


async def _stream_of(client):
    return [chunk async for chunk in client.chat_tools_streaming(MESSAGES, [])]


# ------------------------------------------------------------------ answers

async def test_an_answer_is_reported_once_with_its_usage_and_finish_reason():
    async with Upstream(ANSWER) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        result = await client.chat_tools(MESSAGES, [])
    assert result["assistant"]["content"] == "da"
    assert len(hooks.pre) == 1
    assert [(info.get("error"), info["usage"], info["finish_reason"]) for info in hooks.post] == [
        (None, USAGE, "stop")]


async def test_chat_is_reported_like_chat_tools():
    """llm_router and every other chat() caller were invisible to the hooks."""
    answer = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
              "content": [{"type": "text", "text": "da"}], "stop_reason": "end_turn",
              "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3}}
    async with Upstream(status(200, json.dumps(answer))) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        assert await client.chat(MESSAGES) == "da"
    assert [info["is_streaming"] for info in hooks.pre] == [False]
    assert [(info.get("error"), info["usage"], info["finish_reason"]) for info in hooks.post] == [
        (None, USAGE, "stop")]


@pytest.mark.parametrize("script, raised, error", [
    (api_error(400, "invalid_request_error"), httpx.HTTPStatusError, "HTTP 400: nope"),
    (api_error(429, "rate_limit_error"), LLMRateLimitError, "Rate limit: nope"),
    (api_error(529, "overloaded_error"), LLMServerError, "HTTP 529: nope"),
], ids=["4xx", "429", "529"])
async def test_a_refused_chat_is_reported_and_typed(script, raised, error):
    async with Upstream(script) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        with pytest.raises(raised):
            await client.chat(MESSAGES)
    assert [info["error"] for info in hooks.post] == [error]


async def test_a_chat_cancelled_before_it_left_is_reported():
    async with Upstream(silent()) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.chat(MESSAGES, cancellation_token=token)
        assert upstream.hits == 0
    assert [info["error"] for info in hooks.post] == ["Request cancelled by user"]


# ------------------------------------------------------------- cancellation

async def test_a_stream_cancelled_by_its_token_is_reported_once():
    async with Upstream(sse(START, TEXT_START, text("Hal"), then="hang")) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        gen = client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_drain(gen, lambda chunk: token.cancel()), 10)
    assert len(hooks.pre) == 1
    assert [info["error"] for info in hooks.post] == ["Request cancelled during streaming"]


async def test_a_cancel_while_the_model_sends_only_pings_ends_the_stream_at_once():
    """The SDK swallows pings; a cancel used to wait for the next real event (minutes of thinking)."""
    async with Upstream(sse(START, PING, then="hang")) as upstream:
        client = client_for(upstream.url, timeout=30.0)
        hooks = Hooks(client)
        token = CancellationToken("req")
        gen = client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)
        task = asyncio.create_task(_drain(gen))
        while upstream.hits == 0:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert [info["error"] for info in hooks.post] == ["Request cancelled during streaming"]


async def test_a_stream_whose_task_is_cancelled_is_reported_and_stays_cancelled():
    async with Upstream(sse(START, TEXT_START, text("Hal"), then="hang")) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        started = asyncio.Event()
        task = asyncio.create_task(_drain(client.chat_tools_streaming(MESSAGES, []),
                                          lambda chunk: started.set()))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled()
    assert [info["error"] for info in hooks.post] == ["cancelled"]


async def test_a_token_cancelled_before_the_request_left_is_reported():
    async with Upstream(silent()) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.chat_tools(MESSAGES, [], cancellation_token=token)
        assert upstream.hits == 0
    assert len(hooks.pre) == 1
    assert [info["error"] for info in hooks.post] == ["Request cancelled by user"]


async def test_a_cancel_while_the_answer_is_reported_does_not_report_it_twice():
    def cancel_on_answer(info):
        if not info.get("error"):
            raise asyncio.CancelledError()

    async with Upstream(ANSWER) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client, on_post=cancel_on_answer)
        with pytest.raises(asyncio.CancelledError):
            await client.chat_tools(MESSAGES, [])
    assert len(hooks.post) == 1 and hooks.post[0].get("error") is None


async def test_a_cancel_during_the_retry_wait_is_reported():
    async with Upstream(api_error(529, "overloaded_error", "Overloaded")) as upstream:
        client = client_for(upstream.url, retries=1)
        token = CancellationToken("req")
        hooks = Hooks(client, on_post=lambda info: token.cancel())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(client.chat_tools(MESSAGES, [], cancellation_token=token), 10)
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", None]
    assert hooks.post[1]["error"] == "Request cancelled during retry wait"


async def test_a_stream_the_caller_abandons_is_reported():
    async with Upstream(sse(START, TEXT_START, text("Hal"), then="hang")) as upstream:
        client = client_for(upstream.url)
        hooks = Hooks(client)
        gen = client.chat_tools_streaming(MESSAGES, [])
        assert (await gen.__anext__())["type"] == "content_delta"
        await gen.aclose()
    assert [info["error"] for info in hooks.post] == ["stream abandoned by the caller"]


async def test_a_failure_nobody_expected_is_reported_once():
    async with Upstream(ANSWER) as upstream:
        client = client_for(upstream.url)

        def broken(message):
            raise RuntimeError("serializer broke")

        client._serialize_thinking_blocks = broken
        hooks = Hooks(client)
        with pytest.raises(RuntimeError):
            await client.chat_tools(MESSAGES, [])
    assert [info["error"] for info in hooks.post] == ["serializer broke"]


# -------------------------------------------------- refused after the retries

@pytest.mark.parametrize("script, raised, error", [
    (api_error(400, "invalid_request_error"), httpx.HTTPStatusError, "HTTP 400: nope"),
    (api_error(404, "not_found_error"), httpx.HTTPStatusError, "HTTP 404: nope"),
    (api_error(429, "rate_limit_error"), LLMRateLimitError, "Rate limit: nope"),
    (api_error(500, "api_error"), LLMServerError, "HTTP 500: nope"),
    (api_error(529, "overloaded_error"), LLMServerError, "HTTP 529: nope"),
    (sse(START, TEXT_START, text("Hal"), error_event("overloaded_error", "Overloaded")),
     LLMServerError, "HTTP 529: Overloaded"),
    (silent(), LLMConnectionError, "Network/protocol error"),
    (sse(START, TEXT_START, text("Hal"), then="drop"), LLMConnectionError, "Network/protocol error"),
], ids=["400", "404", "429", "500", "529", "error event", "timeout", "dropped"])
async def test_a_stream_refused_after_its_retries_is_reported_and_typed(script, raised, error):
    """The server's profile fallback catches these types; the SDK's own exceptions it does not."""
    async with Upstream(script) as upstream:
        client = client_for(upstream.url, timeout=0.5)
        hooks = Hooks(client)
        with pytest.raises(raised) as refused:
            await client.chat_tools(MESSAGES, [])
    assert len(hooks.post) == 1, hooks.post
    assert hooks.post[0]["error"].startswith(error), hooks.post[0]["error"]
    assert hooks.post[0]["is_streaming"] is True
    if raised is httpx.HTTPStatusError:
        assert refused.value.response.status_code == int(error[5:8])


async def test_a_rate_limit_waits_as_long_as_the_api_asks(monkeypatch):
    waits = []

    async def sleep(self, seconds, token):
        waits.append(seconds)

    monkeypatch.setattr(AnthropicAsyncClient, "_cancellable_sleep", sleep)
    async with Upstream(api_error(429, "rate_limit_error", headers="retry-after: 4\r\n"),
                        ANSWER) as upstream:
        client = client_for(upstream.url, rate_retries=1)
        result = await client.chat_tools(MESSAGES, [])
    assert result["assistant"]["content"] == "da"
    assert len(waits) == 1 and 4.0 <= waits[0] <= 6.0


# ------------------------------------------------------------ stream restart

@pytest.mark.parametrize("first", [
    sse(START, TEXT_START, text("Hal"), then="drop"),
    sse(START, TEXT_START, text("Hal"), error_event("overloaded_error", "Overloaded")),
], ids=["dropped", "error event"])
async def test_a_retry_after_deltas_tells_the_caller_to_start_over_once(first, monkeypatch):
    """Attempt one sends a delta and fails, attempt two fails before any, attempt three answers."""
    async def sleep(self, seconds, token):
        pass

    monkeypatch.setattr(AnthropicAsyncClient, "_cancellable_sleep", sleep)
    async with Upstream(first, api_error(500, "api_error"), ANSWER) as upstream:
        client = client_for(upstream.url, retries=2)
        hooks = Hooks(client)
        chunks = await _stream_of(client)
    kinds = _types(chunks)
    assert kinds == ["content_delta", "stream_restart", "content_delta", "final"]
    assert chunks[-1]["assistant"]["content"] == "da"
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", "retry", "stop"]


async def test_a_retry_before_any_delta_sends_no_restart(monkeypatch):
    async def sleep(self, seconds, token):
        pass

    monkeypatch.setattr(AnthropicAsyncClient, "_cancellable_sleep", sleep)
    async with Upstream(api_error(529, "overloaded_error"), ANSWER) as upstream:
        client = client_for(upstream.url, retries=1)
        chunks = await _stream_of(client)
    assert _types(chunks) == ["content_delta", "final"]


# ----------------------------------------------------------------- provider

@pytest.mark.parametrize("entry, timeout, window", [
    ({}, 180, 200000),
    ({"request_timeout": 600, "context_window": 1000000}, 600, 1000000),
], ids=["unset", "set"])
def test_the_provider_defaults_apply_unless_the_entry_sets_the_key(entry, timeout, window):
    """Both fields have model defaults (120, 32768); truthiness made the route's own unreachable."""
    client = build_anthropic(LLMModelConfig(provider="anthropic", model="m", api_key="k", **entry))
    assert (client.request_timeout, client.context_window) == (timeout, window)


@pytest.mark.parametrize("entry, refused", [
    ({"include_thoughts": True}, True),
    ({"include_thoughts": True, "max_tokens": 16384, "thinking_budget": 16384}, True),
    ({"include_thoughts": True, "max_tokens": 16384}, False),
    ({"include_thoughts": True, "thinking_request_shape": "adaptive"}, False),
    ({}, False),
], ids=["defaults", "equal", "room", "adaptive", "no thinking"])
def test_a_budget_not_below_max_tokens_is_refused_when_built(entry, refused):
    """The API answers 400 on every call otherwise."""
    cfg = LLMModelConfig(provider="anthropic", model="m", api_key="k", **entry)
    if refused:
        with pytest.raises(ValueError, match="thinking budget .* must be below max_tokens"):
            build_anthropic(cfg)
    else:
        build_anthropic(cfg)


@pytest.mark.parametrize("header", ["inf", "nan", "-5"])
async def test_a_retry_after_that_is_no_wait_counts_as_not_asked(header, monkeypatch):
    waits = []

    async def sleep(self, seconds, token):
        waits.append(seconds)

    monkeypatch.setattr(AnthropicAsyncClient, "_cancellable_sleep", sleep)
    async with Upstream(api_error(429, "rate_limit_error", headers=f"retry-after: {header}\r\n"),
                        ANSWER) as upstream:
        client = client_for(upstream.url, rate_retries=1)
        await client.chat_tools(MESSAGES, [])
    assert len(waits) == 1 and 60.0 <= waits[0] <= 90.0


async def test_a_long_retry_after_goes_to_the_server_instead_of_being_slept(monkeypatch):
    """While the client sleeps a day, the server's fallback chain cannot run."""
    async def sleep(self, seconds, token):
        raise AssertionError(f"slept {seconds}")

    monkeypatch.setattr(AnthropicAsyncClient, "_cancellable_sleep", sleep)
    async with Upstream(api_error(429, "rate_limit_error", headers="retry-after: 86400\r\n")) as upstream:
        client = client_for(upstream.url, rate_retries=2)
        hooks = Hooks(client)
        with pytest.raises(LLMRateLimitError) as limited:
            await client.chat_tools(MESSAGES, [])
        assert upstream.hits == 1
    assert limited.value.retry_after == 86400.0
    assert [info.get("finish_reason") for info in hooks.post] == [None]
