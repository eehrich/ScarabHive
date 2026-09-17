"""Tests for Sequential Thinking plugin hook duplicate injection prevention."""
import pytest
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage
from plugins.sequential_thinking.server import SequentialThinkingServer


@pytest.fixture
def server(tmp_path):
    """Create SequentialThinkingServer instance for testing."""
    from agent_system.config import AgentSystemConfig, ToolServerConfig
    
    config = AgentSystemConfig(data_dir=tmp_path)
    server_config = ToolServerConfig(
        name="sequential_thinking",
        plugin_config={
            "max_history_size": 100,
            "session_ttl_seconds": 3600
        }
    )
    
    return SequentialThinkingServer("sequential_thinking", config, server_config)


@pytest.mark.asyncio
async def test_hook_injects_reminder_once(server):
    """Test that thinking reminder is injected only once."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_001",
        session_id="test_session_123",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
    )
    
    # First injection - no active session
    result1 = await server.on_pre_llm_call(context)
    
    assert result1.success is True
    assert result1.modified is True
    
    # Count system messages with Sequential Thinking marker
    thinking_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## Sequential Thinking Tool Available" in msg.content
    ]
    assert len(thinking_messages) == 1, "Should have exactly one Sequential Thinking injection"
    
    # Second call (simulate multiple LLM calls)
    result2 = await server.on_pre_llm_call(context)
    
    assert result2.success is True
    assert result2.modified is True
    
    # Should still have exactly one injection
    thinking_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## Sequential Thinking Tool Available" in msg.content
    ]
    assert len(thinking_messages) == 1, "Should still have exactly one injection after second call"


@pytest.mark.asyncio
async def test_hook_replaces_reminder_with_active_session(server):
    """Test that reminder is replaced when a thinking session becomes active."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_002",
        session_id="test_agent_session",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Let's think about this")
        ]
    )
    
    # First call - no active session, injects reminder
    result1 = await server.on_pre_llm_call(context)
    assert result1.success is True
    
    # Verify reminder was injected
    reminder_found = any(
        msg.role == "system" and "## Sequential Thinking Tool Available" in msg.content
        for msg in context.messages
    )
    assert reminder_found, "Should have reminder when no active session"
    
    # Create a thinking session
    session_result = await server.execute({
        "thought": "First thought about the problem",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_session_id": "test_agent_session"
    })
    assert session_result["status"] == "success"
    
    # Second call - with active session, should replace reminder with session info
    result2 = await server.on_pre_llm_call(context)
    assert result2.success is True
    
    # Should have exactly one injection
    thinking_injections = [
        msg for msg in context.messages
        if msg.role == "system" and (
            "## Sequential Thinking Tool Available" in msg.content or
            "## Active Sequential Thinking Session" in msg.content
        )
    ]
    assert len(thinking_injections) == 1, "Should have exactly one Sequential Thinking injection"
    
    # Verify it now shows active session
    active_session_found = any(
        msg.role == "system" and "## Active Sequential Thinking Session" in msg.content
        for msg in context.messages
    )
    assert active_session_found, "Should show active session info when session exists"


@pytest.mark.asyncio
async def test_hook_prevents_multiple_injections_across_calls(server):
    """Test that multiple sequential hook calls don't accumulate injections."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_003",
        session_id="test_session_multi",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant.")
        ]
    )
    
    # Simulate 5 consecutive LLM calls
    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Thought {i}"))
        result = await server.on_pre_llm_call(context)
        assert result.success is True
    
    # Count all thinking-related injections
    thinking_messages = [
        msg for msg in context.messages
        if msg.role == "system" and (
            "## Sequential Thinking Tool Available" in msg.content or
            "## Active Sequential Thinking Session" in msg.content
        )
    ]
    
    assert len(thinking_messages) == 1, f"Should have exactly 1 injection after 5 calls, found {len(thinking_messages)}"


@pytest.mark.asyncio
async def test_hook_handles_both_reminder_markers(server):
    """Test that hook correctly removes old injections with either marker format."""
    # Create context with old injection (tagged with injected_by)
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_004",
        session_id="test_session_markers",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content="## Sequential Thinking Tool Available\n\nOld injection", injected_by="sequential_thinking"),
            ChatMessage(role="user", content="Hello")
        ]
    )
    
    # Hook should remove the old injection
    result = await server.on_pre_llm_call(context)
    assert result.success is True
    
    # Should have exactly one injection (old removed, new added)
    thinking_messages = [
        msg for msg in context.messages
        if msg.role == "system" and (
            "## Sequential Thinking Tool Available" in msg.content or
            "## Active Sequential Thinking Session" in msg.content
        )
    ]
    assert len(thinking_messages) == 1, "Should replace old injection"
    
    # Now test with active session marker
    await server.execute({
        "thought": "Testing marker replacement",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_session_id": "test_session_markers"
    })
    
    # Manually inject old active session marker (tagged with injected_by)
    context.messages.insert(1, ChatMessage(
        role="system",
        content="## Active Sequential Thinking Session\n\nOld session info",
        injected_by="sequential_thinking"
    ))
    
    result2 = await server.on_pre_llm_call(context)
    assert result2.success is True
    
    # Should still have exactly one injection
    thinking_messages = [
        msg for msg in context.messages
        if msg.role == "system" and (
            "## Sequential Thinking Tool Available" in msg.content or
            "## Active Sequential Thinking Session" in msg.content
        )
    ]
    assert len(thinking_messages) == 1, "Should handle both marker formats"
