"""Tests for SessionManager CRUD operations."""
import asyncio
import json
import pytest
from pathlib import Path
from datetime import datetime

from agent_system.services.session_manager import (
    SessionManager,
    SessionNotFoundError,
    SessionPermissionError
)


@pytest.fixture
def temp_storage(tmp_path):
    """Create temporary storage directory."""
    storage = tmp_path / "test_sessions"
    storage.mkdir()
    return str(storage)


@pytest.fixture
def session_manager(temp_storage):
    """Create SessionManager instance with temp storage."""
    return SessionManager(storage_path=temp_storage)


@pytest.mark.asyncio
async def test_create_session_basic(session_manager):
    """Test creating a basic session."""
    session = await session_manager.create_session(
        user_id="user1",
        title="Test Session",
        agent_name="test_agent",
        llm_profile="gpt-4"
    )
    
    assert session["user_id"] == "user1"
    assert session["title"] == "Test Session"
    assert session["agent_name"] == "test_agent"
    assert session["llm_profile"] == "gpt-4"
    assert "session_id" in session
    assert len(session["session_id"]) == 10  # Session IDs are 10 chars (hex)
    assert session["messages"] == []
    assert session["metadata"]["message_count"] == 0


@pytest.mark.asyncio
async def test_create_session_with_custom_id(session_manager):
    """Test creating session with custom ID."""
    session = await session_manager.create_session(
        user_id="user1",
        title="Custom ID Session",
        session_id="custom_id_123"
    )
    
    assert session["session_id"] == "custom_id_123"


@pytest.mark.asyncio
async def test_create_session_duplicate_id_fails(session_manager):
    """Test that creating duplicate session ID fails."""
    await session_manager.create_session(
        user_id="user1",
        session_id="duplicate_id"
    )
    
    with pytest.raises(ValueError, match="already exists"):
        await session_manager.create_session(
            user_id="user1",
            session_id="duplicate_id"
        )


@pytest.mark.asyncio
async def test_create_session_global_collision_detection(session_manager):
    """Test that duplicate session IDs are detected across different users (global)."""
    # Create session for user1
    await session_manager.create_session(
        user_id="user1",
        session_id="shared_id_123"
    )
    
    # Try to create session with same ID for different user (should fail)
    with pytest.raises(ValueError, match="already exists"):
        await session_manager.create_session(
            user_id="user2",  # Different user!
            session_id="shared_id_123"  # Same ID - should be rejected
        )


@pytest.mark.asyncio
async def test_session_id_uniqueness_across_users(session_manager):
    """Test that auto-generated session IDs are unique across all users."""
    # Create many sessions for different users
    session_ids = set()
    
    for i in range(50):
        user_id = f"user{i % 5}"  # 5 different users
        session = await session_manager.create_session(
            user_id=user_id,
            title=f"Session {i}"
        )
        
        # Check for collision
        assert session["session_id"] not in session_ids, \
            f"Session ID collision detected: {session['session_id']}"
        
        session_ids.add(session["session_id"])
    
    # All IDs should be unique
    assert len(session_ids) == 50


@pytest.mark.asyncio
async def test_load_session_success(session_manager):
    """Test loading an existing session."""
    created = await session_manager.create_session(
        user_id="user1",
        title="Load Test"
    )
    
    loaded = await session_manager.load_session("user1", created["session_id"])
    
    assert loaded["session_id"] == created["session_id"]
    assert loaded["title"] == "Load Test"
    assert loaded["user_id"] == "user1"


@pytest.mark.asyncio
async def test_load_session_not_found(session_manager):
    """Test loading non-existent session."""
    with pytest.raises(SessionNotFoundError):
        await session_manager.load_session("user1", "nonexistent_id")


@pytest.mark.asyncio
async def test_load_session_permission_denied(session_manager):
    """Test loading session owned by different user."""
    session = await session_manager.create_session(
        user_id="user1",
        title="User1 Session"
    )
    
    with pytest.raises(SessionPermissionError):
        await session_manager.load_session("user2", session["session_id"])


@pytest.mark.asyncio
async def test_save_session_updates_timestamp(session_manager):
    """Test that saving updates the timestamp."""
    session = await session_manager.create_session(user_id="user1")
    original_updated = session["updated_at"]
    
    # Small delay to ensure timestamp changes
    await asyncio.sleep(0.01)
    
    session["title"] = "Updated Title"
    await session_manager.save_session(session)
    
    loaded = await session_manager.load_session("user1", session["session_id"])
    assert loaded["updated_at"] > original_updated
    assert loaded["title"] == "Updated Title"


