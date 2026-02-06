"""Integration tests for session management system."""
import pytest
import asyncio
from pathlib import Path

from agent_system.services.session_manager import (
    SessionManager,
    SessionNotFoundError,
    SessionPermissionError
)


@pytest.fixture
def temp_storage(tmp_path):
    """Temporary storage for tests."""
    return str(tmp_path / "sessions")


@pytest.fixture
def session_manager(temp_storage):
    """SessionManager instance."""
    return SessionManager(storage_path=temp_storage)


@pytest.mark.asyncio
async def test_end_to_end_session_lifecycle(session_manager):
    """Test complete session lifecycle: create, use, update, delete."""
    # Create session
    session = await session_manager.create_session(
        user_id="test_user",
        title="E2E Test Session",
        agent_name="test_agent",
        llm_profile="gpt-4"
    )
    
    session_id = session["session_id"]
    assert session["user_id"] == "test_user"
    assert session["title"] == "E2E Test Session"
    assert len(session["messages"]) == 0
    
    # Add messages (simulating conversation)
    session["messages"].append({
        "role": "user",
        "content": "Hello"
    })
    session["messages"].append({
        "role": "assistant",
        "content": "Hi there!"
    })
    
    await session_manager.save_session(session)
    
    # Load session
    loaded = await session_manager.load_session("test_user", session_id)
    assert len(loaded["messages"]) == 2
    assert loaded["metadata"]["message_count"] == 2
    
    # Rename session
    await session_manager.rename_session("test_user", session_id, "Updated Title")
    renamed = await session_manager.load_session("test_user", session_id)
    assert renamed["title"] == "Updated Title"
    
    # Update metadata
    await session_manager.update_session_metadata(
        "test_user",
        session_id,
        {"tags": ["important", "test"]}
    )
    updated = await session_manager.load_session("test_user", session_id)
    assert updated["metadata"]["tags"] == ["important", "test"]
    
    # Delete session
    await session_manager.delete_session("test_user", session_id)
    
    with pytest.raises(SessionNotFoundError):
        await session_manager.load_session("test_user", session_id)


@pytest.mark.asyncio
async def test_multi_user_isolation(session_manager):
    """Test that users cannot access each other's sessions."""
    # Create sessions for different users
    user1_session = await session_manager.create_session(
        user_id="user1",
        title="User 1 Session"
    )
    
    user2_session = await session_manager.create_session(
        user_id="user2",
        title="User 2 Session"
    )
    
    # User1 can access their own session
    loaded = await session_manager.load_session("user1", user1_session["session_id"])
    assert loaded["title"] == "User 1 Session"
    
    # User1 cannot access user2's session
    with pytest.raises(SessionPermissionError):
        await session_manager.load_session("user1", user2_session["session_id"])
    
    # User2 cannot delete user1's session
    with pytest.raises(SessionPermissionError):
        await session_manager.delete_session("user2", user1_session["session_id"])
    
    # Each user sees only their own sessions
    user1_sessions = await session_manager.list_sessions("user1")
    user2_sessions = await session_manager.list_sessions("user2")
    
    assert len(user1_sessions) == 1
    assert len(user2_sessions) == 1
    assert user1_sessions[0]["session_id"] == user1_session["session_id"]
    assert user2_sessions[0]["session_id"] == user2_session["session_id"]


@pytest.mark.asyncio
async def test_concurrent_access(session_manager):
    """Test concurrent access to sessions."""
    session = await session_manager.create_session(user_id="test_user")
    session_id = session["session_id"]
    
    # Simulate concurrent reads
    async def read_session():
        return await session_manager.load_session("test_user", session_id)
    
    results = await asyncio.gather(
        read_session(),
        read_session(),
        read_session(),
    )
    
    # All reads should succeed
    assert len(results) == 3
    assert all(r["session_id"] == session_id for r in results)


@pytest.mark.asyncio
async def test_cache_behavior(session_manager):
    """Test caching functionality."""
    session = await session_manager.create_session(user_id="test_user")
    session_id = session["session_id"]
    
    # First load - from disk
    await session_manager.load_session("test_user", session_id)
    
    # Check cache
    stats = session_manager.get_cache_stats()
    assert session_id in stats["entries"]
    
    # Second load - from cache (should be faster)
    await session_manager.load_session("test_user", session_id)
    
    # Clear cache
    session_manager.clear_cache()
    stats = session_manager.get_cache_stats()
    assert session_id not in stats["entries"]


