"""Tests for AgentService."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import asyncio

from agent_system.services.agent_service import AgentService
from agent_system.config.models import AgentSystemConfig, AgentConfig


# ===== Fixtures =====

@pytest.fixture
def agent_config():
    """Provide test agent config."""
    return AgentConfig(
        model="gpt-4",
        temperature=0.7,
        max_tokens=1000,
        system_prompt="Test system prompt"
    )


@pytest.fixture
def config(agent_config):
    """Provide test AgentSystemConfig."""
    return AgentSystemConfig(
        agent=agent_config,
        plugin_directory=Path("./plugins")
    )


@pytest.fixture
def mock_agent():
    """Provide a mock agent with common methods."""
    agent = MagicMock()
    agent._sessions = {}
    agent._request_lock = asyncio.Lock()
    agent.run_events = AsyncMock()
    agent.append_to_session = AsyncMock()
    agent.optimize_context = AsyncMock()
    agent.cancel_request = AsyncMock()
    return agent


@pytest.fixture
def agent_service(mock_agent, config):
    """Provide AgentService instance with mocked dependencies."""
    return AgentService(mock_agent, config)


# ===== Initialization Tests =====

def test_init_success(mock_agent, config):
    """Test AgentService initialization."""
    service = AgentService(mock_agent, config)
    
    assert service._agent is mock_agent
    assert service._config is config


# ===== Task Execution Tests (Streaming) =====

@pytest.mark.asyncio
async def test_execute_task_success_streaming(agent_service, mock_agent):
    """Test successful task execution with streaming."""
    # Mock streaming events
    events = [
        {"type": "step", "data": {"step": 1}},
        {"type": "thought", "data": {"thought": "thinking..."}},
        {"type": "result", "data": {"result": "Final answer"}},
        {"type": "end"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    # Execute task
    result_events = []
    async for event in agent_service.execute_task("Test task"):
        result_events.append(event)
    
    assert len(result_events) == 4
    assert result_events[0]["type"] == "step"
    assert result_events[2]["type"] == "result"
    assert result_events[3]["type"] == "end"


@pytest.mark.asyncio
async def test_execute_task_with_session_id(agent_service, mock_agent):
    """Test task execution with session ID."""
    events = [
        {"type": "result", "data": {"result": "Answer"}},
        {"type": "end"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    result_events = []
    async for event in agent_service.execute_task("Task", session_id="session123"):
        result_events.append(event)
    
    assert len(result_events) == 2


@pytest.mark.asyncio
async def test_execute_task_empty_raises_error(agent_service):
    """Test that empty task yields error event."""
    result_events = []
    async for event in agent_service.execute_task(""):
        result_events.append(event)
    
    assert len(result_events) == 1
    assert result_events[0]["type"] == "error"
    assert "cannot be empty" in result_events[0]["message"].lower()


@pytest.mark.asyncio
async def test_execute_task_exception_handling(agent_service, mock_agent):
    """Test exception handling during task execution."""
    async def mock_run_events_error(*args, **kwargs):
        raise RuntimeError("Test error")
        yield  # Make it a generator
    
    mock_agent.run_events = mock_run_events_error
    
    result_events = []
    async for event in agent_service.execute_task("Task"):
        result_events.append(event)
    
    assert len(result_events) == 1
    assert result_events[0]["type"] == "error"
    assert "Test error" in result_events[0]["message"]


@pytest.mark.asyncio
async def test_execute_task_with_images(agent_service, mock_agent):
    """Test task execution with image input (multimodal)."""
    events = [
        {"type": "result", "data": {"result": "Image processed"}},
        {"type": "end"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    # Mock create_multimodal_message
    with patch.object(agent_service, '_create_multimodal_message', new_callable=AsyncMock) as mock_create:
        mock_create.return_value = "multimodal_message"
        
        images = [b"fake_image_bytes"]
        result_events = []
        async for event in agent_service.execute_task("Describe image", images=images):
            result_events.append(event)
        
        # Verify multimodal message creation was called
        mock_create.assert_called_once()
        assert len(result_events) == 2


# ===== Task Execution Tests (Collect Result) =====

@pytest.mark.asyncio
async def test_execute_task_collect_result_success(agent_service, mock_agent):
    """Test collecting final result from task execution."""
    events = [
        {"type": "step", "data": {"step": 1}},
        {"type": "result", "data": {"result": "Final answer"}},
        {"type": "end"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    result = await agent_service.execute_task_collect_result("Task")
    
    assert result["status"] == "success"
    assert result["result"] == "Final answer"
    assert "request_id" in result
    assert len(result["steps"]) == 1


@pytest.mark.asyncio
async def test_execute_task_collect_result_error(agent_service, mock_agent):
    """Test collecting result when task fails."""
    events = [
        {"type": "error", "message": "Task failed"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    result = await agent_service.execute_task_collect_result("Task")
    
    assert result["status"] == "error"
    assert "Task failed" in result["error"]


@pytest.mark.asyncio
async def test_execute_task_collect_result_no_result(agent_service, mock_agent):
    """Test collecting result when no result event is emitted."""
    events = [
        {"type": "step", "data": {"step": 1}},
        {"type": "end"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    result = await agent_service.execute_task_collect_result("Task")
    
    assert result["status"] == "no_result"
    assert result["result"] == ""


# ===== Session Management Tests =====

@pytest.mark.asyncio
async def test_create_session_success(agent_service, mock_agent):
    """Test creating a new session."""
    session_id = await agent_service.create_session()
    
    assert isinstance(session_id, str)
    assert len(session_id) == 8  # short UUID
    assert session_id in mock_agent._sessions
    assert mock_agent._sessions[session_id] == []


@pytest.mark.asyncio
async def test_create_session_generates_unique_ids(agent_service):
    """Test that multiple sessions get unique IDs."""
    session_id1 = await agent_service.create_session()
    session_id2 = await agent_service.create_session()
    
    assert session_id1 != session_id2


@pytest.mark.asyncio
async def test_get_session_success(agent_service, mock_agent):
    """Test retrieving an existing session."""
    # Create session manually
    session_id = "test123"
    messages = [{"role": "user", "content": "Hello"}]
    mock_agent._sessions[session_id] = messages
    
    result = await agent_service.get_session(session_id)
    
    assert result == messages


@pytest.mark.asyncio
async def test_get_session_not_found(agent_service):
    """Test retrieving non-existent session returns None."""
    result = await agent_service.get_session("nonexistent")
    
    assert result is None


@pytest.mark.asyncio
async def test_delete_session_success(agent_service, mock_agent):
    """Test deleting an existing session."""
    # Create session manually
    session_id = "test123"
    mock_agent._sessions[session_id] = []
    
    success = await agent_service.delete_session(session_id)
    
    assert success is True
    assert session_id not in mock_agent._sessions


@pytest.mark.asyncio
async def test_delete_session_not_found(agent_service):
    """Test deleting non-existent session returns False."""
    success = await agent_service.delete_session("nonexistent")
    
    assert success is False


@pytest.mark.asyncio
async def test_append_to_session_success(agent_service, mock_agent):
    """Test appending a message to session."""
    mock_agent.append_to_session.return_value = True
    
    success = await agent_service.append_to_session("session123", "Hello", role="user")
    
    assert success is True
    mock_agent.append_to_session.assert_called_once_with("session123", "Hello")


@pytest.mark.asyncio
async def test_append_to_session_not_found(agent_service, mock_agent):
    """Test appending to non-existent session fails."""
    mock_agent.append_to_session.return_value = False
    
    success = await agent_service.append_to_session("nonexistent", "Hello")
    
    assert success is False


@pytest.mark.asyncio
async def test_list_sessions_empty(agent_service):
    """Test listing sessions when none exist."""
    sessions = await agent_service.list_sessions()
    
    assert sessions == []


@pytest.mark.asyncio
async def test_list_sessions_multiple(agent_service, mock_agent):
    """Test listing multiple sessions."""
    mock_agent._sessions = {
        "session1": [{"content": "msg1"}],
        "session2": [{"content": "msg2"}, {"content": "msg3"}]
    }
    
    sessions = await agent_service.list_sessions()
    
    assert len(sessions) == 2
    assert {"session_id": "session1", "message_count": 1} in sessions
    assert {"session_id": "session2", "message_count": 2} in sessions


# ===== Session Optimization Tests =====

@pytest.mark.asyncio
async def test_optimize_session_success(agent_service, mock_agent):
    """Test successful session optimization."""
    # Setup session with messages
    session_id = "test123"
    mock_agent._sessions[session_id] = [{"content": f"msg{i}"} for i in range(10)]
    
    # Mock optimization to reduce messages
    async def mock_optimize(*args):
        mock_agent._sessions[session_id] = mock_agent._sessions[session_id][:5]
    
    mock_agent.optimize_context = mock_optimize
    
    result = await agent_service.optimize_session(session_id)
    
    assert result["success"] is True
    assert result["original_messages"] == 10
    assert result["optimized_messages"] == 5
    assert result["reduction_percent"] == 50.0


@pytest.mark.asyncio
async def test_optimize_session_not_found(agent_service):
    """Test optimizing non-existent session."""
    result = await agent_service.optimize_session("nonexistent")
    
    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_optimize_session_not_supported(agent_service, mock_agent):
    """Test optimization when agent doesn't support it."""
    session_id = "test123"
    mock_agent._sessions[session_id] = []
    delattr(mock_agent, 'optimize_context')
    
    result = await agent_service.optimize_session(session_id)
    
    assert result["success"] is False
    assert "not supported" in result["error"].lower()


