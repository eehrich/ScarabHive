"""Tests for message_debugger plugin.

Tests the database layer, hook capture, API endpoints, and hybrid plugin integration
using the SQLite-backed storage system.
"""
from __future__ import annotations

import pytest
import time
from pathlib import Path
from types import SimpleNamespace

from agent_system.hooks import HookContext
from agent_system.llm.models import ChatMessage
from agent_system.config.models import AgentSystemConfig, MCPConfig


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def plugin_dir():
    """Get path to message_debugger plugin directory."""
    return Path(__file__).parent.parent.parent.parent.parent / "src" / "plugins" / "message_debugger"


@pytest.fixture
def db(tmp_path):
    """Create a temporary MessageDebuggerDB instance."""
    from plugins.message_debugger.database import MessageDebuggerDB
    db = MessageDebuggerDB(tmp_path / "test_debugger.db", wal_mode=False)
    yield db
    db.close()


@pytest.fixture
def hooks_plugin(plugin_dir, db):
    """Create MessageDebuggerPlugin instance with SQLite DB."""
    from plugins.message_debugger.hooks import MessageDebuggerPlugin
    return MessageDebuggerPlugin(plugin_dir, db=db)


@pytest.fixture
def hybrid_plugin(tmp_path):
    """Create MessageDebuggerHybridPlugin instance with temp DB."""
    from plugins.message_debugger.plugin import MessageDebuggerHybridPlugin

    system_config = AgentSystemConfig()
    mcp_config = MCPConfig()
    mcp_config.config = {"db_path": str(tmp_path / "hybrid_test.db")}

    return MessageDebuggerHybridPlugin("message_debugger", system_config, mcp_config)


@pytest.fixture
def web_factory(db, hybrid_plugin):
    """Create MessageDebuggerWebFactory instance with DB."""
    from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory
    return MessageDebuggerWebFactory(db=db, name="message_debugger", server=hybrid_plugin)


@pytest.fixture
def sample_messages():
    """Create sample chat messages for testing."""
    return [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="What is the capital of France?"),
        ChatMessage(role="assistant", content="The capital of France is Paris."),
    ]


@pytest.fixture
def sample_messages_with_tools():
    """Create sample messages including tool calls."""
    return [
        ChatMessage(role="user", content="What's the weather in Berlin?"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[{
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "weather_forecast",
                    "arguments": '{"city": "Berlin", "units": "celsius"}'
                }
            }]
        ),
        ChatMessage(
            role="tool",
            content='{"temperature": 22, "condition": "sunny"}',
            tool_call_id="call_123"
        ),
        ChatMessage(role="assistant", content="The weather in Berlin is sunny with 22°C."),
    ]


# ============================================================================
# Database Tests
# ============================================================================

