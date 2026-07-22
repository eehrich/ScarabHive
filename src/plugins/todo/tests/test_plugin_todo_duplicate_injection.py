"""Tests for TODO plugin hook duplicate injection prevention."""
import pytest
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage
from plugins.todo.server import TodoServer


@pytest.fixture
def server(tmp_path):
    """Create TodoServer instance for testing."""
    from agent_system.config import AgentSystemConfig, MCPConfig
    
    config = AgentSystemConfig(data_dir=tmp_path)
    mcp_config = MCPConfig(
        name="todo",
        plugin_config={
            "storage_path": str(tmp_path / "todos"),
            "max_tasks": 20
        }
    )
    
    return TodoServer("todo", config, mcp_config)


@pytest.mark.asyncio
async def test_hook_injects_reminder_once(server):
    """Test that TODO reminder is injected only once."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_001",
        session_id="test_session_123",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
    )
    
    # First injection
    result1 = await server.on_pre_llm_call(context)
    
    assert result1.success is True
    assert result1.modified is True
    
    # Count system messages with TODO marker
    todo_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## TODO Tool Available" in msg.content
    ]
    assert len(todo_messages) == 1, "Should have exactly one TODO injection"
    
    # Second call (simulate multiple LLM calls)
    result2 = await server.on_pre_llm_call(context)
    
    assert result2.success is True
    assert result2.modified is True
    
    # Should still have exactly one TODO injection (old one removed, new one added)
    todo_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## TODO Tool Available" in msg.content
    ]
    assert len(todo_messages) == 1, "Should still have exactly one TODO injection after second call"


@pytest.mark.asyncio
async def test_hook_replaces_reminder_with_tasks(server):
    """Test that reminder is replaced when tasks are created."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_002",
        session_id="test_session_456",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Create a task")
        ]
    )
    
    # First call - no tasks yet, injects reminder
    result1 = await server.on_pre_llm_call(context)
    assert result1.success is True
    
    # Verify reminder was injected
    reminder_found = any(
        msg.role == "system" and "## TODO Tool Available" in msg.content and "Example:" in msg.content
        for msg in context.messages
    )
    assert reminder_found, "Should have reminder when no tasks"
    
    # Create a task
    await server.create_todo(
        title="Test task",
        description="Test description",
        priority="high",
        context={"session_id": "test_session_456"}
    )
    
    # Second call - with tasks, should replace reminder with task list
    result2 = await server.on_pre_llm_call(context)
    assert result2.success is True
    
    # Should still have exactly one TODO injection
    todo_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## TODO Tool Available" in msg.content
    ]
    assert len(todo_messages) == 1, "Should have exactly one TODO injection"
    
    # Verify it now shows tasks
    task_list_found = any(
        msg.role == "system" and "Current active tasks:" in msg.content
        for msg in context.messages
    )
    assert task_list_found, "Should show task list when tasks exist"


@pytest.mark.asyncio
async def test_hook_prevents_multiple_injections_across_calls(server):
    """Test that multiple sequential hook calls don't accumulate injections."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_003",
        session_id="test_session_789",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant.")
        ]
    )
    
    # Simulate 5 consecutive LLM calls (e.g., multi-turn conversation)
    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Message {i}"))
        result = await server.on_pre_llm_call(context)
        assert result.success is True
    
    # Count all TODO injections
    todo_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## TODO Tool Available" in msg.content
    ]
    
    assert len(todo_messages) == 1, f"Should have exactly 1 TODO injection after 5 calls, found {len(todo_messages)}"


@pytest.mark.asyncio
async def test_hook_insertion_position_after_system_prompt(server):
    """Test that TODO injection is inserted after first system message."""
    context = HookContext(
        hook_type="inject_todo_tasks",
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
    
    # Find TODO injection position
    todo_index = None
    for i, msg in enumerate(context.messages):
        if msg.role == "system" and "## TODO Tool Available" in msg.content:
            todo_index = i
            break
    
    assert todo_index is not None, "TODO injection should be present"
    assert todo_index == 1, f"TODO injection should be at index 1 (after main system prompt), found at {todo_index}"
    
    # Verify main system prompt is still first
    assert context.messages[0].content == "Main system prompt."
