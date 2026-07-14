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
    """release_request_user_tree entfernt die Request-ID UND alle abgeleiteten
    Sub-IDs (Tool-Suffixe '<id>_001', Sub-Agent-IDs '<id>_sub_...'), aber keine
    fremden Requests — der App-Teardown räumt damit den ganzen Request-Baum."""
    from agent_system.core.request_context import (
        register_request_user, release_request_user_tree, request_user_map,
    )

    register_request_user("parent1", "alice")
    register_request_user("parent1_001", "alice")       # Tool-Call-Suffix
    register_request_user("parent1_sub_xyz", "alice")   # Sub-Agent
    register_request_user("parent1extra", "bob")        # KEIN Kind (kein '_')
    register_request_user("parent2", "bob")

    release_request_user_tree("parent1")

    assert "parent1" not in request_user_map
    assert "parent1_001" not in request_user_map
    assert "parent1_sub_xyz" not in request_user_map
    # Prefix ohne '_'-Trenner und fremde Requests bleiben
    assert request_user_map.get("parent1extra") == "bob"
    assert request_user_map.get("parent2") == "bob"

    release_request_user_tree("parent1extra")
    release_request_user_tree("parent2")


@pytest.mark.asyncio
async def test_register_evicts_oldest_at_cap():
    """FIFO-Backstop: Bei vollem Map verdrängt eine NEUE Registrierung den
    ältesten Eintrag; Re-Registrierung einer bekannten ID verdrängt nichts."""
    import agent_system.core.request_context as rc

    snapshot = dict(rc.request_user_map)
    rc.request_user_map.clear()
    old_max = rc._MAX_ENTRIES
    rc._MAX_ENTRIES = 3
    try:
        rc.register_request_user("r1", "u")
        rc.register_request_user("r2", "u")
        rc.register_request_user("r3", "u")
        # Bekannte ID: kein Evict trotz Cap
        rc.register_request_user("r2", "u2")
        assert set(rc.request_user_map) == {"r1", "r2", "r3"}
        # Neue ID am Cap: ältester Eintrag (r1) fliegt
        rc.register_request_user("r4", "u")
        assert "r1" not in rc.request_user_map
        assert set(rc.request_user_map) == {"r2", "r3", "r4"}
    finally:
        rc._MAX_ENTRIES = old_max
        rc.request_user_map.clear()
        rc.request_user_map.update(snapshot)
