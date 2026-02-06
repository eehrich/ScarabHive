"""
Test session-level locking to prevent race conditions in parallel requests.
"""
import asyncio
import pytest
from agent_system.servers.agent.components.session_tracking import SessionTracker


@pytest.mark.asyncio
async def test_session_lock_prevents_parallel_access():
    """Test that session lock prevents multiple requests from accessing same session simultaneously."""
    tracker = SessionTracker()
    session_id = "test_session_123"
    request_id_1 = "req_001"
    request_id_2 = "req_002"
    
    # Register requests
    request_entry_1 = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    request_entry_2 = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    
    tracker.register_request(request_id_1, session_id, request_entry_1)
    tracker.register_request(request_id_2, session_id, request_entry_2)
    
    # Request 1 acquires lock
    lock_1 = await tracker.acquire_session_lock(session_id, request_id_1, timeout=1.0)
    assert lock_1 is True, "Request 1 should acquire lock"
    
    # Check lock status
    is_locked, owner = tracker.check_session_locked(session_id)
    assert is_locked is True
    assert owner == request_id_1
    
    # Request 2 tries to acquire same lock (should fail due to timeout)
    lock_2 = await tracker.acquire_session_lock(session_id, request_id_2, timeout=0.5)
    assert lock_2 is False, "Request 2 should NOT acquire lock while Request 1 holds it"
    
    # Request 1 releases lock
    await tracker.release_session_lock(session_id, request_id_1)
    
    # Now Request 2 can acquire lock
    lock_2_retry = await tracker.acquire_session_lock(session_id, request_id_2, timeout=1.0)
    assert lock_2_retry is True, "Request 2 should acquire lock after Request 1 releases"
    
    # Check new owner
    is_locked, owner = tracker.check_session_locked(session_id)
    assert is_locked is True
    assert owner == request_id_2
    
    # Cleanup
    await tracker.release_session_lock(session_id, request_id_2)


@pytest.mark.asyncio
async def test_session_lock_reentrant():
    """Test that same request can re-acquire its own lock (re-entrant)."""
    tracker = SessionTracker()
    session_id = "test_session_456"
    request_id = "req_003"
    
    request_entry = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    tracker.register_request(request_id, session_id, request_entry)
    
    # First acquisition
    lock_1 = await tracker.acquire_session_lock(session_id, request_id, timeout=1.0)
    assert lock_1 is True
    
    # Same request tries again (should succeed immediately - re-entrant)
    lock_2 = await tracker.acquire_session_lock(session_id, request_id, timeout=1.0)
    assert lock_2 is True
    
    # Cleanup
    await tracker.release_session_lock(session_id, request_id)


@pytest.mark.asyncio
async def test_session_lock_released_on_unregister():
    """Test that session lock is released when request is unregistered."""
    tracker = SessionTracker()
    session_id = "test_session_789"
    request_id = "req_004"
    
    request_entry = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    tracker.register_request(request_id, session_id, request_entry)
    
    # Acquire lock
    lock_acquired = await tracker.acquire_session_lock(session_id, request_id, timeout=1.0)
    assert lock_acquired is True
    
    # Verify locked
    is_locked, owner = tracker.check_session_locked(session_id)
    assert is_locked is True
    assert owner == request_id
    
    # Unregister request (should release lock)
    tracker.unregister_request(request_id)
    
    # Verify lock is released
    is_locked, owner = tracker.check_session_locked(session_id)
    assert is_locked is False
    assert owner is None


@pytest.mark.asyncio
async def test_parallel_requests_different_sessions():
    """Test that parallel requests on DIFFERENT sessions don't block each other."""
    tracker = SessionTracker()
    session_id_1 = "session_A"
    session_id_2 = "session_B"
    request_id_1 = "req_A"
    request_id_2 = "req_B"
    
    request_entry_1 = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    request_entry_2 = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    
    tracker.register_request(request_id_1, session_id_1, request_entry_1)
    tracker.register_request(request_id_2, session_id_2, request_entry_2)
    
    # Both should acquire locks successfully (different sessions)
    lock_1 = await tracker.acquire_session_lock(session_id_1, request_id_1, timeout=1.0)
    lock_2 = await tracker.acquire_session_lock(session_id_2, request_id_2, timeout=1.0)
    
    assert lock_1 is True
    assert lock_2 is True
    
    # Check both are locked by different owners
    is_locked_1, owner_1 = tracker.check_session_locked(session_id_1)
    is_locked_2, owner_2 = tracker.check_session_locked(session_id_2)
    
    assert is_locked_1 is True
    assert owner_1 == request_id_1
    assert is_locked_2 is True
    assert owner_2 == request_id_2
    
    # Cleanup
    await tracker.release_session_lock(session_id_1, request_id_1)
    await tracker.release_session_lock(session_id_2, request_id_2)


@pytest.mark.asyncio
async def test_session_lock_timeout_handling():
    """Test that lock acquisition respects timeout parameter."""
    tracker = SessionTracker()
    session_id = "timeout_test"
    request_id_1 = "req_timeout_1"
    request_id_2 = "req_timeout_2"
    
    request_entry_1 = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    request_entry_2 = {"cancel": asyncio.Event(), "message_event": asyncio.Event(), "appended": []}
    
    tracker.register_request(request_id_1, session_id, request_entry_1)
    tracker.register_request(request_id_2, session_id, request_entry_2)
    
    # Request 1 acquires lock
    lock_1 = await tracker.acquire_session_lock(session_id, request_id_1, timeout=1.0)
    assert lock_1 is True
    
    # Request 2 tries with short timeout (should fail fast)
    import time
    start = time.time()
    lock_2 = await tracker.acquire_session_lock(session_id, request_id_2, timeout=0.2)
    elapsed = time.time() - start
    
    assert lock_2 is False
    assert elapsed < 0.5, f"Timeout should trigger quickly, but took {elapsed}s"
    
    # Cleanup
    await tracker.release_session_lock(session_id, request_id_1)