@pytest.mark.asyncio
async def test_save_session_updates_message_count(session_manager):
    """Test that saving updates message count metadata."""
    session = await session_manager.create_session(user_id="user1")
    
    session["messages"].append({
        "role": "user",
        "content": "Hello"
    })
    session["messages"].append({
        "role": "assistant",
        "content": "Hi there!"
    })
    
    await session_manager.save_session(session)
    
    loaded = await session_manager.load_session("user1", session["session_id"])
    assert loaded["metadata"]["message_count"] == 2


@pytest.mark.asyncio
async def test_save_session_captures_last_response(session_manager):
    """Test that saving captures last assistant response."""
    session = await session_manager.create_session(user_id="user1")
    
    session["messages"].append({
        "role": "assistant",
        "content": "This is the last response from the assistant"
    })
    
    await session_manager.save_session(session)
    
    loaded = await session_manager.load_session("user1", session["session_id"])
    assert "last response" in loaded["metadata"]["last_agent_response"]


@pytest.mark.asyncio
async def test_delete_session_success(session_manager):
    """Test deleting a session."""
    session = await session_manager.create_session(user_id="user1")
    session_id = session["session_id"]
    
    await session_manager.delete_session("user1", session_id, create_backup=False)
    
    with pytest.raises(SessionNotFoundError):
        await session_manager.load_session("user1", session_id)


@pytest.mark.asyncio
async def test_delete_session_creates_backup(session_manager, temp_storage):
    """Test that delete creates backup file."""
    session = await session_manager.create_session(user_id="user1")
    session_id = session["session_id"]
    
    await session_manager.delete_session("user1", session_id, create_backup=True)
    
    # Check backup file exists
    user_dir = Path(temp_storage) / "user1"
    backups = list(user_dir.glob(f".backup_{session_id}_*.json"))
    assert len(backups) == 1


@pytest.mark.asyncio
async def test_delete_session_permission_denied(session_manager):
    """Test deleting session owned by different user."""
    session = await session_manager.create_session(user_id="user1")
    
    with pytest.raises(SessionPermissionError):
        await session_manager.delete_session("user2", session["session_id"])


@pytest.mark.asyncio
async def test_list_sessions_empty(session_manager):
    """Test listing sessions for user with no sessions."""
    sessions = await session_manager.list_sessions("user1")
    assert sessions == []


@pytest.mark.asyncio
async def test_list_sessions_single_user(session_manager):
    """Test listing sessions for a single user."""
    await session_manager.create_session(user_id="user1", title="Session 1")
    await session_manager.create_session(user_id="user1", title="Session 2")
    await session_manager.create_session(user_id="user1", title="Session 3")
    
    sessions = await session_manager.list_sessions("user1")
    
    assert len(sessions) == 3
    titles = {s["title"] for s in sessions}
    assert titles == {"Session 1", "Session 2", "Session 3"}


@pytest.mark.asyncio
async def test_list_sessions_user_isolation(session_manager):
    """Test that list_sessions only returns user's own sessions."""
    await session_manager.create_session(user_id="user1", title="User1 Session 1")
    await session_manager.create_session(user_id="user1", title="User1 Session 2")
    await session_manager.create_session(user_id="user2", title="User2 Session 1")
    
    user1_sessions = await session_manager.list_sessions("user1")
    user2_sessions = await session_manager.list_sessions("user2")
    
    assert len(user1_sessions) == 2
    assert len(user2_sessions) == 1
    assert all(s["title"].startswith("User1") for s in user1_sessions)
    assert all(s["title"].startswith("User2") for s in user2_sessions)


@pytest.mark.asyncio
async def test_list_sessions_sorted_by_updated(session_manager):
    """Test that sessions are sorted by updated_at (most recent first)."""
    s1 = await session_manager.create_session(user_id="user1", title="First")
    await asyncio.sleep(0.01)
    s2 = await session_manager.create_session(user_id="user1", title="Second")
    await asyncio.sleep(0.01)
    s3 = await session_manager.create_session(user_id="user1", title="Third")
    
    sessions = await session_manager.list_sessions("user1")
    
    assert sessions[0]["title"] == "Third"
    assert sessions[1]["title"] == "Second"
    assert sessions[2]["title"] == "First"


