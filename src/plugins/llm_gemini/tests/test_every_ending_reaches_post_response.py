"""Every way a Gemini request ends reaches post_llm_response exactly once.

The message debugger, the otel chat span and the cost readers only see what
the client reports there. Both clients (REST over httpx, and the google-genai
SDK) are driven against a scripted HTTP server on 127.0.0.1 -- the real
transport, no mocks of the client. Also: the error types the agent server's
profile fallback needs, stream_restart, closing, timeouts and usage.
"""
import asyncio
import inspect
import json

import httpx
import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.core.cancellation import CancellationToken
from agent_system.llm.models import ChatMessage, LLMConnectionError, LLMRateLimitError, LLMServerError
from plugins.llm_gemini.gemini_client import GeminiClient
from plugins.llm_gemini.gemini_sdk_client import GeminiSDKClient
from plugins.llm_gemini.gemini_utils import extract_usage_from_metadata
from plugins.llm_gemini.provider import build_gemini, build_gemini_sdk, make_batch_backend

# google-genai subclasses aiohttp.ClientSession, which aiohttp warns about;
# the suite turns warnings into errors. The SDK's production transport is kept.
pytestmark = pytest.mark.filterwarnings("ignore:Inheritance class AiohttpClientSession")

MESSAGES = [ChatMessage(role="user", content="hi")]
USAGE_META = {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10}
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


def _chunk(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\r\n\r\n".encode()


def _text(text: str, **extra) -> bytes:
    return _chunk({"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}], **extra})


def _stop() -> bytes:
    """The last chunk: a finish reason and the usage, no content."""
    return _chunk({"candidates": [{"finishReason": "STOP"}], "usageMetadata": USAGE_META})


def _answer(text: str) -> str:
    return json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                       "finishReason": "STOP"}], "usageMetadata": USAGE_META})


def _refusal(code: int, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message, "status": "X"}})


def status(code: int, body: str = "{}"):
    async def script(writer):
        raw = body.encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
    return script


def sse(*events: bytes, then: str = "end", gap: float = 0.0):
    """A chunked event stream, *gap* seconds between events; then end it, hang, or drop."""
    async def script(writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        for event in events:
            await asyncio.sleep(gap)
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
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        for task in self._tasks:
            task.cancel()
        self._server.close()

    async def _serve(self, reader, writer):
        self._tasks.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        script = self.scripts[min(self.hits, len(self.scripts) - 1)]
        self.hits += 1
        try:
            await script(writer)
        finally:
            writer.close()


def rest_client(port: int, *, streaming: bool = True, read: float = 5.0, max_retries: int = 0):
    return GeminiClient(model="m", api_key="k", base_url=f"http://127.0.0.1:{port}/v1beta",
                        httpx_timeouts={"connect": 2.0, "read": read, "write": 2.0, "pool": 2.0},
                        max_retries=max_retries, capabilities={"streaming": streaming})


def sdk_client(port: int, *, streaming: bool = True, read: float = 5.0, max_retries: int = 0):
    from google import genai
    from google.genai import types

    client = GeminiSDKClient(model="m", api_key="k", request_timeout=read, max_retries=max_retries,
                             rate_limit_max_retries=0, capabilities={"streaming": streaming})
    client._client = genai.Client(api_key="k", http_options=types.HttpOptions(
        base_url=f"http://127.0.0.1:{port}/"))
    return client


CLIENTS = pytest.mark.parametrize("make", [rest_client, sdk_client], ids=["rest", "sdk"])


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


async def _ask(client, streaming: bool, **kw):
    if not streaming:
        return await client.chat_tools(MESSAGES, [], **kw)
    final = None
    async for chunk in client.chat_tools_streaming(MESSAGES, [], **kw):
        if chunk["type"] == "final":
            final = chunk
    return final


async def _drain(gen, on_chunk=None):
    async for chunk in gen:
        if on_chunk:
            on_chunk(chunk)


# ------------------------------------------------------------- cancellation

@CLIENTS
async def test_a_stream_cancelled_by_its_token_is_reported_once(make):
    async with Upstream(sse(_text("Hal", usageMetadata=USAGE_META), then="hang")) as upstream:
        client = make(upstream.port)
        hooks = Hooks(client)
        token = CancellationToken("req")
        gen = client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_drain(gen, lambda chunk: token.cancel()), 10)
    assert len(hooks.pre) == 1
    assert len(hooks.post) == 1, hooks.post
    assert "cancelled" in hooks.post[0]["error"] and hooks.post[0]["is_streaming"] is True
    assert hooks.post[0]["usage"] == USAGE


