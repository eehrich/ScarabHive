"""Every way a request ends reaches post_llm_response exactly once.

The message debugger, the otel chat span and the cost readers only see what
the client reports there. Driven against a scripted HTTP server on 127.0.0.1
through the real OpenAI SDK -- no mocks of the client.
"""
import asyncio
import json

import httpx
import pytest

from agent_system.core.cancellation import CancellationToken
from agent_system.llm.models import (
    ChatMessage, LLMConnectionError, LLMQuotaExhaustedError, LLMRateLimitError, LLMServerError,
)
from plugins.llm_openai.openai_client import OpenAIAsyncClient

MESSAGES = [ChatMessage(role="user", content="hi")]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def chunk(event) -> bytes:
    data = event if isinstance(event, str) else json.dumps(event)
    return f"data: {data}\n\n".encode()


def _delta_chunk(delta: dict, finish_reason=None, **extra) -> bytes:
    return chunk({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra})


def content(text: str) -> bytes:
    return _delta_chunk({"content": text})


STOP = _delta_chunk({}, "stop")
DONE = chunk("[DONE]")
USAGE_CHUNK = chunk({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                     "choices": [], "usage": USAGE})


def answer(text: str = "da", finish_reason: str = "stop") -> str:
    return json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                    "finish_reason": finish_reason}], "usage": USAGE})


def status(code: int, body: str = "{}"):
    async def script(reader, writer):
        raw = body.encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
    return script


