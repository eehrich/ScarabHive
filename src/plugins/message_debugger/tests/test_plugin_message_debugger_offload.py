"""
message_debugger capture is FIRE-AND-FORGET.

The four capture hooks fire on every LLM call. They used to do the sqlite commit
(+ json.dumps + token estimation) on a worker thread but still *await* it — the
event loop stayed free, yet the agent's own coroutine blocked on the await, which
wedged a whole pipeline run for ~20 min against a multi-GB DB. Now the hooks only
ENQUEUE; a single background writer thread ("msgdbg-writer") does the actual
write. These tests prove: the hook returns without waiting for the DB, the write
runs on the writer thread, and the data still persists correctly after a flush.
"""

import asyncio
import threading
import time
from pathlib import Path

import pytest

from agent_system.hooks import HookContext
from agent_system.llm.models import ChatMessage
from plugins.message_debugger.database import MessageDebuggerDB
from plugins.message_debugger.hooks import MessageDebuggerPlugin

PLUGIN_DIR = Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "message_debugger"


@pytest.fixture
def db(tmp_path):
    d = MessageDebuggerDB(tmp_path / "offload.db", wal_mode=False)
    yield d
    d.close()


@pytest.fixture
def hooks(db):
    h = MessageDebuggerPlugin(PLUGIN_DIR, db=db)
    h.capture_enabled = True
    h.capture_pre_llm = True
    h.capture_post_llm = True
    h.capture_llm_requests = True
    return h


@pytest.fixture
def messages():
    return [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Hi there"),
    ]


class TestFireAndForget:
    @pytest.mark.asyncio
    async def test_hook_returns_without_awaiting_db(self, hooks, db, messages):
        # A slow DB write must NOT delay the hook: the agent only enqueues. This
        # is the whole point of the fix — a wedged multi-GB write can't stall the
        # pipeline anymore.
        real = db.insert_turn

        def slow(*args, **kwargs):
            time.sleep(1.0)
            return real(*args, **kwargs)

        db.insert_turn = slow
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=messages)
        t0 = time.perf_counter()
        result = await hooks.debugger_capture_pre_llm(ctx)
        elapsed = time.perf_counter() - t0

        assert result.success is True
        assert result.metadata.get("queued") is True
        assert elapsed < 0.2, f"hook blocked {elapsed:.2f}s on the (slow) DB write"
        # The write is running on the background writer; drain it, then it's there.
        assert db.flush(timeout=3)
        assert len(db.get_turns()) == 1

    @pytest.mark.asyncio
    async def test_turn_write_runs_on_writer_thread(self, hooks, db, messages):
        main_ident = threading.get_ident()
        sink = {}
        real = db.insert_turn

        def spy(*args, **kwargs):
            sink["ident"] = threading.get_ident()
            sink["name"] = threading.current_thread().name
            return real(*args, **kwargs)

        db.insert_turn = spy
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r1", session_id="s1", messages=messages)
        result = await hooks.debugger_capture_pre_llm(ctx)

        assert result.success is True
        assert db.flush(timeout=3)
        assert sink.get("ident") not in (None, main_ident), "write ran on the caller thread"
        assert sink.get("name") == "msgdbg-writer"
        assert len(db.get_turns()) == 1

    @pytest.mark.asyncio
    async def test_llm_request_write_off_caller_thread(self, hooks, db):
        main_ident = threading.get_ident()
        sink = {}
        real = db.insert_llm_request

        def spy(*args, **kwargs):
            sink["ident"] = threading.get_ident()
            return real(*args, **kwargs)

        db.insert_llm_request = spy
        ctx = HookContext(
            hook_type="pre_llm_request", agent_name="a",
            request_id="r1", session_id="s1",
            llm_provider="openai", llm_model="gpt-5",
            llm_request_url="https://api.openai.com/v1/chat/completions",
            llm_is_streaming=False,
            llm_request_payload={"messages": [{"role": "user", "content": "hi"}]},
        )
        result = await hooks.debugger_capture_pre_request(ctx)

        assert result.success is True
        assert db.flush(timeout=3)
        assert sink.get("ident") not in (None, main_ident)
        assert len(db.get_llm_requests(direction="request")) == 1

    @pytest.mark.asyncio
    async def test_response_write_off_caller_thread(self, hooks, db):
        main_ident = threading.get_ident()
        sink = {}
        real = db.insert_llm_request

        def spy(*args, **kwargs):
            sink["ident"] = threading.get_ident()
            return real(*args, **kwargs)

        db.insert_llm_request = spy
        ctx = HookContext(
            hook_type="post_llm_response", agent_name="a",
            request_id="r1", session_id="s1",
            llm_provider="openai", llm_model="gpt-5",
            llm_duration_ms=120.0, llm_finish_reason="stop",
            llm_usage={"prompt_tokens": 5, "completion_tokens": 7},
            llm_response_data={"choices": [{"message": {"content": "ok"}}]},
        )
        result = await hooks.debugger_capture_post_response(ctx)

        assert result.success is True
        assert db.flush(timeout=3)
        assert sink.get("ident") not in (None, main_ident)
        assert len(db.get_llm_requests(direction="response")) == 1


