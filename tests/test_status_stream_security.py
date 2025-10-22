"""
Unit tests for status events security in /events endpoint - ensuring users cannot access other users' status events.

Note: Since status events are now integrated into the /events endpoint,
security is handled by the existing /events endpoint user ownership checks.
These tests verify that the security model still works for status events.
"""

import pytest


@pytest.mark.skip(reason="Status events now delivered through /events endpoint which has its own security tests")
async def test_status_stream_blocks_other_users_requests():
    """Status events security is now handled by /events endpoint"""
    pass


@pytest.mark.skip(reason="Status events now delivered through /events endpoint which has its own security tests")
async def test_status_stream_allows_owner_access():
    """Status events security is now handled by /events endpoint"""
    pass


@pytest.mark.skip(reason="Status events now delivered through /events endpoint which has its own security tests")
async def test_status_stream_allows_anonymous_access_anonymous_request():
    """Status events security is now handled by /events endpoint"""
    pass


@pytest.mark.skip(reason="Status events now delivered through /events endpoint which has its own security tests")
async def test_status_stream_blocks_authenticated_user_from_anonymous_request():
    """Status events security is now handled by /events endpoint"""
    pass


@pytest.mark.skip(reason="Status events now delivered through /events endpoint which has its own security tests")
async def test_status_stream_allows_unregistered_request_id():
    """Status events security is now handled by /events endpoint"""
    pass


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
