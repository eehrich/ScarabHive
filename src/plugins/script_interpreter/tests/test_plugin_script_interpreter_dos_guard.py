"""
PB10: script_interpreter must not block the event loop nor run unbounded ops.

Two guarantees:
  1. Pathological single C-level ops (`**`, sequence `*`) are rejected up front
     instead of running for tens of seconds / allocating gigabytes.
  2. execute() runs OFF the event loop (worker thread), and concurrent same-
     session calls are serialised by the per-executor lock (no state corruption).
"""

import asyncio
import threading
import time

import pytest
from unittest.mock import Mock

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.script_interpreter.server import ScriptInterpreterServer
from plugins.script_interpreter.executor import ScriptExecutor


class _Status:
    async def progress(self, *a, **k): pass
    async def error(self, *a, **k): pass
    async def end(self, *a, **k): pass


@pytest.fixture
def server():
    return ScriptInterpreterServer(
        "script_interpreter", Mock(spec=AgentSystemConfig),
        ToolServerConfig(type="script_interpreter", enabled=True),
    )


async def _run(server, code, session_id="s1"):
    return await server.call(
        "script_interpreter_execute",
        {"code": code, "_status": _Status(), "_session_id": session_id},
    )


class TestOperandGuards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [
        "x = 99999 ** 9999999",       # ~1.7e8-bit int (would block ~79s)
        'x = 99999 ** 9999999 + 1',   # inside a larger expression
        "x = 2\nx **= 10000000",      # augmented-assign path
    ])
    async def test_pow_blowup_rejected_fast(self, server, code):
        t = time.time()
        result = await _run(server, code)
        elapsed = time.time() - t
        assert "error" in result, f"expected rejection, got {result}"
        assert "too large" in (result.get("error_message") or "")
        assert elapsed < 2.0, f"guard should reject instantly, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [
        's = "a" * 100000000',        # ~100 MB string
        "L = [0] * 100000000",        # ~800 MB list
        "t = (1,) * 100000000",       # tuple
        "s = 'a'\ns *= 100000000",    # augmented-assign path
    ])
    async def test_sequence_mult_blowup_rejected_fast(self, server, code):
        t = time.time()
        result = await _run(server, code)
        elapsed = time.time() - t
        assert "error" in result, f"expected rejection, got {result}"
        assert "too large" in (result.get("error_message") or "")
        assert elapsed < 2.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code,expect", [
        ("print(2 ** 10)", "1024"),
        ("print(2 ** 4096 > 0)", "True"),     # 4097-bit int is well under the limit
        ('print("ab" * 5)', "ababababab"),
        ("print([1, 2] * 3)", "[1, 2, 1, 2, 1, 2]"),
        ("print(3 * 4)", "12"),               # plain int*int never guarded
        ("print(10 ** -2)", "0.01"),          # negative exponent never guarded
    ])
    async def test_legit_ops_still_work(self, server, code, expect):
        result = await _run(server, code)
        assert "error" not in result, f"legit op rejected: {result}"
        assert expect in result["result"]


class TestEventLoopOffload:
    @pytest.mark.asyncio
    async def test_execute_runs_off_event_loop(self, server, monkeypatch):
        main_ident = threading.get_ident()
        seen = {}
        real = ScriptExecutor.execute

        def spy(self, *args, **kwargs):
            seen["ident"] = threading.get_ident()
            return real(self, *args, **kwargs)

        monkeypatch.setattr(ScriptExecutor, "execute", spy)

        result = await _run(server, "print(1 + 1)")
        assert "error" not in result
        assert seen.get("ident") is not None, "executor.execute was never called"
        assert seen["ident"] != main_ident, "execution ran on the event-loop thread"


class TestPerSessionSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_same_session_executes_are_consistent(self, server):
        # Each call sets x then prints it. With the per-executor lock each
        # execute is atomic, so every output reflects its OWN input even though
        # all 30 share one session/executor and run via to_thread.
        async def worker(i):
            r = await _run(server, f"x = {i}\nprint(x)", session_id="shared")
            assert "error" not in r, r
            return i, r["result"]

        results = await asyncio.gather(*[worker(i) for i in range(30)])
        for i, out in results:
            assert f"Output: {i}" in out, f"x corrupted for {i}: {out}"

    @pytest.mark.asyncio
    async def test_distinct_sessions_keep_separate_state(self, server):
        await _run(server, "secret = 111", session_id="a")
        await _run(server, "secret = 222", session_id="b")
        ra = await _run(server, "print(secret)", session_id="a")
        rb = await _run(server, "print(secret)", session_id="b")
        assert "Output: 111" in ra["result"]
        assert "Output: 222" in rb["result"]