class TestMessageDebuggerDB:
    """Test SQLite database layer."""

    def test_create_tables(self, db):
        """Test that tables are created."""
        conn = db._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in tables}
        assert "turns" in names
        assert "llm_requests" in names

    def test_insert_and_get_turn(self, db):
        """Test inserting and retrieving a turn."""
        ts = time.time() * 1000
        row_id = db.insert_turn(
            timestamp_ms=ts,
            snapshot_type="pre_llm",
            agent_name="test_agent",
            request_id="req_1",
            session_id="sess_1",
            step=2,
            message_count=3,
            total_tokens=150,
            messages=[{"role": "user", "content": "hello"}],
        )
        assert row_id > 0

        turn = db.get_turn(row_id)
        assert turn is not None
        assert turn["snapshot_type"] == "pre_llm"
        assert turn["agent_name"] == "test_agent"
        assert turn["message_count"] == 3
        assert turn["total_tokens"] == 150
        assert turn["messages_json"][0]["role"] == "user"

    def test_get_turns_with_filters(self, db):
        """Test filtering turns by agent and session."""
        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="agent_a", session_id="s1")
        db.insert_turn(ts, "post_llm", agent_name="agent_b", session_id="s2")
        db.insert_turn(ts, "pre_llm", agent_name="agent_a", session_id="s3")

        # Filter by agent
        turns = db.get_turns(agent_name="agent_a")
        assert len(turns) == 2

        # Filter by session
        turns = db.get_turns(session_id="s2")
        assert len(turns) == 1
        assert turns[0]["agent_name"] == "agent_b"

        # Filter by type
        turns = db.get_turns(snapshot_type="post_llm")
        assert len(turns) == 1

    def test_count_turns(self, db):
        """Test counting turns."""
        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="a")
        db.insert_turn(ts, "pre_llm", agent_name="a")
        db.insert_turn(ts, "pre_llm", agent_name="b")

        assert db.count_turns() == 3
        assert db.count_turns(agent_name="a") == 2

    def test_a_count_up_to_an_id_reads_an_index_not_the_table(self, db):
        """The rows are large: a count bounded as a rowid range would read every page of the table."""
        for table in ("turns", "llm_requests"):
            where, params = db._where(max_id=7)
            plan = " | ".join(row[3] for row in db._get_conn().execute(
                f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM {table}{where}", params))
            assert "COVERING INDEX" in plan, plan

    def test_newest_id_is_asked_of_the_list_tables_only(self, db):
        assert db.newest_id("turns") == 0
        assert db.newest_id("llm_requests") == 0
        with pytest.raises(ValueError):
            db.newest_id("turns; DROP TABLE turns")

    def test_insert_and_get_llm_request(self, db):
        """Test inserting and retrieving LLM requests."""
        ts = time.time() * 1000
        row_id = db.insert_llm_request(
            timestamp_ms=ts,
            direction="request",
            agent_name="test_agent",
            request_id="req_1",
            provider="openai",
            model="gpt-4o",
            url="https://api.openai.com/v1/chat/completions",
            is_streaming=True,
            payload={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert row_id > 0

        req = db.get_llm_request(row_id)
        assert req is not None
        assert req["direction"] == "request"
        assert req["provider"] == "openai"
        assert req["model"] == "gpt-4o"
        assert req["is_streaming"] == 1
        assert req["payload_json"]["messages"][0]["content"] == "hi"

    def test_a_database_from_before_served_by_gets_the_column(self, tmp_path):
        import sqlite3
        from plugins.message_debugger.database import MessageDebuggerDB

        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE llm_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms REAL NOT NULL,
            direction TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            url TEXT DEFAULT '', is_streaming INTEGER DEFAULT 0, payload_json TEXT, response_json TEXT, error TEXT,
            duration_ms REAL, usage_json TEXT, finish_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        old.execute("INSERT INTO llm_requests (timestamp_ms, direction) VALUES (1, 'response')")
        old.commit()
        old.close()

        db = MessageDebuggerDB(path, wal_mode=False)
        try:
            db.insert_llm_request(2, "response", served_by="DeepInfra")
            assert [row["served_by"] for row in db.get_llm_requests()] == ["DeepInfra", None]
        finally:
            db.close()

    def test_get_llm_requests_with_filters(self, db):
        """Test filtering LLM requests."""
        ts = time.time() * 1000
        db.insert_llm_request(ts, "request", provider="openai", model="gpt-4o")
        db.insert_llm_request(ts, "response", provider="openai", model="gpt-4o", duration_ms=500)
        db.insert_llm_request(ts, "request", provider="anthropic", model="claude-3")

        assert len(db.get_llm_requests(direction="request")) == 2
        assert len(db.get_llm_requests(provider="anthropic")) == 1
        assert db.count_llm_requests(provider="openai") == 2

    def test_get_stats(self, db):
        """Test stats aggregation."""
        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="a", session_id="s1", total_tokens=100)
        db.insert_turn(ts, "post_llm", agent_name="b", session_id="s2", total_tokens=200)
        db.insert_llm_request(ts, "request", provider="openai")
        db.insert_llm_request(ts, "response", provider="openai", duration_ms=300)
        db.insert_llm_request(ts, "response", provider="anthropic", error="timeout")

        stats = db.get_stats()
        assert stats["total_turns"] == 2
        assert stats["total_llm_requests"] == 3
        assert "a" in stats["unique_agents"]
        assert "b" in stats["unique_agents"]
        assert "openai" in stats["unique_providers"]
        assert stats["unique_session_count"] >= 2
        assert stats["error_count"] == 1
        assert "db_size_mb" in stats

    def test_clear_all(self, db):
        """Test clearing all data."""
        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm")
        db.insert_turn(ts, "post_llm")
        db.insert_llm_request(ts, "request")

        result = db.clear_all()
        assert result["turns_deleted"] == 2
        assert result["requests_deleted"] == 1
        assert db.count_turns() == 0
        assert db.count_llm_requests() == 0

    def test_json_serialization(self, db):
        """Test that JSON fields round-trip correctly."""
        ts = time.time() * 1000
        complex_payload = {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "test"}}],
            "temperature": 0.7,
            "nested": {"a": [1, 2, 3]}
        }
        row_id = db.insert_llm_request(
            ts, "request", payload=complex_payload
        )
        req = db.get_llm_request(row_id)
        assert req["payload_json"] == complex_payload


