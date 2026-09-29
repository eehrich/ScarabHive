"""The asyncio default executor is sized for blocking I/O, not for CPUs.

Every `asyncio.to_thread` in the API process lands in this one pool — session
persistence, context_engineer compaction, memory, todo, terminal, and the usage
tracker's SQLite write, ~68 call sites across the plugins. Python's default,
`min(32, cpu_count + 4)`, is a CPU-bound formula and gave SIX workers on the
two-core production container. A dozen sub-agents finishing their calls at once
then queued for a slot, and the usage write holds one for up to its 10 s SQLite
busy_timeout — twice the 5 s the post_llm_call hook is allowed.

Measured 2026-09-01: "Hook 'context_usage_tracker.track_usage' timed out after
5.0s" with the CPU 94 % idle and the admin panel at ten seconds.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from unittest.mock import patch

import pytest

from agent_system.app import ASYNC_EXECUTOR_MAX_WORKERS, _build_default_executor


class TestTheSizeIsNotDerivedFromTheCpuCount:
    def test_a_two_core_host_still_gets_the_full_pool(self):
        """The exact production case: cpu_count() == 2 used to mean 6 workers."""
        with patch("os.cpu_count", return_value=2):
            executor = _build_default_executor()
        try:
            assert executor._max_workers == ASYNC_EXECUTOR_MAX_WORKERS
        finally:
            executor.shutdown(wait=False)

    def test_the_pool_is_bigger_than_pythons_cpu_bound_default(self):
        """Counter-check with a number, so the test still bites if the constant
        is quietly lowered: six workers is what the incident ran on."""
        assert ASYNC_EXECUTOR_MAX_WORKERS >= 32

    def test_the_threads_are_named_for_the_profiler(self):
        executor = _build_default_executor()
        try:
            assert executor._thread_name_prefix == "app_asyncio"
        finally:
            executor.shutdown(wait=False)


class TestWhatTheSmallPoolDidToAHook:
    """The mechanism itself, reproduced: a hook with a 5 s budget whose work is
    queued behind blocking I/O never even starts."""

    @pytest.mark.asyncio
    async def test_a_full_pool_starves_the_next_caller_past_its_deadline(self):
        loop = asyncio.get_running_loop()
        crowded = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        loop.set_default_executor(crowded)
        release = __import__("threading").Event()
        try:
            # Two blocking writes occupy the whole pool, the way a contended
            # SQLite write sits on its busy_timeout.
            blockers = [loop.run_in_executor(None, release.wait) for _ in range(2)]
            started = time.monotonic()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.to_thread(lambda: "usage row"), 0.5)
            assert time.monotonic() - started >= 0.5, (
                "the hook must have waited, not failed instantly")
            release.set()
            await asyncio.gather(*blockers)
        finally:
            release.set()
            crowded.shutdown(wait=True)
            loop.set_default_executor(concurrent.futures.ThreadPoolExecutor())

    @pytest.mark.asyncio
    async def test_the_same_call_succeeds_when_a_slot_is_free(self):
        """Counter-check: without the crowding the call is instant, so the test
        above measures the QUEUE and not a broken to_thread."""
        assert await asyncio.wait_for(
            asyncio.to_thread(lambda: "usage row"), 5.0) == "usage row"
