"""BatchLLMClient reports every request to pre/post_llm_response exactly once.

Fakes only: the queue manager is a stub, the underlying client a local
LLMClient that reports its own (sync) request the way real clients do.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_system.llm.batch.batch_client import BatchLLMClient
from agent_system.llm.models import ChatMessage, LLMClient, LLMConnectionError

MESSAGES = [ChatMessage(role="user", content="hi")]
USAGE = {"prompt_tokens": 7, "completion_tokens": 3}
SYNC_LATENCY_MS = 42.0


class FakeSyncClient(LLMClient):
    """Stands for the real client behind the wrapper: reports its own request."""

    model = "m"

    async def chat(self, messages, cancellation_token=None):
        await self._notify_pre_request({"provider": "sync"})
        await self._notify_post_response({"provider": "sync", "usage": {"prompt_tokens": 1},
                                          "duration_ms": SYNC_LATENCY_MS})
        return "sync answer"

    async def chat_tools(self, messages, tools, cancellation_token=None):
        await self.chat(messages)
        return {"assistant": {"role": "assistant", "content": "sync answer"}}

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None):
        await self.chat(messages)
        yield {"type": "final", "assistant": {"role": "assistant", "content": "sync answer"}}


class FakeQueue:
    def __init__(self, outcome):
        self.outcome = outcome

    async def submit_request(self, **kwargs):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome == "hang":
            await asyncio.Event().wait()
        return self.outcome


def make_client(outcome, fallback=True):
    client = BatchLLMClient(
        underlying_client=FakeSyncClient(),
        queue_manager=FakeQueue(outcome),
        batch_provider_config=SimpleNamespace(fallback_to_sync=fallback),
        model_name="m",
        batch_provider="openai",
    )
    events = []

    async def pre(info):
        events.append(("pre", info))

    async def post(info):
        events.append(("post", info))

    client.set_llm_hooks(on_pre_request=pre, on_post_response=post)
    return client, events


def ends(events, provider):
    return [info for kind, info in events if kind == "post" and info.get("provider") == provider]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat", "chat_tools"])
async def test_batch_answer_reports_once_with_its_usage(method):
    client, events = make_client({"choices": [{"message": {"content": "ok"}}], "usage": USAGE})
    call = client.chat(MESSAGES) if method == "chat" else client.chat_tools(MESSAGES, [])
    await call
    assert [kind for kind, _ in events] == ["pre", "post"]
    (end,) = ends(events, "batch_openai")
    assert end["usage"] == USAGE and not end.get("error")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat", "chat_tools"])
async def test_sync_fallback_reports_itself_after_the_failed_batch(method):
    client, events = make_client(RuntimeError("batch api down"))
    call = client.chat(MESSAGES) if method == "chat" else client.chat_tools(MESSAGES, [])
    await call
    (batch_end,) = ends(events, "batch_openai")
    assert batch_end["error"] == "Batch request failed" and not batch_end.get("usage")
    assert len(ends(events, "sync")) == 1
    assert [kind for kind, _ in events] == ["pre", "post", "pre", "post"]


@pytest.mark.asyncio
async def test_cancel_while_waiting_reports_one_end_and_propagates():
    client, events = make_client("hang")
    task = asyncio.create_task(client.chat_tools(MESSAGES, []))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (end,) = ends(events, "batch_openai")
    assert end["error"] == "cancelled"


@pytest.mark.asyncio
async def test_typed_error_reports_one_end_and_travels_unchanged():
    boom = LLMConnectionError("endpoint dead")  # typed: passed through, no fallback
    client, events = make_client(boom)
    with pytest.raises(LLMConnectionError) as raised:
        await client.chat(MESSAGES)
    assert raised.value is boom
    (end,) = ends(events, "batch_openai")
    assert end["error"] == "endpoint dead"
    assert not ends(events, "sync")


class HookAbort(BaseException):
    """A hook that dies with a BaseException (as a cancel does mid-report)."""


@pytest.mark.asyncio
async def test_a_report_that_dies_is_not_reported_a_second_time():
    client, events = make_client({"assistant": {"content": "ok"}, "usage": USAGE})

    async def post(info):
        events.append(("post", info))
        raise HookAbort()

    client.set_llm_hooks(on_post_response=post)
    with pytest.raises(HookAbort):
        await client.chat(MESSAGES)
    assert len(ends(events, "batch_openai")) == 1


@pytest.mark.asyncio
async def test_without_fallback_a_failed_batch_reports_one_end_then_raises():
    client, events = make_client(RuntimeError("batch api down"), fallback=False)
    with pytest.raises(RuntimeError, match="fallback is disabled"):
        await client.chat(MESSAGES)
    (end,) = ends(events, "batch_openai")
    assert end["error"] == "Batch request failed"
    assert [kind for kind, _ in events] == ["pre", "post"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat", "chat_tools"])
async def test_the_sync_fallback_is_priced_as_sync_with_its_latency(method):
    """The readers ask the wrapper: who answered (batch discount or not), and
    how long it took. After a fallback both answers belong to the sync call."""
    from agent_system.servers.agent.server import _name_the_model

    client, _ = make_client(RuntimeError("batch api down"))
    call = client.chat(MESSAGES) if method == "chat" else client.chat_tools(MESSAGES, [])
    await call
    assert client.last_was_batch is False
    assert client._last_response_duration_ms == SYNC_LATENCY_MS
    assert _name_the_model({}, client)["batch"] is False


@pytest.mark.asyncio
async def test_a_batch_answer_is_priced_as_batch():
    from agent_system.servers.agent.server import _name_the_model

    client, _ = make_client(RuntimeError("batch api down"))
    await client.chat(MESSAGES)  # a fallback first: the flag must come back
    client.queue_manager = FakeQueue({"assistant": {"content": "ok"}, "usage": USAGE})
    await client.chat(MESSAGES)
    assert client.last_was_batch is True
    assert _name_the_model({}, client)["batch"] is True


@pytest.mark.asyncio
async def test_a_streamed_sync_answer_is_priced_as_sync_with_its_latency():
    client, events = make_client({"assistant": {"content": "ok"}, "usage": USAGE})
    await client.chat(MESSAGES)  # a batch answer first: the flag must turn
    chunks = [c async for c in client.chat_tools_streaming(MESSAGES, [])]
    assert chunks[-1]["type"] == "final"
    assert client.last_was_batch is False
    assert client._last_response_duration_ms == SYNC_LATENCY_MS
    assert len(ends(events, "sync")) == 1
