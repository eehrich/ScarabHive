"""Every way a request ends reaches post_llm_response exactly once.

The message debugger, the otel chat span and the cost readers only see what
the client reports there. A request cancelled mid-stream (tokens billed), or
refused after its retries, used to end with a pre_llm_request and no response
at all. Driven against a scripted HTTP server on 127.0.0.1 -- the real httpx
path, no mocks of the client.
"""
import asyncio
import json

import httpx
import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.core.cancellation import CancellationToken
from agent_system.llm.models import LLMConnectionError, LLMRateLimitError, LLMServerError
from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig
from plugins.llm_openai_compat.openai_responses_client import OpenAIResponsesClient
from plugins.llm_openai_compat.provider import _timeout_config

MESSAGES = [{"role": "user", "content": "hi"}]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def _chunk(event: dict | str) -> bytes:
    data = event if isinstance(event, str) else json.dumps(event)
    return f"data: {data}\n\n".encode()


def _content(text: str) -> bytes:
    return _chunk({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})


def status(code: int, body: str = "{}"):
    async def script(writer):
        raw = body.encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
    return script


def sse(*events: bytes, then: str = "end", content_type: str = "text/event-stream"):
    """A chunked event stream; after the events: end it, hang, or drop the connection."""
    async def script(writer):
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                     "Transfer-Encoding: chunked\r\n\r\n".encode())
        for event in events:
            writer.write(f"{len(event):X}\r\n".encode() + event + b"\r\n")
            await writer.drain()
        if then == "hang":
            await asyncio.sleep(3600)
        elif then == "end":
            writer.write(b"0\r\n\r\n")
            await writer.drain()
    return script


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
        self.url = f"http://127.0.0.1:{port}/v1"
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


def chat_client(url: str, *, streaming: bool = True, read: float = 5.0, **kw) -> HTTPXOpenAIClient:
    kw.setdefault("max_retries", 0)
    kw.setdefault("rate_limit_max_retries", 0)
    return HTTPXOpenAIClient(
        model="m", api_key="k", base_url=url, retry_backoff=0,
        timeout_config=HTTPXTimeoutConfig(connect=2.0, read=read, write=2.0, pool=2.0),
        capabilities={"streaming": streaming}, **kw)


async def _drain(gen, on_chunk=None):
    async for chunk in gen:
        if on_chunk:
            on_chunk(chunk)


# ------------------------------------------------------------- cancellation

@pytest.mark.parametrize("with_usage", [False, True], ids=["no usage yet", "usage came"])
async def test_a_stream_cancelled_by_its_token_is_reported_once(with_usage):
    # the usage chunk first, so it has certainly been read when the text arrives
    events = ([_chunk({"choices": [], "usage": USAGE})] if with_usage else []) + [_content("Hal")]
    async with Upstream(sse(*events, then="hang")) as upstream:
        client = chat_client(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        gen = client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_drain(gen, lambda chunk: token.cancel()), 10)

    assert len(hooks.pre) == 1
    assert len(hooks.post) == 1, hooks.post
    info = hooks.post[0]
    assert "cancelled" in info["error"] and info["is_streaming"] is True
    assert info["usage"] == (USAGE if with_usage else None)


async def test_a_stream_whose_task_is_cancelled_is_reported_and_stays_cancelled():
    async with Upstream(sse(_content("Hal"), then="hang")) as upstream:
        client = chat_client(upstream.url)
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


async def test_a_non_streaming_request_whose_task_is_cancelled_is_reported():
    async with Upstream(silent()) as upstream:
        client = chat_client(upstream.url, streaming=False)
        hooks = Hooks(client)
        task = asyncio.create_task(client.chat_tools(MESSAGES, []))
        while upstream.hits == 0:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [(info["error"], info["is_streaming"]) for info in hooks.post] == [("cancelled", False)]


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_token_cancelled_before_the_request_left_is_reported(streaming):
    async with Upstream(silent()) as upstream:
        client = chat_client(upstream.url, streaming=streaming)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.chat_tools(MESSAGES, [], cancellation_token=token)
        assert upstream.hits == 0
    assert len(hooks.pre) == 1
    assert [info["error"] for info in hooks.post] == ["Request cancelled by user"]


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_cancel_while_the_answer_is_reported_does_not_report_it_twice(streaming):
    """The answer's own report is where the cancel lands: it must not turn into a second, failed one."""
    answer = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "da"},
                           "finish_reason": "stop"}], "usage": USAGE}
    script = (sse(_content("da"), _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                         "usage": USAGE}), _chunk("[DONE]"))
              if streaming else status(200, json.dumps(answer)))

    def cancel_on_answer(info):
        if not info.get("error"):
            raise asyncio.CancelledError()

    async with Upstream(script) as upstream:
        client = chat_client(upstream.url, streaming=streaming)
        hooks = Hooks(client, on_post=cancel_on_answer)
        with pytest.raises(asyncio.CancelledError):
            await client.chat_tools(MESSAGES, [])
    assert len(hooks.post) == 1
    assert hooks.post[0].get("error") is None and hooks.post[0]["usage"] == USAGE


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_cancel_during_the_retry_wait_is_reported(streaming):
    """The wait sits in an except branch, where the attempt's own handlers no longer catch."""
    script = sse(_content("Hal"), then="drop") if streaming else silent()
    async with Upstream(script) as upstream:
        client = chat_client(upstream.url, streaming=streaming, read=0.3, max_retries=1)
        client.retry_backoff = 30.0
        hooks = Hooks(client, on_post=lambda info: token.cancel())
        token = CancellationToken("req")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(client.chat_tools(MESSAGES, [], cancellation_token=token), 10)
    assert len(hooks.post) == 2, hooks.post
    assert hooks.post[0]["finish_reason"] == "retry"
    assert hooks.post[1]["error"] == "Request cancelled during retry wait"


