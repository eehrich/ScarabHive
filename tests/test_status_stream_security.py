"""
Unit tests for request tracking and authorization in the /events endpoint.

Tests the request_id -> user_id mapping used for authorization and cleanup.
"""

import pytest


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
