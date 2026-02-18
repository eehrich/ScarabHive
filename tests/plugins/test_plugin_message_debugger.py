"""Tests for message_debugger plugin.

Tests the database layer, hook capture, API endpoints, and hybrid plugin integration
using the SQLite-backed storage system.
"""
from __future__ import annotations

import pytest
import time
from pathlib import Path

from agent_system.hooks import HookContext
from agent_system.llm.models import ChatMessage
from agent_system.config.models import AgentSystemConfig, MCPConfig


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def plugin_dir():
    """Get path to message_debugger plugin directory."""
    return Path(__file__).parent.parent.parent / "src" / "plugins" / "message_debugger"


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

    def test_clear_older_than(self, db):
        """Test clearing old data."""
        old_ts = (time.time() - 3700) * 1000  # >1 hour ago
        recent_ts = time.time() * 1000

        db.insert_turn(old_ts, "pre_llm", agent_name="old")
        db.insert_turn(recent_ts, "pre_llm", agent_name="new")

        result = db.clear_older_than(hours=1)
        assert result["turns_deleted"] == 1
        assert db.count_turns() == 1
        remaining = db.get_turns()
        assert remaining[0]["agent_name"] == "new"

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
        turns = db.get_turns()
        assert len(turns) == 1
        assert turns[0]["snapshot_type"] == "post_llm"
        assert turns[0]["llm_response_json"] is not None
        assert turns[0]["llm_response_json"]["model"] == "gpt-4"

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
        turns = db.get_turns()
        turn_detail = db.get_turn(turns[0]["id"])
        messages = turn_detail["messages_json"]

        tool_call_msg = next(m for m in messages if m.get("tool_calls"))
        assert tool_call_msg["tool_call_count"] == 1
        assert tool_call_msg["tool_calls"][0]["function"]["name"] == "weather_forecast"

        tool_result_msg = next(m for m in messages if m.get("is_tool_result"))
        assert tool_result_msg["tool_call_id"] == "call_123"

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
        reqs = db.get_llm_requests(direction="response")
        assert len(reqs) == 1
        assert reqs[0]["duration_ms"] == pytest.approx(750.5)
        assert reqs[0]["finish_reason"] == "stop"
        assert reqs[0]["usage_json"]["prompt_tokens"] == 10

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

        # 3. Verify both snapshots captured in DB
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