@CLIENTS
@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_request_whose_task_is_cancelled_is_reported_and_stays_cancelled(make, streaming):
    script = sse(_text("Hal"), then="hang") if streaming else silent()
    async with Upstream(script) as upstream:
        client = make(upstream.port, streaming=streaming)
        hooks = Hooks(client)
        task = asyncio.create_task(_ask(client, streaming))
        while upstream.hits == 0:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled()
    assert [(info["error"], info["is_streaming"]) for info in hooks.post] == [("cancelled", streaming)]


@CLIENTS
@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_token_cancelled_before_the_request_left_is_reported(make, streaming):
    async with Upstream(silent()) as upstream:
        client = make(upstream.port, streaming=streaming)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _ask(client, streaming, cancellation_token=token)
        assert upstream.hits == 0
    assert len(hooks.pre) == 1
    assert [info["error"] for info in hooks.post] == ["Request cancelled before attempt"]


@CLIENTS
@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_cancel_during_the_retry_wait_is_reported(make, streaming):
    """The wait sits in an except branch, where the attempt's own handlers no longer catch."""
    async with Upstream(status(503, _refusal(503, "busy"))) as upstream:
        client = make(upstream.port, streaming=streaming, max_retries=1)
        token = CancellationToken("req")
        hooks = Hooks(client, on_post=lambda info: token.cancel())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_ask(client, streaming, cancellation_token=token), 10)
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", None], hooks.post
    assert hooks.post[1]["error"] == "Request cancelled during retry wait"


@CLIENTS
@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_cancel_while_the_answer_is_reported_does_not_report_it_twice(make, streaming):
    def cancel_on_answer(info):
        if not info.get("error"):
            raise asyncio.CancelledError()

    script = sse(_text("da"), _stop()) if streaming else status(200, _answer("da"))
    async with Upstream(script) as upstream:
        client = make(upstream.port, streaming=streaming)
        hooks = Hooks(client, on_post=cancel_on_answer)
        with pytest.raises(asyncio.CancelledError):
            await _ask(client, streaming)
    assert len(hooks.post) == 1 and hooks.post[0].get("error") is None


# -------------------------------------------------------- the caller leaves

@CLIENTS
async def test_a_stream_the_caller_abandons_is_reported_and_closed_at_once(make):
    """aclosing: the request generator closes with the caller's, not at garbage collection."""
    async with Upstream(sse(_text("Hal"), then="hang")) as upstream:
        client = make(upstream.port)
        hooks = Hooks(client)
        inner: list = []
        stream = client._stream
        client._stream = lambda *args: inner.append(stream(*args)) or inner[-1]
        gen = client.chat_tools_streaming(MESSAGES, [])
        assert (await gen.__anext__())["type"] == "content_delta"
        await gen.aclose()
        assert inspect.getasyncgenstate(inner[0]) == inspect.AGEN_CLOSED
    assert [info["error"] for info in hooks.post] == ["stream abandoned by the caller"]


async def test_an_abandoned_sdk_stream_stops_reading_at_once():
    async with Upstream(sse(_text("Hal"), then="hang")) as upstream:
        client = sdk_client(upstream.port)
        readers: list = []
        read = client._cancellable_stream
        client._cancellable_stream = lambda *args: readers.append(read(*args)) or readers[-1]
        gen = client.chat_tools_streaming(MESSAGES, [])
        await gen.__anext__()
        await gen.aclose()
        assert inspect.getasyncgenstate(readers[0]) == inspect.AGEN_CLOSED


# -------------------------------------------------- refused after the retries

@CLIENTS
@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
@pytest.mark.parametrize("script, raised, code", [
    (status(400, _refusal(400, "bad")), httpx.HTTPStatusError, 400),
    (status(404, _refusal(404, "no such model")), httpx.HTTPStatusError, 404),
    (status(503, _refusal(503, "busy")), LLMServerError, 503),
    (status(429, _refusal(429, "slow down")), LLMRateLimitError, None),
    (silent(), LLMConnectionError, None),
], ids=["400", "404", "5xx", "429", "timeout"])
async def test_a_refusal_raises_what_the_fallback_catches_and_is_reported_once(
        make, streaming, script, raised, code):
    """The agent server falls back to the next profile (and blocks a dead key or model) only on
    these types; a plain Exception ended the run without trying the chain."""
    async with Upstream(script) as upstream:
        client = make(upstream.port, streaming=streaming, read=0.5)
        hooks = Hooks(client)
        with pytest.raises(raised) as refused:
            await asyncio.wait_for(_ask(client, streaming), 10)
    if code is not None:
        status_code = (refused.value.response.status_code if raised is httpx.HTTPStatusError
                       else refused.value.status_code)
        assert status_code == code
    assert len(hooks.post) == 1, hooks.post
    assert hooks.post[0]["error"] and hooks.post[0]["is_streaming"] is streaming


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_the_sdk_waits_no_longer_than_its_read_timeout_under_a_token(streaming):
    async with Upstream(silent()) as upstream:
        client = sdk_client(upstream.port, streaming=streaming, read=0.5)
        with pytest.raises(LLMConnectionError):
            await asyncio.wait_for(_ask(client, streaming, cancellation_token=CancellationToken("r")), 10)


