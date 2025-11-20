"""Tests for user_id extraction and injection in sub-agent management."""
import pytest
from unittest.mock import MagicMock

from plugins.sub_agent_manager.manager import SubAgentManager
from agent_system.services.session_manager import SessionManager


@pytest.fixture
async def temp_session_storage(tmp_path):
    """Create temporary session storage with multi-user structure."""
    storage_path = tmp_path / "sessions"
    storage_path.mkdir()
    
    # Create user directories
    (storage_path / "admin").mkdir()
    (storage_path / "user2").mkdir()
    
    # Create sample session files
    import json
    
    admin_session = {
        "session_id": "admin_session_123",
        "user_id": "admin",
        "title": "Admin Session",
        "created_at": "2025-01-15T10:00:00Z",
        "updated_at": "2025-01-15T10:00:00Z",
        "agent_name": "basic_agent",
        "llm_profile": "default",
        "messages": []
    }
    
    user2_session = {
        "session_id": "user2_session_456",
        "user_id": "user2",
        "title": "User2 Session",
        "created_at": "2025-01-15T10:00:00Z",
        "updated_at": "2025-01-15T10:00:00Z",
        "agent_name": "basic_agent",
        "llm_profile": "default",
        "messages": []
    }
    
    with open(storage_path / "admin" / "admin_session_123.json", "w") as f:
        json.dump(admin_session, f)
    
    with open(storage_path / "user2" / "user2_session_456.json", "w") as f:
        json.dump(user2_session, f)
    
    return storage_path


@pytest.mark.asyncio
async def test_extract_user_id_from_session_file(temp_session_storage):
    """Test that user_id is correctly extracted from session file location."""
    # Create SessionManager with temp storage
    session_manager = SessionManager(storage_path=str(temp_session_storage))
    
    # Create mock session service
    session_service = MagicMock()
    session_service.session_manager = session_manager
    
    # Create mock registry
    registry = MagicMock()
    
    # Create SubAgentManager
    manager = SubAgentManager(session_service, registry)
    
    # Test extraction for admin session
    user_id_admin = manager._extract_user_id("admin_session_123")
    assert user_id_admin == "admin", f"Expected 'admin', got '{user_id_admin}'"
    
    # Test extraction for user2 session
    user_id_user2 = manager._extract_user_id("user2_session_456")
    assert user_id_user2 == "user2", f"Expected 'user2', got '{user_id_user2}'"


@pytest.mark.asyncio
async def test_extract_user_id_fallback_for_missing_session(temp_session_storage):
    """Test that _extract_user_id falls back to 'anonymous' for non-existent sessions."""
    session_manager = SessionManager(storage_path=str(temp_session_storage))
    session_service = MagicMock()
    session_service.session_manager = session_manager
    registry = MagicMock()
    
    manager = SubAgentManager(session_service, registry)
    
    # Non-existent session should fall back to 'anonymous' (NOT 'admin' for security)
    user_id = manager._extract_user_id("nonexistent_session_999")
    assert user_id == "anonymous"


@pytest.mark.asyncio
async def test_user_id_injection_in_tool_execution():
    """Test that user_id is injected into tool parameters during execution."""
    from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
    from agent_system.mcp.base import MCPRegistry
    
    # Create mock registry
    registry = MCPRegistry()
    
    # Create tool execution manager
    manager = ToolExecutionManager(registry)
    
    # Create mock server that captures injected params
    captured_params = {}
    
    class MockServer:
        async def call(self, tool_name, params):
            nonlocal captured_params
            captured_params = params.copy()
            return {"result": "success"}
    
    mock_server = MockServer()
    
    # Register mock server in registry
    registry.register("test_tool", mock_server)
    
    # Simulate setting user_id context (as done in execute_tools_streaming)
    manager._current_user_id = "test_user_123"
    manager._current_session_id = "test_session_456"
    
    # Execute single tool
    tool_calls = [{
        "id": "call_1",
        "function": {
            "name": "test_tool",
            "arguments": '{"arg1": "value1"}'
        }
    }]
    
    # Collect results from streaming
    async for item in manager.execute_tools_streaming(
        tool_calls=tool_calls,
        tool_name_mapping={"test_tool": "test_tool"},
        available_tools=["test_tool"],
        step=1,
        session_id="test_session_456",
        user_id="test_user_123"
    ):
        if item.get("type") == "complete":
            # Verification happens via captured_params
            pass
    
    # Verify user_id was injected into captured params
    assert "_user_id" in captured_params, f"user_id was not injected. Params: {captured_params}"
    assert captured_params["_user_id"] == "test_user_123"
    assert captured_params["_session_id"] == "test_session_456"
    assert captured_params["arg1"] == "value1"  # Original param preserved


@pytest.mark.asyncio
async def test_session_metadata_stored_on_restore():
    """Test that SessionTracker stores metadata including user_id when session is restored."""
    from agent_system.servers.agent.components.session_tracking import SessionTracker
    
    tracker = SessionTracker()
    
    # Simulate session restore (as done in SessionService)
    session_id = "test_session_789"
    user_id = "metadata_user"
    
    tracker.set_session_metadata(session_id, {
        "user_id": user_id,
        "agent_name": "basic_agent",
        "llm_profile": "default"
    })
    
    # Retrieve metadata
    metadata = tracker.get_session_metadata(session_id)
    
    assert metadata is not None
    assert metadata["user_id"] == user_id
    assert metadata["agent_name"] == "basic_agent"


@pytest.mark.asyncio
async def test_multi_user_session_isolation(temp_session_storage):
    """Test that sessions from different users are correctly isolated."""
    session_manager = SessionManager(storage_path=str(temp_session_storage))
    
    # Load admin session
    admin_session = await session_manager.load_session("admin", "admin_session_123")
    assert admin_session["user_id"] == "admin"
    
    # Load user2 session
    user2_session = await session_manager.load_session("user2", "user2_session_456")
    assert user2_session["user_id"] == "user2"
    
    # Verify admin cannot access user2's session
    with pytest.raises(Exception):  # Should raise SessionPermissionError
        await session_manager.load_session("admin", "user2_session_456")
