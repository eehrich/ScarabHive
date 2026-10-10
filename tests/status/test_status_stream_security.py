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


@pytest.mark.asyncio
async def test_release_tree_removes_derived_sub_ids():
    """release_request_user_tree removes the request ID AND all derived
    sub-IDs (tool suffixes '<id>_001', sub-agent IDs '<id>_sub_...'), but no
    foreign requests -- the app teardown thereby clears the whole request tree."""
    from agent_system.core.request_context import (
        register_request_user, release_request_user_tree, request_user_map,
    )

    register_request_user("parent1", "alice")
    register_request_user("parent1_001", "alice")       # tool-call suffix
    register_request_user("parent1_sub_xyz", "alice")   # sub-agent
    register_request_user("parent1extra", "bob")        # NOT a child (no '_')
    register_request_user("parent2", "bob")

    release_request_user_tree("parent1")

    assert "parent1" not in request_user_map
    assert "parent1_001" not in request_user_map
    assert "parent1_sub_xyz" not in request_user_map
    # A prefix without a '_' separator and foreign requests stay
    assert request_user_map.get("parent1extra") == "bob"
    assert request_user_map.get("parent2") == "bob"

    release_request_user_tree("parent1extra")
    release_request_user_tree("parent2")


@pytest.mark.asyncio
async def test_register_evicts_oldest_at_cap():
    """FIFO backstop: when the map is full, a NEW registration evicts the
    oldest entry; re-registering a known ID evicts nothing."""
    import agent_system.core.request_context as rc

    snapshot = dict(rc.request_user_map)
    rc.request_user_map.clear()
    old_max = rc._MAX_ENTRIES
    rc._MAX_ENTRIES = 3
    try:
        rc.register_request_user("r1", "u")
        rc.register_request_user("r2", "u")
        rc.register_request_user("r3", "u")
        # Known ID: no eviction despite the cap
        rc.register_request_user("r2", "u2")
        assert set(rc.request_user_map) == {"r1", "r2", "r3"}
        # New ID at the cap: the oldest entry (r1) is evicted
        rc.register_request_user("r4", "u")
        assert "r1" not in rc.request_user_map
        assert set(rc.request_user_map) == {"r2", "r3", "r4"}
    finally:
        rc._MAX_ENTRIES = old_max
        rc.request_user_map.clear()
        rc.request_user_map.update(snapshot)