class TestCaptureCorrectness:
    """The async pipeline must preserve snapshot content + metadata (after flush)."""

    @pytest.mark.asyncio
    async def test_correct_post_llm_after_flush(self, hooks, db, messages):
        ctx = HookContext(
            hook_type="post_llm_call", agent_name="agentX",
            request_id="r2", session_id="s2", messages=messages,
            llm_response={"model": "gpt-5", "usage": {"total_tokens": 42}},
        )
        result = await hooks.debugger_capture_post_llm(ctx)
        assert result.success is True
        assert db.flush(timeout=3)
        turns = db.get_turns()
        assert len(turns) == 1
        assert turns[0]["snapshot_type"] == "post_llm"
        assert turns[0]["agent_name"] == "agentX"

    @pytest.mark.asyncio
    async def test_tool_calls_captured(self, hooks, db):
        captured = {}
        real = db.insert_turn

        def spy(*args, **kwargs):
            captured["messages"] = kwargs.get("messages")
            return real(*args, **kwargs)

        db.insert_turn = spy
        msgs = [
            ChatMessage(role="user", content="weather?"),
            ChatMessage(role="assistant", content=None, tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "weather", "arguments": '{"city":"Berlin"}'},
            }]),
        ]
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=msgs)
        result = await hooks.debugger_capture_pre_llm(ctx)

        assert result.success is True
        assert db.flush(timeout=3)
        md = captured["messages"]
        assert md is not None and len(md) == 2
        assistant = md[1]
        assert assistant["tool_call_count"] == 1
        assert assistant["tool_calls"][0]["function"]["name"] == "weather"

    @pytest.mark.asyncio
    async def test_empty_messages_skips_insert(self, hooks, db):
        calls = {"n": 0}
        real = db.insert_turn

        def spy(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        db.insert_turn = spy
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=[])
        result = await hooks.debugger_capture_pre_llm(ctx)

        assert result.success is True
        assert result.metadata.get("reason") == "no_messages"
        assert db.flush(timeout=3)
        assert calls["n"] == 0  # no DB write enqueued for an empty snapshot
        assert len(db.get_turns()) == 0

    @pytest.mark.asyncio
    async def test_metadata_queued(self, hooks, db, messages):
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=messages)
        result = await hooks.debugger_capture_pre_llm(ctx)
        # Counts are no longer known at hook-return time (the build runs on the
        # writer thread); the hook reports that it queued the snapshot.
        assert result.metadata.get("queued") is True
        assert result.metadata.get("snapshot_type") == "pre_llm"

    @pytest.mark.asyncio
    async def test_db_error_does_not_break_hook(self, hooks, db, messages):
        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        db.insert_turn = boom
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=messages)
        # Best-effort capture: the hook returns success (it only enqueued); the
        # write fails on the writer thread but is swallowed there — nothing
        # crashes, nothing persisted.
        result = await hooks.debugger_capture_pre_llm(ctx)
        assert result.success is True
        db.flush(timeout=3)
        assert len(db.get_turns()) == 0


class TestConcurrentCapture:
    @pytest.mark.asyncio
    async def test_many_concurrent_captures_no_race(self, hooks, db):
        async def worker(i: int) -> None:
            msgs = [ChatMessage(role="user", content=f"msg {i}")]
            ctx = HookContext(hook_type="pre_llm_call", agent_name=f"a{i}",
                              request_id=f"r{i}", session_id=f"s{i}", messages=msgs)
            r = await hooks.debugger_capture_pre_llm(ctx)
            assert r.success is True

        # 40 concurrent captures enqueue onto one queue; the single writer thread
        # serializes the writes, so there is no write race by construction.
        await asyncio.gather(*[worker(i) for i in range(40)])
        assert db.flush(timeout=5)
        assert len(db.get_turns()) == 40
