"""Tests for cognitive_stack plugin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config import AgentSystemConfig, MCPConfig
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage

from plugins.cognitive_stack.server import (
    CognitiveStack,
    CognitiveStackServer,
    StackFrame,
)


@pytest.fixture
def config():
    """Create test config."""
    return AgentSystemConfig()


@pytest.fixture
def mcp_config():
    """Create test MCP config."""
    cfg = MagicMock(spec=MCPConfig)
    cfg.max_depth = 10
    cfg.session_ttl_seconds = 60
    cfg.max_frames_in_prompt = 3
    return cfg


@pytest.fixture
def server(config, mcp_config):
    """Create test server."""
    return CognitiveStackServer("test_stack", config, mcp_config)


@pytest.fixture
def mock_status():
    """Create mock status object."""
    status = AsyncMock()
    status.error = AsyncMock()
    status.end = AsyncMock()
    return status


# =============================================================================
# Stack Creation & Basic Operations
# =============================================================================


def test_stack_frame_creation():
    """Test StackFrame dataclass."""
    frame = StackFrame(
        frame_id="abc123",
        context="Testing requirements analysis",
        timestamp=datetime.now(),
        data={"feature": "X", "step": 1}
    )

    assert frame.frame_id == "abc123"
    assert frame.context == "Testing requirements analysis"
    assert frame.data["feature"] == "X"


def test_cognitive_stack_creation():
    """Test CognitiveStack dataclass."""
    stack = CognitiveStack(
        stack_id="stack_123",
        created_at=datetime.now(),
        last_accessed=datetime.now(),
        max_depth=10
    )

    assert stack.stack_id == "stack_123"
    assert len(stack.frames) == 0
    assert stack.max_depth == 10


@pytest.mark.asyncio
async def test_push_first_frame(server, mock_status):
    """Test pushing first frame creates new stack."""
    params = {
        "operation": "push",
        "context": "Analyzing user requirements",
        "data": {"task": "requirements", "priority": "high"},
        "_status": mock_status
    }

    result = await server.cognitive_stack( params)

    assert result["status"] == "success"
    assert "stack_id" in result
    assert result["depth"] == 1
    assert result["max_depth"] == 10
    assert "frame_id" in result

    # Check status was called
    mock_status.end.assert_called_once()


@pytest.mark.asyncio
async def test_push_multiple_frames(server, mock_status):
    """Test pushing multiple frames onto same stack."""
    # First push
    result1 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Main task: Feature X",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    # Second push
    result2 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Sub-task: Analyze dependencies",
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result2["depth"] == 2
    assert result2["stack_id"] == stack_id


@pytest.mark.asyncio
async def test_push_validates_empty_context(server, mock_status):
    """Test push rejects empty context."""
    result = await server.cognitive_stack( {
        "operation": "push",
        "context": "   ",  # Empty/whitespace
        "_status": mock_status
    })

    assert result["status"] == "error"
    assert "empty" in result["error"].lower()
    mock_status.error.assert_called_once()


@pytest.mark.asyncio
async def test_push_enforces_max_depth(server, mock_status):
    """Test push enforces max depth limit."""
    # Fill stack to max depth
    result1 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    for i in range(2, 11):  # Push 9 more (total 10)
        await server.cognitive_stack( {
            "operation": "push",
            "context": f"Frame {i}",
            "stack_id": stack_id,
            "_status": mock_status
        })

    # Try pushing 11th frame (should fail)
    result_fail = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 11",
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result_fail["status"] == "error"
    assert "Maximum stack depth" in result_fail["error"]


# =============================================================================
# Pop Operation
# =============================================================================


@pytest.mark.asyncio
async def test_pop_frame(server, mock_status):
    """Test popping frame from stack."""
    # Push two frames
    result1 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 1",
        "data": {"id": 1},
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 2",
        "data": {"id": 2},
        "stack_id": stack_id,
        "_status": mock_status
    })

    # Pop top frame
    result = await server.cognitive_stack( {
        "operation": "pop",
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert result["frame"]["context"] == "Frame 2"
    assert result["frame"]["data"]["id"] == 2
    assert result["remaining_depth"] == 1


@pytest.mark.asyncio
async def test_pop_empty_stack(server, mock_status):
    """Test popping from empty stack returns error."""
    # Create empty stack
    result1 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    # Pop the only frame
    await server.cognitive_stack( {"operation": "pop", "stack_id": stack_id, "_status": mock_status})

    # Try popping again (empty)
    result = await server.cognitive_stack( {
        "operation": "pop",
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result["status"] == "error"
    assert "empty" in result["error"].lower()


@pytest.mark.asyncio
async def test_pop_nonexistent_stack(server, mock_status):
    """Test popping from non-existent stack."""
    result = await server.cognitive_stack( {
        "operation": "pop",
        "stack_id": "nonexistent",
        "_status": mock_status
    })

    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


# =============================================================================
# Peek Operation
# =============================================================================


@pytest.mark.asyncio
async def test_peek_top_frame(server, mock_status):
    """Test peeking at top frame without removing."""
    # Push frames
    result1 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 2",
        "stack_id": stack_id,
        "_status": mock_status
    })

    # Peek
    result = await server.cognitive_stack( {
        "operation": "peek",
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert len(result["frames"]) == 1
    assert result["frames"][0]["context"] == "Frame 2"
    assert result["total_depth"] == 2  # Should still have 2 frames


@pytest.mark.asyncio
async def test_peek_multiple_frames(server, mock_status):
    """Test peeking at multiple frames."""
    # Push 3 frames
    result1 = await server.push({
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    await server.push({
        "context": "Frame 2",
        "stack_id": stack_id,
        "_status": mock_status
    })
    await server.push({
        "context": "Frame 3",
        "stack_id": stack_id,
        "_status": mock_status
    })

    # Peek top 2
    result = await server.peek({
        "stack_id": stack_id,
        "depth": 2,
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert len(result["frames"]) == 2
    assert result["frames"][0]["context"] == "Frame 2"
    assert result["frames"][1]["context"] == "Frame 3"


@pytest.mark.asyncio
async def test_peek_empty_stack(server, mock_status):
    """Test peeking at empty stack."""
    result1 = await server.push({
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    # Pop to empty
    await server.pop({"stack_id": stack_id, "_status": mock_status})

    # Peek
    result = await server.peek({
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert result["frames"] == []
    assert result["depth"] == 0


# =============================================================================
# List Operation
# =============================================================================


@pytest.mark.asyncio
async def test_list_all_frames(server, mock_status):
    """Test listing all frames in stack."""
    # Push 3 frames
    result1 = await server.push({
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    await server.push({
        "context": "Frame 2",
        "stack_id": stack_id,
        "_status": mock_status
    })
    await server.push({
        "context": "Frame 3",
        "stack_id": stack_id,
        "_status": mock_status
    })

    # List
    result = await server.list_frames({
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert len(result["frames"]) == 3
    assert result["depth"] == 3
    assert result["frames"][0]["context"] == "Frame 1"
    assert result["frames"][2]["context"] == "Frame 3"


# =============================================================================
# Clear Operation
# =============================================================================


@pytest.mark.asyncio
async def test_clear_specific_stack(server, mock_status):
    """Test clearing specific stack."""
    # Push frames
    result1 = await server.push({
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    await server.push({
        "context": "Frame 2",
        "stack_id": stack_id,
        "_status": mock_status
    })

    # Clear
    result = await server.clear({
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert result["cleared_frames"] == 2

    # Verify stack is empty
    peek_result = await server.peek({
        "stack_id": stack_id,
        "_status": mock_status
    })
    assert peek_result["depth"] == 0


@pytest.mark.asyncio
async def test_clear_all_stacks(server, mock_status):
    """Test clearing all stacks."""
    # Create 2 stacks
    await server.push({
        "context": "Stack 1 Frame 1",
        "_status": mock_status
    })

    await server.push({
        "context": "Stack 2 Frame 1",
        "_status": mock_status
    })

    # Clear all (no stack_id)
    result = await server.clear({
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert result["cleared_stacks"] == 2
    assert result["cleared_frames"] == 2


# =============================================================================
# Session Management
# =============================================================================


@pytest.mark.asyncio
async def test_agent_session_mapping(server, mock_status):
    """Test agent session ID mapping to stack ID."""
    # Push with agent session ID
    result = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 1",
        "_status": mock_status,
        "_session_id": "agent_session_123"
    })
    stack_id = result["stack_id"]

    # Check mapping
    assert server._agent_session_mapping["agent_session_123"] == stack_id

    # Push again with same agent session (should reuse stack)
    result2 = await server.cognitive_stack( {
        "operation": "push",
        "context": "Frame 2",
        "stack_id": stack_id,  # Explicitly provide stack_id
        "_status": mock_status,
        "_session_id": "agent_session_123"
    })

    assert result2["stack_id"] == stack_id
    assert result2["depth"] == 2


def test_cleanup_old_stacks(server):
    """Test TTL-based stack cleanup."""
    # Create old stack
    old_stack = CognitiveStack(
        stack_id="old_stack",
        created_at=datetime.now() - timedelta(seconds=120),
        last_accessed=datetime.now() - timedelta(seconds=120)
    )
    server._stacks["old_stack"] = old_stack

    # Create recent stack
    recent_stack = CognitiveStack(
        stack_id="recent_stack",
        created_at=datetime.now(),
        last_accessed=datetime.now()
    )
    server._stacks["recent_stack"] = recent_stack

    # Cleanup
    server._cleanup_old_stacks()

    # Old should be gone, recent should remain
    assert "old_stack" not in server._stacks
    assert "recent_stack" in server._stacks


# =============================================================================
# Hook: Pre-LLM Call Injection
# =============================================================================


@pytest.mark.asyncio
async def test_hook_no_active_stack(server):
    """Test hook injects reminder when no active stack."""
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_123",
        messages=[
            ChatMessage(role="system", content="You are an assistant"),
            ChatMessage(role="user", content="Help me")
        ],
        session_id="test_session",
        agent=MagicMock()
    )

    result = await server.on_pre_llm_call(context)

    assert result.success
    assert result.modified

    # Should have injected reminder
    injected = [m for m in result.context.messages if "Cognitive Stack Tool Available" in m.content]
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_hook_with_active_stack(server, mock_status):
    """Test hook injects active stack info."""
    # Create stack
    result = await server.push({
        "context": "Main task: Feature X",
        "data": {"feature": "X"},
        "_status": mock_status,
        "_session_id": "test_session"
    })
    stack_id = result["stack_id"]

    await server.push({
        "context": "Sub-task: Analyze dependencies",
        "stack_id": stack_id,
        "_status": mock_status,
        "_session_id": "test_session"
    })

    # Call hook
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_123",
        messages=[
            ChatMessage(role="system", content="You are an assistant"),
            ChatMessage(role="user", content="Help me")
        ],
        session_id="test_session",
        agent=MagicMock()
    )

    result = await server.on_pre_llm_call(context)

    assert result.success
    assert result.modified

    # Should have injected stack info
    injected = [m for m in result.context.messages if "Active Cognitive Stack" in m.content]
    assert len(injected) == 1
    assert "Depth**: 2" in injected[0].content


@pytest.mark.asyncio
async def test_hook_removes_old_injection(server, mock_status):
    """Test hook removes previous injection before adding new one."""
    # Create stack
    result = await server.push({
        "context": "Task 1",
        "_status": mock_status,
        "_session_id": "test_session"
    })

    # Initial messages with old injection
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_123",
        messages=[
            ChatMessage(role="system", content="You are an assistant"),
            ChatMessage(role="system", content="## Active Cognitive Stack\nOld injection"),
            ChatMessage(role="user", content="Help me")
        ],
        session_id="test_session",
        agent=MagicMock()
    )

    result = await server.on_pre_llm_call(context)

    # Should have only ONE injection (old removed, new added)
    injections = [m for m in result.context.messages if "Cognitive Stack" in m.content]
    assert len(injections) == 1


@pytest.mark.asyncio
async def test_hook_no_session_id(server):
    """Test hook skips if no session ID."""
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_123",
        messages=[ChatMessage(role="user", content="Help")],
        session_id=None,
        agent=MagicMock()
    )

    result = await server.on_pre_llm_call(context)

    assert result.success
    assert not result.modified


# =============================================================================
# Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_push_same_stack(server, mock_status):
    """Test concurrent pushes to same stack."""
    result1 = await server.push({
        "context": "Frame 1",
        "_status": mock_status
    })
    stack_id = result1["stack_id"]

    # Concurrent pushes
    tasks = [
        server.push({
            "context": f"Frame {i}",
            "stack_id": stack_id,
            "_status": mock_status
        })
        for i in range(2, 6)
    ]

    await asyncio.gather(*tasks)

    # Should have 5 frames total
    list_result = await server.list_frames({
        "stack_id": stack_id,
        "_status": mock_status
    })

    assert list_result["depth"] == 5


@pytest.mark.asyncio
async def test_data_persistence_across_operations(server, mock_status):
    """Test that frame data persists through peek/list operations."""
    result = await server.push({
        "context": "Task with data",
        "data": {"key": "value", "nested": {"a": 1}},
        "_status": mock_status
    })
    stack_id = result["stack_id"]

    # Peek should show data
    peek_result = await server.peek({
        "stack_id": stack_id,
        "_status": mock_status
    })
    assert peek_result["frames"][0]["data"]["key"] == "value"

    # List should show data
    list_result = await server.list_frames({
        "stack_id": stack_id,
        "_status": mock_status
    })
    assert list_result["frames"][0]["data"]["nested"]["a"] == 1

    # Pop should return data
    pop_result = await server.pop({
        "stack_id": stack_id,
        "_status": mock_status
    })
    assert pop_result["frame"]["data"]["key"] == "value"