async def test_a_stream_the_caller_abandons_is_reported():
    async with Upstream(sse(_content("Hal"), then="hang")) as upstream:
        client = chat_client(upstream.url)
        hooks = Hooks(client)
        gen = client.chat_tools_streaming(MESSAGES, [])
        assert (await gen.__anext__())["type"] == "content_delta"
        await gen.aclose()
    assert [info["error"] for info in hooks.post] == ["stream abandoned by the caller"]


async def test_a_responses_stream_the_caller_abandons_is_reported():
    delta = _chunk({"type": "response.output_text.delta", "delta": "Hal"})
    async with Upstream(sse(delta, then="hang")) as upstream:
        client = _responses_client(upstream.url, streaming=True)
        hooks = Hooks(client)
        gen = client.chat_tools_streaming(MESSAGES, [])
        assert (await gen.__anext__())["type"] == "content_delta"
        await gen.aclose()
    assert [info["error"] for info in hooks.post] == ["stream abandoned by the caller"]


_UPSTREAM_ERROR = _chunk({"error": {"code": 502, "message": "Provider disconnected"},
                          "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]})
_FINISH_ERROR = _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]})


@pytest.mark.parametrize("event, detail", [(_UPSTREAM_ERROR, "Provider disconnected"),
                                           (_FINISH_ERROR, "finish_reason error")],
                         ids=["error chunk", "finish_reason only"])
async def test_an_upstream_error_mid_stream_is_no_answer(event, detail):
    async with Upstream(sse(_content("Hal"), event, _chunk("[DONE]"))) as upstream:
        client = chat_client(upstream.url)
        hooks = Hooks(client)
        with pytest.raises(LLMConnectionError):
            await client.chat_tools(MESSAGES, [])
    assert len(hooks.post) == 1
    assert hooks.post[0]["error"] == f"Network/protocol error: Upstream error in stream: {detail}"


async def test_an_upstream_error_mid_stream_is_retried_from_scratch():
    good = sse(_content("ganz"), _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
               _chunk("[DONE]"))
    async with Upstream(sse(_content("Hal"), _UPSTREAM_ERROR, _chunk("[DONE]")), good) as upstream:
        client = chat_client(upstream.url, max_retries=1)
        result = await client.chat_tools(MESSAGES, [])
    assert result["assistant"]["content"] == "ganz" and result["finish_reason"] == "stop"


# -------------------------------------------------- refused after the retries

def _body_error(code: int, message: str) -> bytes:
    return _chunk({"error": {"code": code, "message": message}})


@pytest.mark.parametrize("script, raised, error", [
    (status(400, '{"error": "bad"}'), httpx.HTTPStatusError, "HTTP 400"),
    (status(500, "boom"), LLMServerError, "HTTP 500"),
    (status(429, "slow down"), LLMRateLimitError, "Rate limit: slow down"),
    (sse(_body_error(429, "too many requests"), then="end"), LLMRateLimitError, "Upstream 429 body-error"),
    (sse(_body_error(400, "Provider returned error"), then="end"), httpx.HTTPStatusError, "Unrecoverable body-400"),
    (silent(), LLMConnectionError, "Request timed out"),
    (sse(_content("Hal"), then="drop"), LLMConnectionError, "Network/protocol error"),
], ids=["4xx", "5xx", "429", "body-429", "body-400", "timeout", "dropped"])
async def test_a_stream_refused_after_its_retries_is_reported(script, raised, error):
    async with Upstream(script) as upstream:
        client = chat_client(upstream.url, read=0.5)
        hooks = Hooks(client)
        with pytest.raises(raised):
            await client.chat_tools(MESSAGES, [])
    assert len(hooks.post) == 1, hooks.post
    assert hooks.post[0]["error"].startswith(error), hooks.post[0]["error"]
    assert hooks.post[0]["is_streaming"] is True


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_refusal_reaches_the_caller_as_the_status_error(streaming):
    """The agent server falls back to the next profile (and blocks a dead key or model) on
    httpx.HTTPStatusError; the streaming path used to hand over a plain Exception instead."""
    async with Upstream(status(404, '{"error": "no such model"}')) as upstream:
        client = chat_client(upstream.url, streaming=streaming)
        with pytest.raises(httpx.HTTPStatusError) as refused:
            await client.chat_tools(MESSAGES, [])
    assert refused.value.response.status_code == 404


