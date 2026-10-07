"""Every way a request ends reaches post_llm_response exactly once.

The message debugger, the otel chat span and the cost readers only see what
the client reports there. A request cancelled mid-stream, abandoned by its
caller or refused after its retries used to end with a pre_llm_request and no
response at all. Driven against a scripted HTTP server on 127.0.0.1 (a random
port, never a real Ollama) -- the real httpx path, no mocks of the client.
"""
import asyncio
import json

import httpx
import pytest

from agent_system.core.cancellation import CancellationToken
from agent_system.llm.models import (
    ChatMessage, LLMConnectionError, LLMRateLimitError, LLMServerError, abandon_report,
)
from plugins.llm_ollama.ollama_client import OllamaNativeAsyncClient

MESSAGES = [ChatMessage(role="user", content="hi")]
DONE = {"done": True, "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 3}
USAGE = {"cost": 0.0, "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}  # Ollama bills nothing


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _content(text: str) -> bytes:
    return _line({"message": {"role": "assistant", "content": text}, "done": False})


def status(code: int, body: str = "{}"):
    async def script(writer):
        raw = body.encode()
        writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
        await writer.drain()
    return script


def ndjson(*lines: bytes, then: str = "end"):
    """A chunked NDJSON stream; after the lines: end it, hang, or drop the connection."""
    async def script(writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        for line in lines:
            writer.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
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
        self.client_closed = asyncio.Event()  # the client hung up on a connection
        self._tasks: set = set()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
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
        watch = asyncio.create_task(self._watch_close(reader))
        self._tasks.add(watch)
        script = self.scripts[min(self.hits, len(self.scripts) - 1)]
        self.hits += 1
        try:
            await script(writer)
        finally:
            writer.close()

    async def _watch_close(self, reader):
        await reader.read()  # EOF: the client closed its side
        self.client_closed.set()


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


def make_client(url: str, *, timeout: float = 5.0, max_retries: int = 0) -> OllamaNativeAsyncClient:
    client = OllamaNativeAsyncClient(model="m", base_url=url, timeout=timeout)
    client.max_retries = max_retries
    client.retry_backoff = 0
    return client


async def _drain(gen, on_chunk=None):
    async for chunk in gen:
        if on_chunk:
            on_chunk(chunk)


async def _ask(client, streaming: bool, **kw):
    if streaming:
        return [chunk async for chunk in client.chat_tools_streaming(MESSAGES, [], **kw)]
    return await client.chat_tools(MESSAGES, [], **kw)


# ------------------------------------------------------------------ answers

@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_an_answer_is_reported_once_with_its_finish_reason(streaming):
    script = (ndjson(_content("da"), _line(DONE)) if streaming
              else status(200, json.dumps({"message": {"content": "da"}, **DONE})))
    async with Upstream(script) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        answer = await _ask(client, streaming)
    result = answer[-1] if streaming else answer
    assert result["finish_reason"] == "stop" and result["usage"] == USAGE
    assert [(info.get("error"), info["finish_reason"], info["usage"]) for info in hooks.post] == [
        (None, "stop", USAGE)]


# ------------------------------------------------------------- cancellation

async def test_a_stream_cancelled_by_its_token_is_reported_once():
    async with Upstream(ndjson(_content("Hal"), then="hang")) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        gen = client.chat_tools_streaming(MESSAGES, [], cancellation_token=token)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_drain(gen, lambda chunk: token.cancel()), 10)
    assert len(hooks.pre) == 1
    assert [(info["error"], info["is_streaming"]) for info in hooks.post] == [
        ("Request cancelled by user", True)]


async def test_a_stream_whose_task_is_cancelled_is_reported_and_stays_cancelled():
    async with Upstream(ndjson(_content("Hal"), then="hang")) as upstream:
        client = make_client(upstream.url)
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


@pytest.mark.parametrize("with_token", [False, True], ids=["task cancel", "token cancel"])
async def test_a_non_streaming_request_cancelled_while_out_is_reported(with_token):
    async with Upstream(silent()) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req") if with_token else None
        task = asyncio.create_task(client.chat_tools(MESSAGES, [], cancellation_token=token))
        while upstream.hits == 0:
            await asyncio.sleep(0.02)
        if token:
            token.cancel()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    assert len(hooks.post) == 1 and hooks.post[0]["is_streaming"] is False
    assert "cancel" in hooks.post[0]["error"]


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_token_cancelled_before_the_request_left_is_reported(streaming):
    async with Upstream(silent()) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        token = CancellationToken("req")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _ask(client, streaming, cancellation_token=token)
        assert upstream.hits == 0
    assert len(hooks.pre) == 1
    assert [info["error"] for info in hooks.post] == ["Request cancelled by user"]


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_cancel_while_the_answer_is_reported_does_not_report_it_twice(streaming):
    """The answer's own report is where the cancel lands: it must not turn into a second, failed one."""
    script = (ndjson(_content("da"), _line(DONE)) if streaming
              else status(200, json.dumps({"message": {"content": "da"}, **DONE})))

    def cancel_on_answer(info):
        if not info.get("error"):
            raise asyncio.CancelledError()

    async with Upstream(script) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client, on_post=cancel_on_answer)
        with pytest.raises(asyncio.CancelledError):
            await _ask(client, streaming)
    assert len(hooks.post) == 1
    assert hooks.post[0].get("error") is None and hooks.post[0]["usage"] == USAGE


async def test_a_cancel_during_the_retry_wait_is_reported():
    """The wait sits in an except branch, where the attempt's own handlers no longer catch."""
    async with Upstream(ndjson(_content("Hal"), then="drop")) as upstream:
        client = make_client(upstream.url, max_retries=1)
        client.retry_backoff = 30.0
        token = CancellationToken("req")
        hooks = Hooks(client, on_post=lambda info: token.cancel())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_ask(client, True, cancellation_token=token), 10)
    assert len(hooks.post) == 2, hooks.post
    assert hooks.post[0]["finish_reason"] == "retry"
    assert hooks.post[1]["error"] == "Request cancelled during retry wait"


