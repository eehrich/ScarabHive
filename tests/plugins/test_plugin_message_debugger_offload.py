"""
PB25: message_debugger DB writes must run OFF the event loop.

The four capture hooks fire on every LLM call and previously did synchronous
sqlite commits (+ json.dumps + token estimation) directly on the event loop.
These tests prove the heavy work now runs in a worker thread (different thread
id) while still persisting correctly.
"""

import asyncio
import threading
from pathlib import Path

import pytest

from agent_system.hooks import HookContext
from agent_system.llm.models import ChatMessage
from plugins.message_debugger.database import MessageDebuggerDB
from plugins.message_debugger.hooks import MessageDebuggerPlugin

PLUGIN_DIR = Path(__file__).parent.parent.parent / "src" / "plugins" / "message_debugger"


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


def _thread_spy(real, sink):
    def spy(*args, **kwargs):
        sink["ident"] = threading.get_ident()
        return real(*args, **kwargs)
    return spy


class TestDbWritesOffloaded:
    @pytest.mark.asyncio
    async def test_turn_insert_runs_off_event_loop(self, hooks, db, messages):
        main_ident = threading.get_ident()
        sink = {}
        db.insert_turn = _thread_spy(db.insert_turn, sink)

        ctx = HookContext(
            hook_type="pre_llm_call", agent_name="a",
            request_id="r1", session_id="s1", messages=messages,
        )
        result = await hooks.debugger_capture_pre_llm(ctx)

        assert result.success is True
        assert "ident" in sink, "insert_turn was never called"
        assert sink["ident"] != main_ident, "turn write ran on the event-loop thread"
        # correctness preserved: the turn was persisted
        assert len(db.get_turns()) == 1

    @pytest.mark.asyncio
    async def test_llm_request_insert_runs_off_event_loop(self, hooks, db):
        main_ident = threading.get_ident()
        sink = {}
        db.insert_llm_request = _thread_spy(db.insert_llm_request, sink)

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
        assert "ident" in sink, "insert_llm_request was never called"
        assert sink["ident"] != main_ident, "request write ran on the event-loop thread"
        assert len(db.get_llm_requests(direction="request")) == 1

    @pytest.mark.asyncio
    async def test_response_insert_runs_off_event_loop(self, hooks, db):
        main_ident = threading.get_ident()
        sink = {}
        db.insert_llm_request = _thread_spy(db.insert_llm_request, sink)

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
        assert sink["ident"] != main_ident, "response write ran on the event-loop thread"
        assert len(db.get_llm_requests(direction="response")) == 1

    @pytest.mark.asyncio
    async def test_capture_still_correct_post_llm(self, hooks, db, messages):
        # End-to-end correctness through the to_thread path (no spy).
        ctx = HookContext(
            hook_type="post_llm_call", agent_name="agentX",
            request_id="r2", session_id="s2", messages=messages,
            llm_response={"model": "gpt-5", "usage": {"total_tokens": 42}},
        )
        result = await hooks.debugger_capture_post_llm(ctx)
        assert result.success is True
        turns = db.get_turns()
        assert len(turns) == 1
        assert turns[0]["snapshot_type"] == "post_llm"
        assert turns[0]["agent_name"] == "agentX"


class TestCaptureCorrectnessThroughToThread:
    """The to_thread refactor must preserve the snapshot content + metadata."""

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
        assert calls["n"] == 0  # no DB write for an empty snapshot
        assert len(db.get_turns()) == 0

    @pytest.mark.asyncio
    async def test_metadata_counts_match(self, hooks, db, messages):
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=messages)
        result = await hooks.debugger_capture_pre_llm(ctx)
        assert result.metadata["captured"] is True
        assert result.metadata["message_count"] == len(messages)
        assert result.metadata["total_tokens"] >= 0

    @pytest.mark.asyncio
    async def test_db_error_does_not_break_hook(self, hooks, db, messages):
        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        db.insert_turn = boom
        ctx = HookContext(hook_type="pre_llm_call", agent_name="a",
                          request_id="r", session_id="s", messages=messages)
        result = await hooks.debugger_capture_pre_llm(ctx)
        # Best-effort capture: a DB failure must NOT fail the LLM call.
        assert result.success is True
        assert "error" in result.metadata


class TestConcurrentCapture:
    @pytest.mark.asyncio
    async def test_many_concurrent_captures_no_race(self, hooks, db):
        async def worker(i: int) -> None:
            msgs = [ChatMessage(role="user", content=f"msg {i}")]
            ctx = HookContext(hook_type="pre_llm_call", agent_name=f"a{i}",
                              request_id=f"r{i}", session_id=f"s{i}", messages=msgs)
            r = await hooks.debugger_capture_pre_llm(ctx)
            assert r.success is True

        # 40 concurrent captures through the to_thread path + thread-local
        # connections; a write race would surface as an exception here.
        await asyncio.gather(*[worker(i) for i in range(40)])
        assert len(db.get_turns()) == 40