async def test_a_non_streaming_request_that_never_got_through_is_reported():
    async with Upstream(silent()) as upstream:
        client = chat_client(upstream.url, streaming=False, read=0.5)
        hooks = Hooks(client)
        with pytest.raises(LLMConnectionError):
            await client.chat_tools(MESSAGES, [])
    assert [info["error"] for info in hooks.post] == ["Request failed after 1 attempts (ReadTimeout)"]


# ---------------------------------------------------------- Responses route

def _responses_client(url: str, streaming: bool) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        model="m", api_key="k", base_url=url, max_retries=1, retry_backoff=0,
        capabilities={"streaming": streaming},
        timeout_config=HTTPXTimeoutConfig(connect=2.0, read=5.0, write=2.0, pool=2.0))


async def test_a_responses_stream_cancelled_by_its_token_is_reported_once():
    delta = _chunk({"type": "response.output_text.delta", "delta": "Hal"})
    async with Upstream(sse(delta, then="hang")) as upstream:
        client = _responses_client(upstream.url, streaming=True)
        hooks = Hooks(client)
        token = CancellationToken("req")
        gen = client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_drain(gen, lambda chunk: token.cancel()), 10)
    assert len(hooks.pre) == 1
    assert len(hooks.post) == 1 and "cancelled" in hooks.post[0]["error"]


async def test_a_responses_body_that_is_no_json_is_reported_as_a_retry():
    answer = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "da"}]}],
              "usage": {"input_tokens": 7, "output_tokens": 3}}
    async with Upstream(status(200, "not json"), status(200, json.dumps(answer))) as upstream:
        client = _responses_client(upstream.url, streaming=False)
        hooks = Hooks(client)
        result = await client.chat_tools(MESSAGES, [])
    assert result["assistant"]["content"] == "da"
    assert len(hooks.pre) == 2
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", None]


# ------------------------------------------------------------------ timeouts

def test_a_timeout_the_entry_does_not_set_falls_back_to_the_route_default():
    cfg = LLMModelConfig(provider="openai_responses", model="m", request_timeout=500,
                         httpx_timeouts={"connect": 3.0})
    timeouts = _timeout_config(cfg, default_read=600.0, default_write=30.0)
    assert (timeouts.connect, timeouts.read, timeouts.write, timeouts.pool) == (3.0, 500.0, 30.0, 5.0)


def test_a_timeout_the_entry_sets_wins():
    cfg = LLMModelConfig(provider="openai_httpx", model="m", request_timeout=500,
                         httpx_timeouts={"connect": 1.0, "read": 60.0, "write": 2.0, "pool": 4.0})
    timeouts = _timeout_config(cfg, default_read=180.0, default_write=10.0)
    assert (timeouts.connect, timeouts.read, timeouts.write, timeouts.pool) == (1.0, 60.0, 2.0, 4.0)


@pytest.mark.parametrize("timeouts", [None, {"connect": 3.0}], ids=["no httpx_timeouts", "partial"])
def test_without_a_request_timeout_the_route_default_applies(timeouts):
    """request_timeout has a model default (120): only one the entry sets may replace the route's own."""
    cfg = LLMModelConfig(provider="openai_responses", model="m", httpx_timeouts=timeouts)
    assert _timeout_config(cfg, default_read=600.0, default_write=30.0).read == 600.0


# ------------------------------------------------------------ stream restart

_STOP = _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
_THINK = _chunk({"choices": [{"index": 0, "delta": {"reasoning": "hm"}, "finish_reason": None}]})


def _types(chunks):
    return [chunk["type"] for chunk in chunks]


async def _stream_of(client):
    return [chunk async for chunk in client.chat_tools_streaming(MESSAGES, [])]


_TOOL = _chunk({"choices": [{"index": 0, "delta": {"tool_calls": [
    {"index": 0, "id": "c1", "function": {"name": "read", "arguments": "{"}}]}, "finish_reason": None}]})