@CLIENTS
async def test_a_stream_that_stalls_after_its_first_piece_is_no_answer(make):
    async with Upstream(sse(_text("Hal"), then="hang")) as upstream:
        client = make(upstream.port, read=0.5)
        with pytest.raises(LLMConnectionError):
            await asyncio.wait_for(_ask(client, True), 10)


def _thought(text: str) -> bytes:
    return _chunk({"candidates": [{"content": {"role": "model", "parts": [{"text": text, "thought": True}]}}]})


@CLIENTS
async def test_a_thinking_loop_is_no_answer(make):
    """Both clients give up on the same thought repeated; the SDK client's loop used to end the
    run untyped, where the REST client's fell back to the next profile."""
    loop = [_thought("I will now check the file again.")] * 6
    async with Upstream(sse(*loop, then="hang")) as upstream:
        client = make(upstream.port)
        with pytest.raises(LLMConnectionError):
            await asyncio.wait_for(_ask(client, True), 10)


# ------------------------------------------------------------- the answers

USAGE_ONLY = _chunk({"usageMetadata": USAGE_META})
FINISH_ONLY = _chunk({"candidates": [{"finishReason": "STOP"}]})


@CLIENTS
@pytest.mark.parametrize("streaming, script", [
    (True, sse(_text("da"), _stop())),
    (True, sse(_text("da"), FINISH_ONLY, USAGE_ONLY)),
    (False, status(200, _answer("da"))),
], ids=["stream", "stream, usage alone last", "no stream"])
async def test_an_answer_is_reported_once_with_its_usage(make, streaming, script):
    """The usage often comes in a chunk without content; skipping such chunks lost it."""
    async with Upstream(script) as upstream:
        client = make(upstream.port, streaming=streaming)
        hooks = Hooks(client)
        result = await _ask(client, streaming)
    assert result["assistant"]["content"] == "da"
    assert [(info.get("error"), info["usage"]) for info in hooks.post] == [(None, USAGE)]


@CLIENTS
async def test_an_answer_without_candidates_is_reported(make):
    blocked = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": USAGE_META})
    async with Upstream(status(200, blocked)) as upstream:
        client = make(upstream.port, streaming=False)
        hooks = Hooks(client)
        result = await client.chat_tools(MESSAGES, [])
    assert result["assistant"]["content"] == ""
    assert [(info.get("error"), info["usage"]) for info in hooks.post] == [(None, USAGE)]


@CLIENTS
async def test_chat_reports_like_chat_tools(make):
    async with Upstream(status(200, _answer("da"))) as upstream:
        client = make(upstream.port, streaming=False)
        hooks = Hooks(client)
        assert await client.chat(MESSAGES) == "da"
    assert len(hooks.pre) == 1 and len(hooks.post) == 1


# ------------------------------------------------------------ stream restart

@CLIENTS
async def test_a_retry_after_deltas_tells_the_caller_to_start_over_once(make):
    async with Upstream(sse(_text("Hal"), then="drop"), sse(_text("ganz"), _stop())) as upstream:
        client = make(upstream.port, max_retries=1)
        chunks = [chunk async for chunk in client.chat_tools_streaming(MESSAGES, [])]
    kinds = [chunk["type"] for chunk in chunks]
    assert kinds == ["content_delta", "stream_restart", "content_delta", "final"], kinds
    assert chunks[-1]["assistant"]["content"] == "ganz"


@CLIENTS
async def test_a_retry_before_any_delta_sends_no_restart(make):
    async with Upstream(status(503, _refusal(503, "busy")), sse(_text("ganz"), _stop())) as upstream:
        client = make(upstream.port, max_retries=1)
        chunks = [chunk async for chunk in client.chat_tools_streaming(MESSAGES, [])]
    assert [chunk["type"] for chunk in chunks] == ["content_delta", "final"]


# ------------------------------------------------------------------ timeouts

def _cfg(provider: str, **kw) -> LLMModelConfig:
    return LLMModelConfig(provider=provider, model="m", api_key="k", **kw)


@pytest.mark.parametrize("entry, read, connect", [
    ({"request_timeout": 240, "httpx_timeouts": {"connect": 3.0}}, 240.0, 3.0),
    ({"request_timeout": 240, "httpx_timeouts": {"read": 60.0}}, 60.0, 10.0),
    ({"httpx_timeouts": {"connect": 3.0}}, 180.0, 3.0),
    ({}, 180.0, 10.0),
], ids=["request_timeout", "httpx read wins", "neither", "nothing"])
def test_the_read_timeout_is_the_one_the_entry_sets(entry, read, connect):
    """request_timeout has a model default (120): only one the entry sets replaces the 180 s.
    A key httpx_timeouts leaves out falls back instead of taking the pydantic default."""
    timeouts = build_gemini(_cfg("gemini", **entry)).timeouts
    assert (timeouts.read, timeouts.connect) == (read, connect)
    assert build_gemini_sdk(_cfg("gemini_sdk", **entry)).read_timeout == read