def sse(*events: bytes, then: str = "end"):
    """A chunked event stream; after the events: end it, hang, or drop the connection."""
    async def script(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        for event in events:
            writer.write(f"{len(event):X}\r\n".encode() + event + b"\r\n")
            await writer.drain()
        if then == "hang":
            await reader.read()  # until the client closes the connection
            await asyncio.sleep(3600)
        elif then == "end":
            writer.write(b"0\r\n\r\n")
            await writer.drain()
    return script


def silent():
    async def script(reader, writer):
        await asyncio.sleep(3600)
    return script


class Upstream:
    """One script per connection, the last one repeated."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.hits = 0
        self.bodies: list = []
        self.closed_by_client = asyncio.Event()
        self._tasks: set = set()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/v1"
        return self

    async def __aexit__(self, *exc):
        for task in self._tasks:
            task.cancel()
        self._server.close()

    async def _serve(self, reader, writer):
        self._tasks.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")), 0)
        self.bodies.append(json.loads(await reader.readexactly(length)))
        script =self.scripts[min(self.hits, len(self.scripts) - 1)]
        self.hits += 1
        original_read = reader.read

        async def read_until_closed(*args):
            data = await original_read(*args)
            if not data:
                self.closed_by_client.set()
            return data

        reader.read = read_until_closed
        try:
            await script(reader, writer)
        finally:
            writer.close()


def make_client(url: str, *, attempts: int = 1, timeout: float = 5.0) -> OpenAIAsyncClient:
    return OpenAIAsyncClient(model="m", api_key="k", base_url=url, timeout=timeout,
                             max_attempts=attempts, base_backoff=0, min_backoff=0)


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

    def errors(self):
        return [info.get("error") for info in self.post]


async def call(client, streaming: bool, token=None):
    """chat_tools, or the stream collected; for a stream its last event."""
    if not streaming:
        return await client.chat_tools(MESSAGES, [], cancellation_token=token)
    events = [event async for event in client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)]
    return events[-1]


both = pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])


async def test_chat_reports_to_the_hooks_like_chat_tools():
    async with Upstream(status(200, answer())) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        assert await client.chat(MESSAGES) == "da"
    assert len(hooks.pre) == 1
    assert [(info.get("error"), info["usage"], info["finish_reason"]) for info in hooks.post] == [
        (None, USAGE, "stop")]


@both
async def test_the_finish_reason_reaches_the_caller(streaming):
    """The agent's truncation guard reads it: a cut answer must not pass for a whole one."""
    script = (sse(content("Hal"), _delta_chunk({}, "length"), DONE) if streaming
              else status(200, answer("Hal", "length")))
    async with Upstream(script) as upstream:
        result = await call(make_client(upstream.url), streaming)
    assert result["finish_reason"] == "length"


@both
async def test_the_thinking_of_an_earlier_turn_is_never_sent(streaming):
    """reasoning_content is kept on the message for us; OpenAI refuses a field it does not know."""
    script = sse(content("da"), STOP, DONE) if streaming else status(200, answer())
    history = [ChatMessage(role="user", content="hi"),
               ChatMessage(role="assistant", content="done", reasoning_content="why"),
               ChatMessage(role="user", content="and?")]
    async with Upstream(script) as upstream:
        client = make_client(upstream.url)
        if streaming:
            [_ async for _ in client.chat_tools_streaming(history, [])]
        else:
            await client.chat_tools(history, [])
    [body] = upstream.bodies
    assert body["messages"][1]["content"] == "done"
    assert "reasoning_content" not in body["messages"][1]


# ------------------------------------------------- refused after the retries

@both
@pytest.mark.parametrize("script, raised, error", [
    (status(404, '{"error": {"message": "no such model"}}'), httpx.HTTPStatusError, "HTTP 404"),
    (status(400, '{"error": {"message": "context_length_exceeded"}}'), httpx.HTTPStatusError, "HTTP 400"),
    (status(500), LLMServerError, "HTTP 500"),
    (status(429), LLMRateLimitError, "Rate limit exceeded"),
    (silent(), LLMConnectionError, "Network/protocol error"),
], ids=["404", "400", "5xx", "429", "timeout"])
async def test_a_refusal_raises_what_the_fallback_reads_and_is_reported_once(streaming, script, raised, error):
    """server.py falls back (and blocks a dead key) on these types; as an error dict a 4xx
    went through the generic upstream branch, and a 400 lost its text to a missing import."""
    async with Upstream(script) as upstream:
        client = make_client(upstream.url, attempts=2, timeout=0.5)
        hooks = Hooks(client)
        with pytest.raises(raised) as refused:
            await call(client, streaming)
        hits = upstream.hits
    status_error = isinstance(refused.value, httpx.HTTPStatusError)
    assert hits == (1 if status_error else 2), "the SDK's own retries multiply the client's"
    [end] = [info for info in hooks.post if info.get("finish_reason") != "retry"]
    assert end["error"].startswith(error) and end["is_streaming"] is streaming
    assert len(hooks.pre) == 1
    if status_error:
        assert "no such model" in str(refused.value) or "context_length_exceeded" in str(refused.value)


@pytest.mark.parametrize("value, seconds", [
    ("20", 20.0), ("1.5s", 1.5), ("200ms", 0.2), ("6m0s", 360.0), ("1h2m3.5s", 3723.5),
    ("", None), ("soon", None), ("6m0x", None),
])
def test_a_rate_limit_header_is_read_in_every_unit_openai_sends(value, seconds):
    from plugins.llm_openai.openai_client import _seconds

    assert _seconds(value) == seconds


def _rate_limited(headers: str, body: str = '{"error": {"message": "slow down"}}'):
    async def script(reader, writer):
        raw = body.encode()
        writer.write(f"HTTP/1.1 429 X\r\nContent-Type: application/json\r\n{headers}"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
    return script


@both
async def test_a_rate_limit_longer_than_the_backoff_cap_is_not_slept(streaming):
    """OpenAI names its token reset as "6m0s": the server blocks the model for it and falls back."""
    async with Upstream(_rate_limited("x-ratelimit-reset-tokens: 6m0s\r\n")) as upstream:
        client = make_client(upstream.url, attempts=3)
        hooks = Hooks(client)
        with pytest.raises(LLMRateLimitError) as limited:
            await asyncio.wait_for(call(client, streaming), 10)
        hits = upstream.hits
    assert hits == 1 and limited.value.retry_after == 360.0
    assert len(hooks.post) == 1


@both
async def test_a_spent_quota_is_not_retried(streaming):
    body = '{"error": {"message": "You exceeded your current quota", "type": "insufficient_quota"}}'
    async with Upstream(_rate_limited("", body)) as upstream:
        client = make_client(upstream.url, attempts=3)
        hooks = Hooks(client)
        with pytest.raises(LLMQuotaExhaustedError):
            await call(client, streaming)
        hits = upstream.hits
    assert hits == 1 and len(hooks.post) == 1


async def _broken(**kwargs):
    raise RuntimeError("detector broke")


@both
async def test_an_error_nobody_expected_is_reported_once(streaming):
    async with Upstream(status(200, answer())) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        client._client.chat.completions.create = _broken
        result = await call(client, streaming)
    assert result["assistant"]["error"]["message"] == "detector broke"
    assert hooks.errors() == ["detector broke"]


# ------------------------------------------------------------- cancellation

@both
async def test_a_token_cancelled_before_the_request_left_is_reported(streaming):
    async with Upstream(silent()) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call(client, streaming, token)
        assert upstream.hits == 0
    assert len(hooks.pre) == 1 and hooks.errors() == ["Request cancelled by user"]


async def test_a_request_cancelled_by_its_token_while_out_is_reported():
    async with Upstream(silent()) as upstream:
        client = make_client(upstream.url, timeout=30)
        hooks = Hooks(client)
        token = CancellationToken("req")
        asyncio.get_running_loop().call_later(0.3, token.cancel)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(client.chat_tools(MESSAGES, [], cancellation_token=token), 10)
    assert hooks.errors() == ["Request cancelled by user"]


@pytest.mark.parametrize("with_usage", [False, True], ids=["no usage yet", "usage came"])
async def test_a_stream_cancelled_by_its_token_is_reported_once(with_usage):
    events = ([USAGE_CHUNK] if with_usage else []) + [content("Hal")]
    async with Upstream(sse(*events, then="hang")) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        with pytest.raises(asyncio.CancelledError):
            async for _ in client.chat_tools_streaming(MESSAGES, [], cancellation_token=token):
                token.cancel()
    assert len(hooks.post) == 1 and "cancelled" in hooks.post[0]["error"]
    assert hooks.post[0]["usage"] == (USAGE if with_usage else None)


@both
async def test_a_request_whose_task_is_cancelled_is_reported_and_stays_cancelled(streaming):
    script = sse(content("Hal"), then="hang") if streaming else silent()
    async with Upstream(script) as upstream:
        client = make_client(upstream.url, timeout=30)
        hooks = Hooks(client)
        task = asyncio.create_task(call(client, streaming))
        while upstream.hits == 0:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled()
    assert hooks.errors() == ["cancelled"]


@both
async def test_a_cancel_during_the_retry_wait_is_reported(streaming):
    async with Upstream(status(500)) as upstream:
        client = make_client(upstream.url, attempts=2)
        client._retry_min_backoff = 30.0
        token = CancellationToken("req")
        hooks = Hooks(client, on_post=lambda info: token.cancel())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call(client, streaming, token), 10)
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", None]
    assert hooks.post[1]["error"] == "Request cancelled during retry wait"


@both
async def test_a_cancel_while_the_answer_is_reported_does_not_report_it_twice(streaming):
    script = sse(content("da"), STOP, USAGE_CHUNK, DONE) if streaming else status(200, answer())

    def cancel_on_answer(info):
        if not info.get("error"):
            raise asyncio.CancelledError()

    async with Upstream(script) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client, on_post=cancel_on_answer)
        with pytest.raises(asyncio.CancelledError):
            await call(client, streaming)
    assert len(hooks.post) == 1 and hooks.post[0].get("error") is None


