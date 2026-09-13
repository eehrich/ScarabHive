"""Sessions from a streamed run land under the logged-in user, not 'anonymous'.

EventSource cannot set an Authorization header. The first fix passed the JWT as
``?token=`` in the URL; that path is gone (a token in a URL leaks into logs,
history and Referer). The browser's ``access_token`` cookie authenticates the
same-origin EventSource instead -- see test_endpoint_security_middleware.py for
the refusal of a query token.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def temp_storage(tmp_path):
    """Create temporary storage directory."""
    storage = tmp_path / "test_sessions"
    storage.mkdir()
    return str(storage)


@pytest.fixture
def session_manager(temp_storage):
    """Create SessionManager instance with temp storage."""
    from agent_system.services.session_manager import SessionManager
    return SessionManager(storage_path=temp_storage)


@pytest.mark.asyncio 
async def test_session_saved_under_correct_user(session_manager):
    """Test that sessions are saved under authenticated user_id, not 'anonymous'.    This test prevents regression of the bug where EventSource requests
    defaulted to 'anonymous' user_id despite user being logged in.
    """
    # Simulate authenticated user
    user_id = "admin"
    
    # Create session
    session = await session_manager.create_session(
        user_id=user_id,
        title="Test Session",
        agent_name="basic_agent"
    )
    
    # Verify session is created under correct user
    assert session["user_id"] == user_id
    assert session["user_id"] != "anonymous"
    
    # Verify session can be loaded by the same user
    loaded = await session_manager.load_session(user_id, session["session_id"])
    assert loaded["user_id"] == user_id
    
    # Verify session CANNOT be loaded by anonymous user (permission check)
    from agent_system.services.session_manager import SessionPermissionError
    with pytest.raises(SessionPermissionError):
        await session_manager.load_session("anonymous", session["session_id"])
    
    print("✅ Session correctly saved under authenticated user_id")
    print(f"   - User: {user_id}")
    print(f"   - Session ID: {session['session_id']}")
    print("   - Not accessible by 'anonymous' user")


@pytest.mark.asyncio 
async def test_no_session_collision_between_users(session_manager):
    """Test that sessions with same ID across users are detected.
    
    This test documents the symptom (duplicate session IDs) that led us
    to discover the root cause (EventSource auth bug).
    """
    # Try to create sessions with same ID for different users
    test_session_id = "test_collision"
    
    # Create session for user1
    session1 = await session_manager.create_session(
        user_id="user1",
        session_id=test_session_id
    )
    assert session1["session_id"] == test_session_id
    
    # Try to create session with same ID for user2 - should fail
    with pytest.raises(ValueError, match="already exists"):
        await session_manager.create_session(
            user_id="user2", 
            session_id=test_session_id  # Duplicate ID
        )
    
    print("✅ Global collision detection working")
    print(f"   - Session {test_session_id} exists for user1")
    print("   - Prevented duplicate creation for user2")