@pytest.mark.asyncio
async def test_list_sessions_metadata_only(session_manager):
    """Test that list_sessions returns metadata without full messages."""
    session = await session_manager.create_session(user_id="user1")
    
    # Add messages
    session["messages"] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"}
    ]
    await session_manager.save_session(session)
    
    sessions = await session_manager.list_sessions("user1")
    
    assert len(sessions) == 1
    # Should not have messages field (only metadata)
    assert "messages" not in sessions[0]
    assert sessions[0]["message_count"] == 2


@pytest.mark.asyncio
async def test_rename_session(session_manager):
    """Test renaming a session."""
    session = await session_manager.create_session(
        user_id="user1",
        title="Original Title"
    )
    
    await session_manager.rename_session("user1", session["session_id"], "New Title")
    
    loaded = await session_manager.load_session("user1", session["session_id"])
    assert loaded["title"] == "New Title"


@pytest.mark.asyncio
async def test_update_session_metadata(session_manager):
    """Test updating session metadata."""
    session = await session_manager.create_session(user_id="user1")
    
    await session_manager.update_session_metadata(
        "user1",
        session["session_id"],
        tags=["research", "mcp"],
        custom_field="custom_value"
    )
    
    loaded = await session_manager.load_session("user1", session["session_id"])
    assert loaded["metadata"]["tags"] == ["research", "mcp"]
    assert loaded["metadata"]["custom_field"] == "custom_value"


@pytest.mark.asyncio
async def test_cache_functionality(session_manager):
    """Test that caching works correctly."""
    session = await session_manager.create_session(user_id="user1")
    
    # First load - from disk
    await session_manager.load_session("user1", session["session_id"])
    
    # Check cache
    cache_stats = session_manager.get_cache_stats()
    assert session["session_id"] in cache_stats["entries"]
    
    # Second load - from cache (should be fast)
    await session_manager.load_session("user1", session["session_id"])


@pytest.mark.asyncio
async def test_clear_cache(session_manager):
    """Test clearing the cache."""
    session = await session_manager.create_session(user_id="user1")
    await session_manager.load_session("user1", session["session_id"])
    
    assert len(session_manager.get_cache_stats()["entries"]) == 1
    
    session_manager.clear_cache()
    
    assert len(session_manager.get_cache_stats()["entries"]) == 0


@pytest.mark.asyncio
async def test_atomic_write_integrity(session_manager, temp_storage):
    """Test that atomic writes preserve file integrity."""
    session = await session_manager.create_session(user_id="user1")
    
    # Modify and save multiple times
    for i in range(10):
        session["messages"].append({
            "role": "user",
            "content": f"Message {i}"
        })
        await session_manager.save_session(session)
    
    # Verify file is valid JSON
    user_dir = Path(temp_storage) / "user1"
    session_file = user_dir / f"{session['session_id']}.json"
    
    with open(session_file, 'r') as f:
        data = json.load(f)
    
    assert len(data["messages"]) == 10
    assert data["session_id"] == session["session_id"]


@pytest.mark.asyncio
async def test_directory_traversal_protection(session_manager):
    """Test that directory traversal attacks are prevented."""
    # Try to create session with malicious user_id
    session = await session_manager.create_session(
        user_id="../../../etc/passwd",
        title="Malicious"
    )
    
    # User ID should be sanitized (.. -> _, / -> _, \ -> _)
    # "../../../etc/passwd" becomes "______etc_passwd"
    loaded = await session_manager.load_session("______etc_passwd", session["session_id"])
    assert loaded is not None


@pytest.mark.asyncio
async def test_invalid_session_data_validation(session_manager):
    """Test that invalid session data is rejected."""
    session = await session_manager.create_session(user_id="user1")
    
    # Remove required field
    del session["title"]
    
    with pytest.raises(ValueError, match="Missing required field"):
        await session_manager.save_session(session)


@pytest.mark.asyncio
async def test_corrupt_session_file_handling(session_manager, temp_storage):
    """Test handling of corrupted session files."""
    session = await session_manager.create_session(user_id="user1")
    
    # Clear cache to ensure file is read from disk
    session_manager.clear_cache()
    
    # Corrupt the file
    user_dir = Path(temp_storage) / "user1"
    session_file = user_dir / f"{session['session_id']}.json"
    
    with open(session_file, 'w') as f:
        f.write("{ invalid json }")
    
    with pytest.raises(ValueError, match="Corrupt session file"):
        await session_manager.load_session("user1", session["session_id"], bypass_cache=True)