@pytest.mark.parametrize("first", [_content("Hal"), _THINK, _TOOL], ids=["content", "thinking", "tool call"])
async def test_a_retry_after_deltas_tells_the_caller_to_start_over_once(first):
    """Attempt one sends a delta and drops, attempt two fails before any, attempt three answers."""
    async with Upstream(sse(first, then="drop"), status(500),
                        sse(_content("ganz"), _STOP, _chunk("[DONE]"))) as upstream:
        client = chat_client(upstream.url, max_retries=2)
        chunks = await _stream_of(client)
    kinds = _types(chunks)
    assert kinds.count("stream_restart") == 1
    assert kinds[kinds.index("stream_restart") + 1:] == ["content_delta", "final"]
    assert chunks[-1]["assistant"]["content"] == "ganz"


async def test_a_retry_before_any_delta_sends_no_restart():
    async with Upstream(status(500), sse(_content("ganz"), _STOP, _chunk("[DONE]"))) as upstream:
        client = chat_client(upstream.url, max_retries=1)
        chunks = await _stream_of(client)
    assert _types(chunks) == ["content_delta", "final"]


def _completed(text: str) -> bytes:
    return _chunk({"type": "response.completed", "response": {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}})


async def test_a_responses_retry_after_deltas_tells_the_caller_to_start_over_once():
    delta = _chunk({"type": "response.output_text.delta", "delta": "Hal"})
    good = sse(_chunk({"type": "response.output_text.delta", "delta": "ganz"}), _completed("ganz"),
               _chunk("[DONE]"))
    async with Upstream(sse(delta, then="drop"), status(500), good) as upstream:
        client = _responses_client(upstream.url, streaming=True)
        client.max_retries = 2
        chunks = await _stream_of(client)
    kinds = _types(chunks)
    assert kinds.count("stream_restart") == 1
    assert kinds[kinds.index("stream_restart") + 1:] == ["content_delta", "final"]


async def test_a_responses_retry_before_any_delta_sends_no_restart():
    good = sse(_chunk({"type": "response.output_text.delta", "delta": "ganz"}), _completed("ganz"),
               _chunk("[DONE]"))
    async with Upstream(status(500), good) as upstream:
        client = _responses_client(upstream.url, streaming=True)
        chunks = await _stream_of(client)
    assert _types(chunks) == ["content_delta", "final"]


# --------------------------------------------- Responses: one end per request

async def test_a_responses_cancel_during_the_retry_wait_is_reported():
    async with Upstream(status(500)) as upstream:
        client = _responses_client(upstream.url, streaming=False)
        client.retry_backoff = 30.0
        token = CancellationToken("req")
        hooks = Hooks(client, on_post=lambda info: token.cancel())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(client.chat_tools(MESSAGES, [], cancellation_token=token), 10)
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", None]
    assert hooks.post[1]["error"] == "Request cancelled during retry wait"


async def test_a_responses_failure_nobody_expected_is_reported_once():
    def broken(body_text):
        raise RuntimeError("detector broke")

    async with Upstream(status(400, "nope")) as upstream:
        client = _responses_client(upstream.url, streaming=False)
        client._is_reasoning_artifact_rejection = broken
        hooks = Hooks(client)
        with pytest.raises(RuntimeError):
            await client.chat_tools(MESSAGES, [])
    assert [info["error"] for info in hooks.post] == ["detector broke"]


async def test_a_responses_request_never_sent_reports_nothing():
    async with Upstream(silent()) as upstream:
        client = _responses_client(upstream.url, streaming=False)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.chat_tools(MESSAGES, [], cancellation_token=token)
    assert hooks.pre == [] and hooks.post == []


@pytest.mark.parametrize("script", [
    sse(_chunk({"type": "response.output_text.delta", "delta": "da"}), _completed("da"), _chunk("[DONE]")),
    sse(_chunk({"type": "response.output_text.delta", "delta": "da"})),
], ids=["answer", "no terminal event"])
async def test_a_responses_stream_closed_after_its_answer_reports_nothing_more(script):
    async with Upstream(script) as upstream:
        client = _responses_client(upstream.url, streaming=True)
        client.max_retries = 0
        hooks = Hooks(client)
        gen = client.chat_tools_streaming(MESSAGES, [])
        while (await gen.__anext__())["type"] != "final":
            pass
        await gen.aclose()
    assert len(hooks.post) == 1 and hooks.post[0].get("error") is None


@pytest.mark.parametrize("script, raised, error", [
    (status(400, "nope"), httpx.HTTPStatusError, "HTTP 400: nope"),
    (status(429, "slow down"), LLMRateLimitError, "HTTP 429: slow down"),
], ids=["4xx", "429"])
async def test_a_responses_refusal_is_reported_once(script, raised, error):
    async with Upstream(script) as upstream:
        client = _responses_client(upstream.url, streaming=False)
        hooks = Hooks(client)
        with pytest.raises(raised):
            await client.chat_tools(MESSAGES, [])
    assert [info["error"] for info in hooks.post] == [error]