# ============================================================================
# Hook Plugin Tests
# ============================================================================

class TestMessageDebuggerHooks:
    """Test message capture hook functionality."""

    @pytest.mark.asyncio
    async def test_capture_pre_llm_basic(self, hooks_plugin, sample_messages, db):
        """Test basic pre-LLM message capture into SQLite."""
        context = HookContext(
            hook_type="pre_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages
        )

        result = await hooks_plugin.debugger_capture_pre_llm(context)

        assert result.success is True
        assert result.modified is False

        assert db.flush(timeout=3)  # capture is fire-and-forget; drain the writer
        turns = db.get_turns()
        assert len(turns) == 1
        assert turns[0]["snapshot_type"] == "pre_llm"
        assert turns[0]["agent_name"] == "test_agent"
        assert turns[0]["message_count"] == 3

    @pytest.mark.asyncio
    async def test_capture_post_llm_basic(self, hooks_plugin, sample_messages, db):
        """Test basic post-LLM message capture."""
        context = HookContext(
            hook_type="post_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages,
            llm_response={"model": "gpt-4", "usage": {"total_tokens": 50}}
        )

        result = await hooks_plugin.debugger_capture_post_llm(context)

        assert result.success is True
        assert db.flush(timeout=3)
        turns = db.get_turns()
        assert len(turns) == 1
        assert turns[0]["snapshot_type"] == "post_llm"
        # a list row carries only the usage of the response; the turn itself all of it
        assert turns[0]["usage_json"] == {"total_tokens": 50}
        assert "llm_response_json" not in turns[0]
        assert db.get_turn(turns[0]["id"])["llm_response_json"]["model"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_capture_disabled(self, hooks_plugin, sample_messages, db):
        """Test that capture can be disabled."""
        hooks_plugin.capture_pre_llm = False

        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages
        )

        await hooks_plugin.debugger_capture_pre_llm(context)
        assert db.count_turns() == 0

    @pytest.mark.asyncio
    async def test_capture_empty_messages(self, hooks_plugin, db):
        """Test handling of empty message list."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=[]
        )

        result = await hooks_plugin.debugger_capture_pre_llm(context)

        assert result.success is True
        assert result.metadata["reason"] == "no_messages"
        assert db.count_turns() == 0

    @pytest.mark.asyncio
    async def test_capture_with_tool_calls(self, hooks_plugin, sample_messages_with_tools, db):
        """Test capture of messages with tool calls."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages_with_tools
        )

        result = await hooks_plugin.debugger_capture_pre_llm(context)

        assert result.success is True
        assert db.flush(timeout=3)
        turns = db.get_turns()
        turn_detail = db.get_turn(turns[0]["id"])
        messages = turn_detail["messages_json"]

        tool_call_msg = next(m for m in messages if m.get("tool_calls"))
        assert tool_call_msg["tool_call_count"] == 1
        assert tool_call_msg["tool_calls"][0]["function"]["name"] == "weather_forecast"

        tool_result_msg = next(m for m in messages if m.get("is_tool_result"))
        assert tool_result_msg["tool_call_id"] == "call_123"

    @pytest.mark.asyncio
    async def test_capture_keeps_every_message_field(self, hooks_plugin, db):
        """No hand-picked field list: whatever a message carries is in the snapshot,
        including a field ChatMessage does not have yet."""
        class FutureMessage(ChatMessage):
            brand_new_field: str = "added later"

        blob, long_text = "x" * 5000, "y" * 5000
        context = HookContext(
            hook_type="pre_llm_call", request_id="req_1", session_id="sess_1",
            agent_name="test_agent",
            messages=[
                ChatMessage(role="user", content=long_text),
                ChatMessage(role="assistant", content="ok", served_by="Google AI Studio",
                            thinking_model="some-model", rd_orphaned=False,
                            reasoning_details=[{"type": "reasoning.encrypted", "data": blob}]),
                FutureMessage(role="user", content="next"),
            ],
        )
        await hooks_plugin.debugger_capture_pre_llm(context)

        assert db.flush(timeout=3)
        user, assistant, future = db.get_turn(db.get_turns()[0]["id"])["messages_json"]
        assert user["content"] == long_text  # content stays whole
        assert "reasoning_content" not in user  # unset fields are not stored
        assert assistant["served_by"] == "Google AI Studio"
        assert assistant["thinking_model"] == "some-model"
        assert assistant["rd_orphaned"] is False
        data = assistant["reasoning_details"][0]["data"]
        assert data.startswith("x" * 500) and data.endswith("[5000 chars]")
        assert len(data) < 600  # an opaque blob is cut to a preview
        assert future["brand_new_field"] == "added later"

    @pytest.mark.asyncio
    async def test_max_field_chars_zero_keeps_everything(self, plugin_dir, db):
        from plugins.message_debugger.hooks import MessageDebuggerPlugin
        plugin = MessageDebuggerPlugin(
            plugin_dir, db=db, mcp_config=SimpleNamespace(config={"max_field_chars": 0}))
        blob = "x" * 5000
        context = HookContext(
            hook_type="pre_llm_call", request_id="r", session_id="s", agent_name="a",
            messages=[ChatMessage(role="assistant", content="ok",
                                  reasoning_details=[{"data": blob}])],
        )
        await plugin.debugger_capture_pre_llm(context)

        assert db.flush(timeout=3)
        (message,) = db.get_turn(db.get_turns()[0]["id"])["messages_json"]
        assert message["reasoning_details"][0]["data"] == blob

    @pytest.mark.asyncio
    async def test_post_llm_stores_the_whole_response_compacted(self, hooks_plugin, sample_messages, db):
        context = HookContext(
            hook_type="post_llm_call", request_id="r", session_id="s", agent_name="a",
            messages=sample_messages,
            llm_response={"model": "m", "usage": {"prompt_tokens": 10},
                          "assistant": {"served_by": "DeepInfra", "content": "z" * 5000}},
        )
        await hooks_plugin.debugger_capture_post_llm(context)

        assert db.flush(timeout=3)
        stored = db.get_turn(db.get_turns()[0]["id"])["llm_response_json"]
        assert stored["usage"] == {"prompt_tokens": 10}
        assert stored["assistant"]["served_by"] == "DeepInfra"
        assert stored["assistant"]["content"].endswith("[5000 chars]")

    @pytest.mark.asyncio
    async def test_tool_call_details_can_be_left_out(self, plugin_dir, sample_messages_with_tools, db):
        from plugins.message_debugger.hooks import MessageDebuggerPlugin
        plugin = MessageDebuggerPlugin(
            plugin_dir, db=db, mcp_config=SimpleNamespace(config={"include_tool_calls": False}))
        context = HookContext(
            hook_type="pre_llm_call", request_id="r", session_id="s", agent_name="a",
            messages=sample_messages_with_tools,
        )
        await plugin.debugger_capture_pre_llm(context)

        assert db.flush(timeout=3)
        messages = db.get_turn(db.get_turns()[0]["id"])["messages_json"]
        assert not any({"tool_calls", "tool_call_id", "tool_call_count"} & set(m) for m in messages)

    @pytest.mark.asyncio
    async def test_token_estimation(self, hooks_plugin, sample_messages, db):
        """Test token estimation for captured messages."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages
        )

        await hooks_plugin.debugger_capture_pre_llm(context)

        assert db.flush(timeout=3)
        turns = db.get_turns()
        assert turns[0]["total_tokens"] > 0
        turn_detail = db.get_turn(turns[0]["id"])
        for msg in turn_detail["messages_json"]:
            assert msg["estimated_tokens"] is not None
            assert msg["estimated_tokens"] > 0

    @pytest.mark.asyncio
    async def test_capture_preserves_context(self, hooks_plugin, sample_messages):
        """Test that capture hook doesn't modify context."""
        context = HookContext(
            hook_type="pre_llm_call",
            request_id="req_123",
            session_id="sess_456",
            agent_name="test_agent",
            messages=sample_messages
        )

        original_messages = context.messages.copy()
        result = await hooks_plugin.debugger_capture_pre_llm(context)

        assert result.modified is False
        assert context.messages == original_messages

    @pytest.mark.asyncio
    async def test_capture_pre_request(self, hooks_plugin, db):
        """Test pre_llm_request hook captures request payload."""
        context = HookContext(
            hook_type="pre_llm_request",
            agent_name="test_agent",
            request_id="req_1",
            session_id="sess_1",
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_request_url="https://api.openai.com/v1/chat/completions",
            llm_is_streaming=True,
            llm_request_payload={"messages": [{"role": "user", "content": "hi"}]},
        )

        result = await hooks_plugin.debugger_capture_pre_request(context)

        assert result.success is True
        assert db.flush(timeout=3)
        reqs = db.get_llm_requests(direction="request")
        assert len(reqs) == 1
        assert reqs[0]["provider"] == "openai"
        assert reqs[0]["model"] == "gpt-4o"
        assert reqs[0]["is_streaming"] == 1

    @pytest.mark.asyncio
    async def test_capture_post_response(self, hooks_plugin, db):
        """Test post_llm_response hook captures response data."""
        context = HookContext(
            hook_type="post_llm_response",
            agent_name="test_agent",
            request_id="req_1",
            session_id="sess_1",
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_duration_ms=750.5,
            llm_finish_reason="stop",
            llm_usage={"prompt_tokens": 10, "completion_tokens": 20},
            llm_response_data={"choices": [{"message": {"content": "hello"}}]},
        )

        result = await hooks_plugin.debugger_capture_post_response(context)

        assert result.success is True
        assert db.flush(timeout=3)
        reqs = db.get_llm_requests(direction="response")
        assert len(reqs) == 1
        assert reqs[0]["duration_ms"] == pytest.approx(750.5)
        assert reqs[0]["finish_reason"] == "stop"
        assert reqs[0]["usage_json"]["prompt_tokens"] == 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize("metadata, expected", [
        ({"served_by": "Google AI Studio"}, "Google AI Studio"),
        ({}, None),  # a provider that names no backend
    ])
    async def test_capture_post_response_keeps_the_backend_that_served_it(self, hooks_plugin, db, metadata, expected):
        context = HookContext(hook_type="post_llm_response", request_id="req_1", session_id="sess_1",
                              llm_provider="openai_responses", llm_model="m", metadata=metadata)

        await hooks_plugin.debugger_capture_post_response(context)

        assert db.flush(timeout=3)
        row = db.get_llm_requests(direction="response")[0]
        assert row["served_by"] == expected, "the list rows lack the backend"
        assert db.get_llm_request(row["id"])["served_by"] == expected

    @pytest.mark.asyncio
    async def test_capture_post_response_with_error(self, hooks_plugin, db):
        """Test capturing an error response."""
        context = HookContext(
            hook_type="post_llm_response",
            agent_name="test_agent",
            request_id="req_1",
            session_id="sess_1",
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_duration_ms=100.0,
            llm_error="Rate limit exceeded",
        )

        result = await hooks_plugin.debugger_capture_post_response(context)

        assert result.success is True
        assert db.flush(timeout=3)
        reqs = db.get_llm_requests()
        assert len(reqs) == 1
        assert reqs[0]["error"] == "Rate limit exceeded"

    @pytest.mark.asyncio
    async def test_capture_llm_requests_disabled(self, hooks_plugin, db):
        """Test that LLM request capture can be disabled."""
        hooks_plugin.capture_llm_requests = False

        context = HookContext(
            hook_type="pre_llm_request",
            agent_name="test_agent",
            request_id="req_1",
            session_id="sess_1",
            llm_provider="openai",
            llm_request_payload={"messages": []},
        )

        await hooks_plugin.debugger_capture_pre_request(context)
        assert db.count_llm_requests() == 0