async def test_a_stream_the_caller_abandons_is_closed_before_it_is_reported():
    """Without aclosing the inner request generator stays open until the outer
    one is gone -- the report would come while the connection still runs."""
    reports: list = []

    async with Upstream(ndjson(_content("Hal"), then="hang")) as upstream:
        client = make_client(upstream.url)

        async def post(info):
            try:
                await asyncio.wait_for(upstream.client_closed.wait(), 1)
                reports.append((info["error"], "closed"))
            except asyncio.TimeoutError:
                reports.append((info["error"], "still open"))

        client.set_llm_hooks(on_post_response=post)
        gen = client.chat_tools_streaming(MESSAGES, [])
        assert (await gen.__anext__())["type"] == "content_delta"
        await gen.aclose()
    assert reports == [("stream abandoned by the caller", "closed")]


async def test_an_abandon_the_agent_explains_leaves_one_row_with_its_reason():
    """The agent server sets abandon_report before it closes a looping stream;
    the client's own end report carries that reason (LLMClient._notify_post_response)."""
    async with Upstream(ndjson(_content("Hal"), then="hang")) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        gen = client.chat_tools_streaming(MESSAGES, [])
        await gen.__anext__()
        pending = {"reported": False, "fields": {"finish_reason": "reasoning_loop_aborted",
                                                 "error": "reasoning loop aborted: x"}}
        abandon_report.set(pending)
        try:
            await gen.aclose()
        finally:
            abandon_report.set(None)
    assert [info["finish_reason"] for info in hooks.post] == ["reasoning_loop_aborted"]
    assert pending["reported"] is True


async def test_a_stream_closed_after_its_answer_reports_nothing_more():
    async with Upstream(ndjson(_content("da"), _line(DONE))) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        gen = client.chat_tools_streaming(MESSAGES, [])
        while (await gen.__anext__())["type"] != "final":
            pass
        await gen.aclose()
    assert len(hooks.post) == 1 and hooks.post[0].get("error") is None


# -------------------------------------------------- refused after the retries

@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
@pytest.mark.parametrize("script, raised, error", [
    (status(404, '{"error": "model \'m\' not found"}'), httpx.HTTPStatusError, "Ollama 404: model 'm' not found"),
    (status(500, '{"error": "runner crashed"}'), LLMServerError, "Ollama 500: runner crashed"),
    (status(429, '{"error": "slow down"}'), LLMRateLimitError, "Ollama 429: slow down"),
    (silent(), LLMConnectionError, "Network/protocol error: "),
], ids=["4xx", "5xx", "429", "timeout"])
async def test_a_refusal_is_raised_typed_and_reported_once(streaming, script, raised, error):
    """The agent server falls back (and blocks a dead model) on these types; as an
    error answer the streaming path was asked once more on the same model first."""
    async with Upstream(script) as upstream:
        client = make_client(upstream.url, timeout=0.5)
        hooks = Hooks(client)
        with pytest.raises(raised):
            await _ask(client, streaming)
    assert len(hooks.post) == 1, hooks.post
    assert hooks.post[0]["error"].startswith(error), hooks.post[0]["error"]
    assert hooks.post[0]["is_streaming"] is streaming


