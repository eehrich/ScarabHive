"""The cancel every client hands to the agent server.

`execute_with_cancellation` reports the cancel it detects as a plain
Exception; the server reads only `asyncio.CancelledError` as a cancel, and
anything else walks the fallback chain and ends the run as a failure.
"""
import asyncio
from types import SimpleNamespace

import pytest

from plugins.llm_common import cancellation


async def test_a_cancelled_call_ends_as_a_cancel():
    token = SimpleNamespace(is_cancelled=False)

    async def slow_answer():
        token.is_cancelled = True
        await asyncio.sleep(30)  # only the cancel can end this

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await asyncio.wait_for(
            cancellation.await_call(asyncio.ensure_future(slow_answer()), token), timeout=5)

    assert str(cancelled.value) == "Request cancelled by user"
    assert cancelled.value.__cause__ is not None, "the reason the call ended is lost"


async def test_an_error_keeps_its_error():
    """Only a cancel is a cancel: an upstream error must keep its fallback chain."""
    token = SimpleNamespace(is_cancelled=False)

    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await cancellation.await_call(asyncio.ensure_future(fail()), token)


async def test_an_answer_comes_back():
    token = SimpleNamespace(is_cancelled=False)

    async def answer():
        return "hello"

    assert await cancellation.await_call(asyncio.ensure_future(answer()), token) == "hello"


async def test_without_a_token_the_call_still_runs():
    """The watcher reads the token unguarded, so None must not reach it."""

    async def answer():
        await asyncio.sleep(0.25)  # long enough for the watcher to look once
        return "hello"

    assert await cancellation.await_call(asyncio.ensure_future(answer()), None) == "hello"


async def test_a_cancel_from_outside_ends_the_call():
    """The run itself was cancelled: the provider call must not keep billing."""
    started = asyncio.Event()

    async def slow_answer():
        started.set()
        await asyncio.sleep(30)

    call = asyncio.ensure_future(slow_answer())
    waiting = asyncio.ensure_future(cancellation.await_call(call, SimpleNamespace(is_cancelled=False)))
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    # 0.15 s: long enough for the core watcher to notice the finished call and
    # end itself, so it does not outlive the test loop.
    await asyncio.sleep(0.15)
    assert call.cancelled(), "the provider call was left running"


async def test_the_call_is_over_before_the_caller_cleans_up():
    """The caller closes its HTTP client next; a request only asked to stop still holds it."""
    started, torn_down = asyncio.Event(), asyncio.Event()

    async def slow_answer():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # A client that closes its connection on the way out: asking it to
            # stop is not the same as it having stopped.
            await asyncio.sleep(0.05)
            torn_down.set()

    call = asyncio.ensure_future(slow_answer())
    waiting = asyncio.ensure_future(cancellation.await_call(call, SimpleNamespace(is_cancelled=False)))
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert torn_down.is_set(), "the call was only asked to stop, not waited for"
