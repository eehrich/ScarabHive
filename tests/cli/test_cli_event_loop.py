"""Tests for the CLI's single, persistent event loop.

The bug this guards against reached production and was invisible: the CLI ran
each of its coroutines through ``asyncio.run``, which closes its loop on
return. Anything a step left running was therefore dead by the next step. It
cost external MCP servers entirely -- they connect during bootstrap, and
``ServerConnection.connected`` is ``task is not None and not task.done()``, so
the agent's tool discovery found a pool with zero connected servers and handed
the agent no external tools at all. The log said "2 connected, 0 failed" one
second earlier, and ``agent-cli mcp test`` worked, because that opens a loop of
its own -- which is exactly why nobody caught it.

The invariant is therefore not "it works" but "consecutive calls share a live
loop", and that is what these test.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agent_system.cli_utils import event_loop


@pytest.fixture(autouse=True)
def _fresh_loop():
    """Each test starts and ends without a CLI loop of its own."""
    event_loop.close_cli_loop()
    yield
    event_loop.close_cli_loop()


class TestRunAsync:
    def test_consecutive_calls_share_one_loop(self):
        seen = []

        async def note():
            seen.append(asyncio.get_running_loop())

        event_loop.run_async(note())
        event_loop.run_async(note())

        assert seen[0] is seen[1]

    def test_a_task_started_in_one_call_is_alive_in_the_next(self):
        """The actual failure: a long-lived connection task must survive."""
        state = {}

        async def start():
            state["task"] = asyncio.create_task(asyncio.sleep(30))
            await asyncio.sleep(0)

        async def inspect():
            return state["task"].done()

        event_loop.run_async(start())
        assert event_loop.run_async(inspect()) is False, (
            "the task from the previous call is already done -- the loop was "
            "closed in between, which is the bug")

    def test_close_cli_loop_cancels_leftovers_instead_of_awaiting_them(self):
        """Teardown must CANCEL what is still running, not wait it out.

        Waiting also ends up closed and cancelled, so the end state alone
        cannot tell the two apart -- only the clock can. The leftovers in
        production are MCP connection tasks that never finish on their own, so
        a teardown that awaits them does not take 30 seconds, it never
        returns.
        """
        state = {}

        async def start():
            state["task"] = asyncio.create_task(asyncio.sleep(30))
            state["loop"] = asyncio.get_running_loop()
            await asyncio.sleep(0)

        event_loop.run_async(start())
        started = time.monotonic()
        event_loop.close_cli_loop()
        elapsed = time.monotonic() - started

        assert elapsed < 5, (
            f"teardown took {elapsed:.1f}s -- it awaited the pending task "
            f"instead of cancelling it")
        assert state["loop"].is_closed()
        assert state["task"].cancelled() or state["task"].done()

    def test_a_new_loop_is_built_after_close(self):
        """A closed loop must not be reused -- the CLI may run again in-process.

        Compare the loop OBJECTS, not their ids: the first loop is closed and
        unreferenced by the time the second exists, so CPython may hand the new
        loop the same address -- an id() comparison failed exactly that way.
        """
        loops = []

        async def note():
            loops.append(asyncio.get_running_loop())

        event_loop.run_async(note())
        event_loop.close_cli_loop()
        event_loop.run_async(note())

        assert loops[0] is not loops[1]
        assert loops[0].is_closed() and not loops[1].is_closed()

    def test_close_is_idempotent(self):
        event_loop.run_async(asyncio.sleep(0))
        event_loop.close_cli_loop()
        event_loop.close_cli_loop()  # must not raise

    def test_exceptions_propagate_like_asyncio_run(self):
        async def boom():
            raise ValueError("durchgereicht")

        with pytest.raises(ValueError, match="durchgereicht"):
            event_loop.run_async(boom())

        # ... and the loop is still usable afterwards.
        assert event_loop.run_async(_answer()) == 42


    def test_another_thread_gets_its_own_loop(self):
        """asyncio.run was per-thread safe; sharing the loop must not undo that.

        Driving one loop from two threads is undefined behaviour, and the
        sequencing this helper exists for is a property of the CLI's single
        main thread. Off it, the fallback must simply work.
        """
        async def loop_id():
            return id(asyncio.get_running_loop())

        main_loop = event_loop.run_async(loop_id())
        result = {}

        def worker():
            try:
                result["id"] = event_loop.run_async(loop_id())
            except Exception as exc:  # pragma: no cover - the failure we guard
                result["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)

        assert "error" not in result, f"off-thread call raised {result.get('error')!r}"
        assert result["id"] != main_loop


async def _answer() -> int:
    return 42