@pytest.mark.asyncio
async def test_optimize_session_exception(agent_service, mock_agent):
    """Test optimization exception handling."""
    session_id = "test123"
    mock_agent._sessions[session_id] = []
    mock_agent.optimize_context.side_effect = RuntimeError("Optimization failed")
    
    result = await agent_service.optimize_session(session_id)
    
    assert result["success"] is False
    assert "Optimization failed" in result["error"]


# ===== Request Cancellation Tests =====

@pytest.mark.asyncio
async def test_cancel_request_success(agent_service, mock_agent):
    """Test cancelling an ongoing request."""
    mock_agent.cancel_request.return_value = True
    
    success = await agent_service.cancel_request("req123")
    
    assert success is True
    mock_agent.cancel_request.assert_called_once_with("req123")


@pytest.mark.asyncio
async def test_cancel_request_not_found(agent_service, mock_agent):
    """Test cancelling non-existent request."""
    mock_agent.cancel_request.return_value = False
    
    success = await agent_service.cancel_request("nonexistent")
    
    assert success is False


@pytest.mark.asyncio
async def test_cancel_request_not_supported(agent_service, mock_agent):
    """Test cancellation when agent doesn't support it."""
    delattr(mock_agent, 'cancel_request')
    
    success = await agent_service.cancel_request("req123")
    
    assert success is False


