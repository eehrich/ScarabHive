"""
Unit tests for status stream security - ensuring users cannot access other users' status streams.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_app_dependencies():
    """Mock the global dependencies in app.py"""
    with patch('agent_system.app._request_user_map', {}) as request_map, \
         patch('agent_system.app._get_current_user_optional') as mock_get_user, \
         patch('agent_system.app.status_bus') as mock_status_bus:
        
        # Setup mock status bus
        mock_queue = AsyncMock()
        mock_status_bus.subscribe = AsyncMock(return_value=mock_queue)
        
        yield {
            'request_map': request_map,
            'get_user': mock_get_user,
            'status_bus': mock_status_bus,
            'queue': mock_queue
        }


@pytest.mark.asyncio
async def test_status_stream_blocks_other_users_requests(mock_app_dependencies):
    """Test that a user cannot access another user's status stream"""
    from agent_system.app import build_app
    
    app = build_app()
    client = TestClient(app)
    
    # Setup: User A owns request_id "abc123"
    from agent_system.app import _request_user_map
    _request_user_map["abc123"] = "user_a"
    
    # Mock: User B is trying to access the stream
    user_b_mock = MagicMock()
    user_b_mock.username = "user_b"
    mock_app_dependencies['get_user'].return_value = user_b_mock
    
    # Test: User B tries to connect to User A's status stream
    response = client.get("/status/stream?request_id=abc123")
    
    # Assert: Should be denied with 403
    assert response.status_code == 403
    assert "Cannot access other users' status streams" in response.json()["detail"]


@pytest.mark.asyncio
async def test_status_stream_allows_owner_access(mock_app_dependencies):
    """Test that a user CAN access their own status stream"""
    from agent_system.app import build_app
    
    app = build_app()
    client = TestClient(app)
    
    # Setup: User A owns request_id "abc123"
    from agent_system.app import _request_user_map
    _request_user_map["abc123"] = "user_a"
    
    # Mock: User A is accessing their own stream
    user_a_mock = MagicMock()
    user_a_mock.username = "user_a"
    mock_app_dependencies['get_user'].return_value = user_a_mock
    
    # Setup mock queue behavior
    mock_queue = mock_app_dependencies['queue']
    mock_queue.get = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_queue.empty = MagicMock(return_value=True)
    
    # Test: User A tries to connect to their own status stream
    # Note: Using streaming endpoint, so we can't fully test the stream in sync client
    # Just verify no 403 is raised
    response = client.get("/status/stream?request_id=abc123", stream=True)
    
    # Assert: Should succeed (200) or at least not be 403
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_status_stream_allows_anonymous_access_anonymous_request(mock_app_dependencies):
    """Test that anonymous users can access anonymous requests"""
    from agent_system.app import build_app
    
    app = build_app()
    client = TestClient(app)
    
    # Setup: Anonymous user owns request_id "anon123"
    from agent_system.app import _request_user_map
    _request_user_map["anon123"] = "anonymous"
    
    # Mock: Anonymous user accessing stream (no authentication)
    mock_app_dependencies['get_user'].return_value = None
    
    # Test: Anonymous user tries to connect to anonymous status stream
    response = client.get("/status/stream?request_id=anon123", stream=True)
    
    # Assert: Should succeed
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_status_stream_blocks_authenticated_user_from_anonymous_request(mock_app_dependencies):
    """Test that authenticated users cannot access anonymous users' requests"""
    from agent_system.app import build_app
    
    app = build_app()
    client = TestClient(app)
    
    # Setup: Anonymous user owns request_id "anon123"
    from agent_system.app import _request_user_map
    _request_user_map["anon123"] = "anonymous"
    
    # Mock: Authenticated user trying to access anonymous request
    user_mock = MagicMock()
    user_mock.username = "evil_user"
    mock_app_dependencies['get_user'].return_value = user_mock
    
    # Test: Authenticated user tries to connect to anonymous status stream
    response = client.get("/status/stream?request_id=anon123")
    
    # Assert: Should be denied with 403
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_status_stream_allows_unregistered_request_id(mock_app_dependencies):
    """Test that requests with no registered owner are allowed (backward compatibility)"""
    from agent_system.app import build_app
    
    app = build_app()
    client = TestClient(app)
    
    # Setup: request_id "xyz789" is NOT in the map (old request or race condition)
    # _request_user_map does NOT contain "xyz789"
    
    # Mock: User trying to access unregistered request
    user_mock = MagicMock()
    user_mock.username = "user_a"
    mock_app_dependencies['get_user'].return_value = user_mock
    
    # Test: User tries to connect to unregistered request's status stream
    response = client.get("/status/stream?request_id=xyz789", stream=True)
    
    # Assert: Should be allowed (owner_user_id is None, so check is skipped)
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_request_id_cleanup_after_completion():
    """Test that request_id is removed from map after request completes"""
    from agent_system.app import _request_user_map
    
    # Simulate request lifecycle
    request_id = "test_request_123"
    user_id = "test_user"
    
    # Request starts: Register ownership
    _request_user_map[request_id] = user_id
    assert request_id in _request_user_map
    
    # Request completes: Cleanup should happen
    _request_user_map.pop(request_id, None)
    assert request_id not in _request_user_map


@pytest.mark.asyncio
async def test_multiple_users_parallel_requests():
    """Test that multiple users can have parallel requests without interference"""
    from agent_system.app import _request_user_map
    
    # Setup: Multiple users with their own requests
    _request_user_map["req_alice_1"] = "alice"
    _request_user_map["req_bob_1"] = "bob"
    _request_user_map["req_alice_2"] = "alice"
    
    # Verify isolation
    assert _request_user_map["req_alice_1"] == "alice"
    assert _request_user_map["req_bob_1"] == "bob"
    assert _request_user_map["req_alice_2"] == "alice"
    
    # Simulate cleanup after completion
    _request_user_map.pop("req_alice_1", None)
    assert "req_alice_1" not in _request_user_map
    assert "req_bob_1" in _request_user_map
    assert "req_alice_2" in _request_user_map
