"""Tests for Memory plugin hook duplicate injection prevention."""
import pytest
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage
from plugins.memory.server import MemoryServer


@pytest.fixture
def server(tmp_path):
    """Create MemoryServer instance for testing."""
    from agent_system.config import AgentSystemConfig, MCPConfig

    config = AgentSystemConfig(data_dir=tmp_path)
    mcp_config = MCPConfig(
        name="memory",
        plugin_config={
            "storage_path": str(tmp_path / "memories")
        }
    )

    return MemoryServer("memory", config, mcp_config)


@pytest.mark.asyncio
async def test_hook_injects_memories_once(server):
    """Test that memory list is injected only once."""
    # Create some memories first
    await server.execute({
        "operation": "store",
        "title": "Test Memory 1",
        "content": "Important information",
        "tags": ["test"],
        "_session_id": "test_session_123"
    })
    await server.execute({
        "operation": "store",
        "title": "Test Memory 2",
        "content": "More information",
        "tags": ["test"],
        "_session_id": "test_session_123"
    })

    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_001",
        session_id="test_session_123",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="What do you remember?")
        ]
    )

    # First injection
    result1 = await server.on_pre_llm_call(context)

    assert result1.success is True
    assert result1.modified is True

    # Count system messages with memory marker
    memory_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content
    ]
    assert len(memory_messages) == 1, "Should have exactly one memory injection"

    # Second call (simulate multiple LLM calls)
    result2 = await server.on_pre_llm_call(context)

    assert result2.success is True
    assert result2.modified is True

    # Should still have exactly one memory injection
    memory_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content
    ]
    assert len(memory_messages) == 1, "Should still have exactly one memory injection after second call"


@pytest.mark.asyncio
async def test_hook_prevents_multiple_injections_across_calls(server):
    """Test that multiple sequential hook calls don't accumulate memory injections."""
    # Create a memory
    await server.execute({
        "operation": "store",
        "title": "Persistent Memory",
        "content": "This should appear only once",
        "tags": ["test"],
        "_session_id": "test_session_multi"
    })

    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_002",
        session_id="test_session_multi",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant.")
        ]
    )

    # Simulate 5 consecutive LLM calls
    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Query {i}"))
        result = await server.on_pre_llm_call(context)
        assert result.success is True

    # Count all memory injections
    memory_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content
    ]

    assert len(memory_messages) == 1, f"Should have exactly 1 memory injection after 5 calls, found {len(memory_messages)}"


@pytest.mark.asyncio
async def test_hook_updates_memory_list_when_changed(server):
    """Test that memory list is updated when memories change."""
    import uuid
    unique_session = f"test_session_{uuid.uuid4().hex[:8]}"
    
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_003",
        session_id=unique_session,
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Tell me what you know")
        ]
    )

    # First call - no memories yet
    result1 = await server.on_pre_llm_call(context)
    assert result1.modified is False  # No memories to inject

    # Create a memory
    await server.execute({
        "operation": "store",
        "title": "New Memory",
        "content": "Newly stored information",
        "tags": ["new"],
        "_session_id": unique_session
    })

    # Second call - should inject the new memory
    result2 = await server.on_pre_llm_call(context)
    assert result2.modified is True

    # Verify memory is in the injection
    memory_content = None
    for msg in context.messages:
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content:
            memory_content = msg.content
            break

    assert memory_content is not None, "Should have memory injection"
    assert "New Memory" in memory_content, "Should contain the new memory"

    # Create another memory
    await server.execute({
        "operation": "store",
        "title": "Second Memory",
        "content": "Another piece of information",
        "tags": ["new"],
        "_session_id": unique_session
    })

    # Third call - should update with both memories
    result3 = await server.on_pre_llm_call(context)
    assert result3.modified is True

    # Should still have exactly one injection
    memory_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content
    ]
    assert len(memory_messages) == 1, "Should have exactly one memory injection"

    # Verify both memories are present
    memory_content = memory_messages[0].content
    assert "New Memory" in memory_content, "Should contain first memory"
    assert "Second Memory" in memory_content, "Should contain second memory"


@pytest.mark.asyncio
async def test_hook_insertion_position_after_system_prompt(server):
    """Test that memory injection is inserted after first system message."""
    # Create a memory
    await server.execute({
        "operation": "store",
        "title": "Position Test Memory",
        "content": "Testing injection position",
        "tags": ["position"],
        "_session_id": "test_session_pos"
    })

    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_004",
        session_id="test_session_pos",
        messages=[
            ChatMessage(role="system", content="Main system prompt."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
    )

    result = await server.on_pre_llm_call(context)
    assert result.success is True

    # Find memory injection position
    memory_index = None
    for i, msg in enumerate(context.messages):
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content:
            memory_index = i
            break

    assert memory_index is not None, "Memory injection should be present"
    assert memory_index == 1, f"Memory injection should be at index 1 (after main system prompt), found at {memory_index}"

    # Verify main system prompt is still first
    assert context.messages[0].content == "Main system prompt."


@pytest.mark.asyncio
async def test_hook_no_duplication_with_manual_injection(server):
    """Test that hook removes manually injected old memory lists."""
    # Create a memory
    await server.execute({
        "operation": "store",
        "title": "Test Memory",
        "content": "Content",
        "tags": ["test"],
        "_session_id": "test_session_manual"
    })

    # Manually inject old-style memory list (tagged with injected_by)
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="test_req_005",
        session_id="test_session_manual",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content="## AVAILABLE MEMORIES\n\n- Old memory list", injected_by="memory"),
            ChatMessage(role="user", content="Question")
        ]
    )

    # Hook should remove old injection and add new one
    result = await server.on_pre_llm_call(context)
    assert result.success is True

    # Should have exactly one memory injection
    memory_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "AVAILABLE MEMORIES" in msg.content
    ]
    assert len(memory_messages) == 1, "Should replace old manual injection"
    assert "Old memory list" not in memory_messages[0].content, "Old content should be removed"
    assert "Test Memory" in memory_messages[0].content, "New content should be present"