async def test_a_stream_that_keeps_dropping_is_a_connection_error_after_its_retries():
    async with Upstream(ndjson(_content("Hal"), then="drop")) as upstream:
        client = make_client(upstream.url, max_retries=2)
        hooks = Hooks(client)
        with pytest.raises(LLMConnectionError, match="Stream failed after 3 attempts"):
            await _ask(client, True)
        assert upstream.hits == 3
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", "retry", None]
    assert hooks.post[-1]["error"].startswith("Stream failed after 3 attempts")


async def test_a_5xx_is_retried_before_it_is_raised():
    async with Upstream(status(503, '{"error": "server busy"}')) as upstream:
        client = make_client(upstream.url, max_retries=2)
        hooks = Hooks(client)
        with pytest.raises(LLMServerError) as refused:
            await _ask(client, True)
        assert upstream.hits == 3
    assert refused.value.status_code == 503
    assert [info.get("finish_reason") for info in hooks.post] == ["retry", "retry", None]


_PORTAL = "<html><body>Please log in to the guest network</body></html>"


@pytest.mark.parametrize("streaming", [True, False], ids=["stream", "no stream"])
async def test_a_body_that_is_no_json_is_a_connection_error(streaming):
    """A proxy, captive portal or web UI answering 200 with HTML is not Ollama: fall back.
    Blocking, the raw JSONDecodeError reached no fallback; streaming, every line was
    skipped and the stream ended as a success with no content."""
    script = ndjson(_PORTAL.encode() + b"\n") if streaming else status(200, _PORTAL)
    async with Upstream(script) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        with pytest.raises(LLMConnectionError) as refused:
            await _ask(client, streaming)
    assert str(refused.value) == f"Ollama answered 200 with a body that is no JSON: {_PORTAL}"
    assert [info["error"] for info in hooks.post] == [str(refused.value)]


async def test_a_stream_without_its_done_line_is_no_answer():
    async with Upstream(ndjson(_content("Hal"))) as upstream:
        client = make_client(upstream.url)
        hooks = Hooks(client)
        with pytest.raises(LLMConnectionError, match="without its done line"):
            await _ask(client, True)
    assert len(hooks.post) == 1 and "done line" in hooks.post[0]["error"]


def test_two_hosts_are_two_endpoints_for_the_model_health_blocks():
    """model_health keys a block by base_url: without it a 404 on one Ollama host
    blocked the same model name on every host."""
    from agent_system.llm.model_health import model_key

    first = model_key(OllamaNativeAsyncClient(model="m", base_url="http://10.0.0.1:11434/"))
    second = model_key(OllamaNativeAsyncClient(model="m", base_url="http://10.0.0.2:11434"))
    assert first[0] == "http://10.0.0.1:11434" and second[0] == "http://10.0.0.2:11434"


# ------------------------------------------------------------ stream restart

def _types(chunks):
    return [chunk["type"] for chunk in chunks]


_THINK = _line({"message": {"role": "assistant", "thinking": "hm"}, "done": False})
_TOOL = _line({"message": {"role": "assistant", "tool_calls": [
    {"function": {"name": "read", "arguments": {}}}]}, "done": False})


@pytest.mark.parametrize("first", [_content("Hal"), _THINK, _TOOL], ids=["content", "thinking", "tool call"])
async def test_a_retry_after_deltas_tells_the_caller_to_start_over_once(first):
    """Attempt one sends a delta and drops, attempt two fails before any, attempt three answers."""
    async with Upstream(ndjson(first, then="drop"), status(500),
                        ndjson(_content("ganz"), _line(DONE))) as upstream:
        client = make_client(upstream.url, max_retries=2)
        chunks = await _ask(client, True)
    kinds = _types(chunks)
    assert kinds.count("stream_restart") == 1
    assert kinds[kinds.index("stream_restart") + 1:] == ["content_delta", "final"]
    assert chunks[-1]["assistant"]["content"] == "ganz"


async def test_a_retry_before_any_delta_sends_no_restart():
    async with Upstream(status(500), ndjson(_content("ganz"), _line(DONE))) as upstream:
        client = make_client(upstream.url, max_retries=1)
        chunks = await _ask(client, True)
    assert _types(chunks) == ["content_delta", "final"]