async def test_a_stream_the_caller_abandons_is_reported_and_closed_at_once():
    async with Upstream(sse(content("Hal"), then="hang")) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        stream = client.chat_tools_streaming(MESSAGES, [])
        assert (await stream.__anext__())["type"] == "content_delta"
        await stream.aclose()
        assert hooks.errors() == ["stream abandoned by the caller"]
        # The connection, too: an abandoned response otherwise holds it until collected.
        await asyncio.wait_for(upstream.closed_by_client.wait(), 2)


# ------------------------------------------------------------ stream restart

def _types(events):
    return [event["type"] for event in events]


async def test_a_retry_after_deltas_tells_the_caller_to_start_over_once():
    """Attempt one sends a delta and drops, attempt two fails before any, attempt three answers."""
    async with Upstream(sse(content("Hal"), then="drop"), status(500),
                        sse(content("ganz"), STOP, DONE)) as upstream:
        client = make_client(upstream.url, attempts=3)
        events = [event async for event in client.chat_tools_streaming(MESSAGES, [])]
    kinds = _types(events)
    assert kinds.count("stream_restart") == 1
    assert kinds[kinds.index("stream_restart") + 1:] == ["content_delta", "final"]
    assert events[-1]["assistant"]["content"] == "ganz"


async def test_a_retry_before_any_delta_sends_no_restart():
    async with Upstream(status(500), sse(content("ganz"), STOP, DONE)) as upstream:
        client = make_client(upstream.url, attempts=2)
        events = [event async for event in client.chat_tools_streaming(MESSAGES, [])]
    assert _types(events) == ["content_delta", "final"]