@pytest.mark.asyncio
async def test_session_restore_after_restart(session_manager, temp_storage):
    """Test that sessions persist across manager restarts."""
    # Create session with first manager instance
    session = await session_manager.create_session(
        user_id="test_user",
        title="Persistent Session"
    )
    session["messages"] = [
        {"role": "user", "content": "Test message"}
    ]
    await session_manager.save_session(session)
    session_id = session["session_id"]
    
    # Create new manager instance (simulating restart)
    new_manager = SessionManager(storage_path=temp_storage)
    
    # Load session with new instance
    loaded = await new_manager.load_session("test_user", session_id)
    assert loaded["title"] == "Persistent Session"
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0]["content"] == "Test message"


@pytest.mark.asyncio
async def test_large_conversation(session_manager):
    """Test handling of large conversations."""
    session = await session_manager.create_session(user_id="test_user")
    
    # Add 1000 messages
    for i in range(1000):
        session["messages"].append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message {i}"
        })
    
    await session_manager.save_session(session)
    
    # Load and verify
    loaded = await session_manager.load_session("test_user", session["session_id"])
    assert len(loaded["messages"]) == 1000
    assert loaded["metadata"]["message_count"] == 1000


@pytest.mark.asyncio
async def test_session_list_sorting(session_manager):
    """Test that sessions are sorted by updated_at."""
    # Create multiple sessions with delays
    s1 = await session_manager.create_session(user_id="test_user", title="First")
    await asyncio.sleep(0.01)
    await session_manager.create_session(user_id="test_user", title="Second")
    await asyncio.sleep(0.01)
    
    # Update first session (should move to top)
    s1_loaded = await session_manager.load_session("test_user", s1["session_id"])
    s1_loaded["messages"].append({"role": "user", "content": "Update"})
    await session_manager.save_session(s1_loaded)
    
    # List should have updated session first
    sessions = await session_manager.list_sessions("test_user")
    assert sessions[0]["session_id"] == s1["session_id"]


@pytest.mark.asyncio
async def test_delete_with_backup(session_manager, temp_storage):
    """Test that delete creates backup."""
    session = await session_manager.create_session(
        user_id="test_user",
        title="To Delete"
    )
    session_id = session["session_id"]
    
    # Delete with backup
    await session_manager.delete_session("test_user", session_id, create_backup=True)
    
    # Check backup file exists
    user_dir = Path(temp_storage) / "test_user"
    backups = list(user_dir.glob(f".backup_{session_id}_*.json"))
    assert len(backups) == 1


@pytest.mark.asyncio
async def test_metadata_updates(session_manager):
    """Test various metadata update scenarios."""
    session = await session_manager.create_session(user_id="test_user")
    session_id = session["session_id"]
    
    # Add tags
    await session_manager.update_session_metadata(
        "test_user",
        session_id,
        {"tags": ["work", "important"]}
    )
    
    loaded = await session_manager.load_session("test_user", session_id)
    assert loaded["metadata"]["tags"] == ["work", "important"]
    
    # Add custom fields
    await session_manager.update_session_metadata(
        "test_user",
        session_id,
        {"custom_field": "custom_value", "priority": "high"}
    )
    
    loaded = await session_manager.load_session("test_user", session_id)
    assert loaded["metadata"]["custom_field"] == "custom_value"
    assert loaded["metadata"]["priority"] == "high"
    # Tags should still be there
    assert loaded["metadata"]["tags"] == ["work", "important"]


@pytest.mark.asyncio
async def test_error_handling(session_manager):
    """Test error handling in various scenarios."""
    # Load non-existent session
    with pytest.raises(SessionNotFoundError):
        await session_manager.load_session("test_user", "nonexistent123")
    
    # Create duplicate session
    await session_manager.create_session(
        user_id="test_user",
        session_id="custom123"
    )
    
    with pytest.raises(ValueError, match="already exists"):
        await session_manager.create_session(
            user_id="test_user",
            session_id="custom123"
        )
    
    # Rename non-existent session
    with pytest.raises(SessionNotFoundError):
        await session_manager.rename_session("test_user", "nonexistent", "New Title")