# ===== Helper Method Tests =====

def test_generate_request_id():
    """Test request ID generation."""
    request_id = AgentService._generate_request_id()
    
    assert isinstance(request_id, str)
    assert len(request_id) == 8


def test_generate_session_id():
    """Test session ID generation."""
    session_id = AgentService._generate_session_id()
    
    assert isinstance(session_id, str)
    assert len(session_id) == 8


def test_generate_unique_ids():
    """Test that generated IDs are unique."""
    ids = {AgentService._generate_request_id() for _ in range(100)}
    assert len(ids) == 100  # All unique


# ===== Edge Cases =====

@pytest.mark.asyncio
async def test_concurrent_session_access(agent_service, mock_agent):
    """Test concurrent access to sessions is safe."""
    async def create_and_access():
        session_id = await agent_service.create_session()
        await agent_service.get_session(session_id)
        await agent_service.delete_session(session_id)
    
    # Run concurrently
    await asyncio.gather(*[create_and_access() for _ in range(10)])
    
    # All should complete without errors
    assert len(mock_agent._sessions) == 0  # All cleaned up


@pytest.mark.asyncio
async def test_execute_task_with_request_id(agent_service, mock_agent):
    """Test task execution with custom request ID."""
    events = [
        {"type": "result", "data": {"result": "Answer"}},
        {"type": "end"}
    ]
    
    async def mock_run_events(*args, **kwargs):
        # Verify request_id passed
        assert kwargs.get("request_id") == "custom_req_id"
        for event in events:
            yield event
    
    mock_agent.run_events = mock_run_events
    
    result_events = []
    async for event in agent_service.execute_task("Task", request_id="custom_req_id"):
        result_events.append(event)
    
    assert len(result_events) == 2