# ============================================================================
# Web Endpoints Tests
# ============================================================================

class TestMessageDebuggerWebEndpoints:
    """Test REST API endpoints."""

    @pytest.mark.asyncio
    async def test_list_turns_empty(self, web_factory):
        """Test listing turns when DB is empty."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        router = web_factory.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/turns")
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 0
        assert result["turns"] == []

    @pytest.mark.asyncio
    async def test_list_turns_with_data(self, web_factory, db):
        """Test listing turns with data."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="agent_1", session_id="s1", message_count=3)
        db.insert_turn(ts, "post_llm", agent_name="agent_2", session_id="s2", message_count=5)

        app = FastAPI()
        router = web_factory.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/turns")
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 2
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_lists_hold_still_at_the_newest_id_they_were_answered_as_of(self, web_factory, db):
        """Each list says the newest id it was answered as of; asked with it as ``max_id``, a list leaves out what
        was captured since, in its entries and its total. Numbers past SQLite's integers are refused, not a 500."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(web_factory.get_web_router())
        client = TestClient(app)
        base = "/plugins/message_debugger"
        ts = time.time() * 1000
        kept = {"turns": db.insert_turn(ts, "pre_llm", agent_name="a"),
                "llm-requests": db.insert_llm_request(ts, "request", agent_name="a")}
        field = {"turns": "turns", "llm-requests": "requests"}
        as_of = {path: client.get(f"{base}/{path}").json()["as_of_id"] for path in kept}
        assert as_of == kept
        db.insert_turn(ts + 1, "pre_llm", agent_name="a")
        db.insert_llm_request(ts + 1, "response", agent_name="a")
        for path, newest in as_of.items():
            held = client.get(f"{base}/{path}?max_id={newest}").json()
            assert ([entry["id"] for entry in held[field[path]]], held["total"], held["as_of_id"]) == ([newest], 1, newest)
            now = client.get(f"{base}/{path}").json()
            assert (now["total"], now["as_of_id"]) == (2, newest + 1)
            for asked in ("max_id", "offset"):
                assert client.get(f"{base}/{path}?{asked}={2**63}").status_code == 422
            assert client.get(f"{base}/{path}/{2**63}").status_code == 404

    @pytest.mark.asyncio
    async def test_list_turns_filter_by_agent(self, web_factory, db):
        """Test filtering turns by agent name."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="agent_1")
        db.insert_turn(ts, "pre_llm", agent_name="agent_2")
        db.insert_turn(ts, "pre_llm", agent_name="agent_1")

        app = FastAPI()
        router = web_factory.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/turns?agent_name=agent_1")
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_lists_narrow_to_a_request_and_count_what_their_filters_match(self, web_factory, db):
        """The panel opened from a chat answer asks for its request: both lists narrow to it and the calls under
        it -- tool calls and sub-agents run under ``<request_id>_...`` -- and total counts what all the filters
        given match, not the whole table."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="a", request_id="r-1", session_id="s-1")
        db.insert_turn(ts + 1, "post_llm", agent_name="a", request_id="r-1", session_id="s-1")
        db.insert_turn(ts + 2, "pre_llm", agent_name="tool", request_id="r-1_001", session_id="s-1")
        db.insert_turn(ts + 3, "pre_llm", agent_name="sub", request_id="r-1_sub_ab12", session_id="s-sub")
        # other requests are the newest: a list that ignores the filter shows them first; r-10 and r-1a share
        # r-1's first characters without being calls under it
        for offset, request_id in enumerate(("r-2", "r-10", "r-1a"), start=4):
            db.insert_turn(ts + offset, "pre_llm", agent_name="a", request_id=request_id, session_id="s-1")
        db.insert_llm_request(ts, "request", agent_name="a", request_id="r-1", session_id="s-1", provider="p")
        db.insert_llm_request(ts + 1, "response", agent_name="a", request_id="r-1", session_id="s-1", provider="p")
        db.insert_llm_request(ts + 2, "response", agent_name="sub", request_id="r-1_sub_ab12", session_id="s-sub",
                              provider="p")
        db.insert_llm_request(ts + 3, "response", agent_name="a", request_id="r-2", session_id="s-2", provider="p")

        app = FastAPI()
        app.include_router(web_factory.get_web_router())
        client = TestClient(app)

        turns = client.get("/plugins/message_debugger/turns?request_id=r-1").json()
        assert [turn["request_id"] for turn in turns["turns"]] == ["r-1_sub_ab12", "r-1_001", "r-1", "r-1"]
        assert turns["total"] == 4
        assert client.get("/plugins/message_debugger/turns?request_id=r-1&limit=1").json()["turns"][0]["request_id"] \
            == "r-1_sub_ab12"
        assert client.get("/plugins/message_debugger/turns?request_id=r-1&snapshot_type=pre_llm").json()["total"] == 3
        assert client.get("/plugins/message_debugger/turns?session_id=s-1&request_id=r-1").json()["total"] == 3

        entries = client.get("/plugins/message_debugger/llm-requests?request_id=r-1&limit=1").json()
        assert [entry["request_id"] for entry in entries["requests"]] == ["r-1_sub_ab12"]
        assert entries["total"] == 3
        assert client.get("/plugins/message_debugger/llm-requests?session_id=s-1&direction=response").json()["total"] == 1

    @pytest.mark.asyncio
    async def test_prune_compacts_the_file_after_a_clear(self, web_factory, db):
        """Pages freed before -- by a clear, by the automatic retention -- go back to the disk only through a
        VACUUM: the prune runs it although retention has nothing to do."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        for i in range(40):
            db.insert_llm_request(time.time() * 1000 + i, "request", payload={"messages": ["x" * 5000]})
        db.clear_all()
        assert db.free_pages() > 0, "fixture: the clear freed no pages"

        app = FastAPI()
        app.include_router(web_factory.get_web_router())
        result = TestClient(app).post("/plugins/message_debugger/prune?vacuum=true").json()

        assert result["stripped"] == result["turns_deleted"] == result["requests_deleted"] == 0
        assert result["vacuumed"] is True
        assert db.free_pages() == 0

    @pytest.mark.asyncio
    async def test_list_llm_requests(self, web_factory, db):
        """Test listing LLM requests."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        ts = time.time() * 1000
        db.insert_llm_request(ts, "request", provider="openai", model="gpt-4o")
        db.insert_llm_request(ts, "response", provider="openai", model="gpt-4o", duration_ms=500)

        app = FastAPI()
        router = web_factory.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/llm-requests")
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_get_stats_endpoint(self, web_factory, db):
        """Test getting statistics."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm", agent_name="a", total_tokens=100)
        db.insert_llm_request(ts, "response", provider="openai", duration_ms=300)

        app = FastAPI()
        router = web_factory.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/stats")
        assert response.status_code == 200
        result = response.json()
        assert result["total_turns"] == 1
        assert result["total_llm_requests"] == 1
        assert result["unique_session_count"] >= 0

    @pytest.mark.asyncio
    async def test_clear_all_endpoint(self, web_factory, db):
        """Test clearing all data."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        ts = time.time() * 1000
        db.insert_turn(ts, "pre_llm")
        db.insert_llm_request(ts, "request")

        app = FastAPI()
        router = web_factory.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.delete("/plugins/message_debugger/clear")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "cleared"
        assert result["turns_deleted"] == 1
        assert result["requests_deleted"] == 1


# ============================================================================
# Hybrid Plugin Tests
# ============================================================================

class TestMessageDebuggerHybridPlugin:
    """Test hybrid plugin integration."""

    def test_plugin_initialization(self, hybrid_plugin):
        """Test that hybrid plugin initializes correctly."""
        assert hybrid_plugin.name == "message_debugger"
        assert hasattr(hybrid_plugin, "hooks_plugin")
        assert hasattr(hybrid_plugin, "web_factory")
        assert hybrid_plugin._db is not None

    def test_get_hooks(self, hybrid_plugin):
        """Test that hooks are properly exposed."""
        hooks = hybrid_plugin.get_hooks()
        assert isinstance(hooks, list)
        assert len(hooks) == 4  # pre_llm, post_llm, pre_request, post_response

        hook_names = [h["name"] for h in hooks]
        assert "debugger_capture_pre_llm" in hook_names
        assert "debugger_capture_post_llm" in hook_names
        assert "debugger_capture_pre_request" in hook_names
        assert "debugger_capture_post_response" in hook_names

    def test_get_web_router(self, hybrid_plugin):
        """Test that web router is properly exposed."""
        router = hybrid_plugin.get_web_router()
        assert router is not None
        assert router.prefix == "/plugins/message_debugger"

    def test_db_shared_between_hooks_and_web(self, hybrid_plugin):
        """Test that hooks and web factory share the same DB."""
        assert hybrid_plugin.hooks_plugin.db is hybrid_plugin._db
        assert hybrid_plugin.web_factory.db is hybrid_plugin._db


# ============================================================================
# Integration Tests
# ============================================================================

class TestMessageDebuggerIntegration:
    """Test end-to-end integration scenarios."""

    @pytest.mark.asyncio
    async def test_pre_and_post_capture_workflow(self, hybrid_plugin, sample_messages):
        """Test full workflow: capture pre -> capture post -> verify via API."""
        # 1. Capture pre-LLM
        pre_context = HookContext(
            hook_type="pre_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages
        )

        await hybrid_plugin.hooks_plugin.debugger_capture_pre_llm(pre_context)

        # 2. Capture post-LLM (with response)
        post_context = HookContext(
            hook_type="post_llm_call",
            agent_name="test_agent",
            request_id="req_123",
            session_id="sess_456",
            messages=sample_messages + [ChatMessage(role="assistant", content="Response")],
            llm_response={"model": "gpt-4", "usage": {"total_tokens": 60}}
        )

        await hybrid_plugin.hooks_plugin.debugger_capture_post_llm(post_context)

        # 3. Verify both snapshots captured in DB (drain the async writer first)
        assert hybrid_plugin._db.flush(timeout=3)
        turns = hybrid_plugin._db.get_turns()
        assert len(turns) == 2
        types = {t["snapshot_type"] for t in turns}
        assert "pre_llm" in types
        assert "post_llm" in types

        # 4. Verify via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        router = hybrid_plugin.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/turns")
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_full_llm_request_response_flow(self, hybrid_plugin):
        """Test full LLM request/response capture via hooks and API."""
        # Capture request
        req_context = HookContext(
            hook_type="pre_llm_request",
            agent_name="coding_agent",
            request_id="req_42",
            session_id="sess_99",
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_request_url="https://api.openai.com/v1/chat/completions",
            llm_is_streaming=False,
            llm_request_payload={"messages": [{"role": "user", "content": "test"}]},
        )
        await hybrid_plugin.hooks_plugin.debugger_capture_pre_request(req_context)

        # Capture response
        resp_context = HookContext(
            hook_type="post_llm_response",
            agent_name="coding_agent",
            request_id="req_42",
            session_id="sess_99",
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_duration_ms=850.0,
            llm_finish_reason="stop",
            llm_usage={"prompt_tokens": 10, "completion_tokens": 25, "total_tokens": 35},
            llm_response_data={"id": "chatcmpl-abc", "choices": [{"message": {"content": "response"}}]},
        )
        await hybrid_plugin.hooks_plugin.debugger_capture_post_response(resp_context)
        # Captures are fire-and-forget: drain the writer before reading via the API.
        assert hybrid_plugin._db.flush(timeout=3)

        # Verify via API
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        router = hybrid_plugin.get_web_router()
        app.include_router(router)
        client = TestClient(app)

        response = client.get("/plugins/message_debugger/llm-requests")
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == 2

        # Check stats
        response = client.get("/plugins/message_debugger/stats")
        stats = response.json()
        assert stats["total_llm_requests"] == 2
        assert "openai" in stats["unique_providers"]