def test_the_factory_s_read_timeout_reaches_both_clients():
    """The core stamps llm_system.httpx_timeouts with read = the entry's request_timeout."""
    stamped = {"connect": 10.0, "read": 240.0, "write": 10.0, "pool": 5.0}
    cfg = _cfg("gemini", request_timeout=240, httpx_timeouts=stamped)
    assert build_gemini(cfg).timeouts.read == 240.0
    assert build_gemini_sdk(cfg).read_timeout == 240.0


# --------------------------------------------------------------------- usage

def test_thinking_tokens_count_as_output():
    """Gemini counts thoughts apart from the answer and bills them as output."""
    from google.genai import types

    rest = {"promptTokenCount": 7, "candidatesTokenCount": 3, "thoughtsTokenCount": 40,
            "totalTokenCount": 50}
    sdk = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=7, candidates_token_count=3, thoughts_token_count=40, total_token_count=50)
    for metadata in (rest, sdk):
        usage = extract_usage_from_metadata(metadata)
        assert usage["completion_tokens"] == 43
        assert usage["completion_tokens_details"] == {"reasoning_tokens": 40}
        assert usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"]


# ------------------------------------------------ refusals are not retried

@CLIENTS
@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
@pytest.mark.parametrize("code", [403, 404])
async def test_a_refusal_goes_to_the_fallback_without_retries(make, streaming, code):
    """Retried, a dead key or a missing model cost three more requests and 7 s first."""
    async with Upstream(status(code, _refusal(code, "no"))) as upstream:
        client = make(upstream.port, streaming=streaming, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError):
            await asyncio.wait_for(_ask(client, streaming), 10)
    assert upstream.hits == 1


@CLIENTS
async def test_a_sporadic_schema_400_is_still_retried(make):
    too_many = status(400, _refusal(400, "too many states for serving"))
    async with Upstream(too_many, status(200, _answer("da"))) as upstream:
        client = make(upstream.port, streaming=False, max_retries=1)
        result = await asyncio.wait_for(client.chat_tools(MESSAGES, []), 10)
    assert result["assistant"]["content"] == "da" and upstream.hits == 2


# ---------------------------------------------- stall clock, finish, thinking

async def test_the_sdk_stall_clock_starts_over_with_every_piece():
    """Four pieces 0.7 s apart take 2.8 s -- longer than read=1.0, but no gap is. The gap
    exceeds the client's 0.5 s cancel check, so the stall clock is looked at in between."""
    pieces = [_text(word) for word in ("a", "b", "c")] + [_stop()]
    async with Upstream(sse(*pieces, gap=0.7)) as upstream:
        client = sdk_client(upstream.port, read=1.0)
        final = await asyncio.wait_for(_ask(client, True), 10)
    assert final["assistant"]["content"] == "abc"


@CLIENTS
async def test_a_streamed_answer_reports_its_finish_reason(make):
    async with Upstream(sse(_text("da"), _stop())) as upstream:
        client = make(upstream.port)
        hooks = Hooks(client)
        await _ask(client, True)
    assert hooks.post[0]["finish_reason"] in ("STOP", "stop")


@CLIENTS
async def test_thinking_streams_as_thinking_and_the_answer_as_content(make):
    """The agent server shows thinking_delta as thinking and watches it for loops."""
    async with Upstream(sse(_thought("hm, let me see"), _text("da"), _stop())) as upstream:
        client = make(upstream.port)
        chunks = [chunk async for chunk in client.chat_tools_streaming(MESSAGES, [])]
    assert [(c["type"], c.get("delta")) for c in chunks[:-1]] == [
        ("thinking_delta", "hm, let me see"), ("content_delta", "da")]
    assert chunks[1]["accumulated"] == "da"
    assert chunks[-1]["assistant"]["reasoning_content"] == "hm, let me see"


# ---------------------------------------------------------------------- keys

@pytest.mark.parametrize("key", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_key_is_no_key(monkeypatch, key):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", " ")
    cfg = LLMModelConfig(provider="gemini", model="m", api_key=key)
    with pytest.raises(ValueError):
        build_gemini(cfg)
    assert make_batch_backend(cfg) is None


def test_a_key_is_sent_without_its_spaces(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = LLMModelConfig(provider="gemini", model="m", api_key=" k \n")
    assert build_gemini(cfg).api_key == "k"
