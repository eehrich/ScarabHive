"""Tests for SessionManager CRUD operations."""
import asyncio
import json
import pytest
from pathlib import Path

from agent_system.services.session_manager import (
    SessionDeletedError,
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
async def test_find_session_owner_with_read_error(session_manager, temp_storage):
    """Test that _find_session_owner_async returns user_id from directory name when file read fails.
    
    This tests the fix for the bug where concurrent access causes Permission Denied errors
    on Windows, which previously led to false "Session already exists" errors.
    """
    from unittest.mock import patch
    
    # Create a session first
    session = await session_manager.create_session(
        user_id="user1",
        title="Test Session"
    )
    sid = session["session_id"]
    
    # Mock _read_session_file_async to simulate Permission Denied
    async def mock_read_error(path):
        raise PermissionError("Permission denied")
    
    with patch.object(session_manager, '_read_session_file_async', side_effect=mock_read_error):
        # _find_session_owner_async should still return the owner from directory name
        owner = await session_manager._find_session_owner_async(sid)
        assert owner == "user1", "Should return user_id from directory name when file read fails"


@pytest.mark.asyncio
async def test_find_session_owner_sync_with_read_error(session_manager, temp_storage):
    """Test sync version also handles read errors correctly."""
    from unittest.mock import patch
    
    # Create a session first
    session = await session_manager.create_session(
        user_id="user1",
        title="Test Session"
    )
    sid = session["session_id"]
    
    # Mock _read_session_file to simulate Permission Denied
    def mock_read_error(path):
        raise PermissionError("Permission denied")
    
    with patch.object(session_manager, '_read_session_file', side_effect=mock_read_error):
        # _find_session_owner should still return the owner from directory name
        owner = session_manager._find_session_owner(sid)
        assert owner == "user1", "Should return user_id from directory name when file read fails"


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
    await session_manager.create_session(user_id="user1", title="First")
    await asyncio.sleep(0.01)
    await session_manager.create_session(user_id="user1", title="Second")
    await asyncio.sleep(0.01)
    await session_manager.create_session(user_id="user1", title="Third")
    
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
        {"tags": ["research", "mcp"], "custom_field": "custom_value"}
    )
    
    loaded = await session_manager.load_session("user1", session["session_id"])
    assert loaded["metadata"]["tags"] == ["research", "mcp"]
    assert loaded["metadata"]["custom_field"] == "custom_value"


@pytest.mark.asyncio
async def test_update_session_metadata_deep_merge_sub_agents(session_manager):
    """Test that nested dicts like sub_agents are deep-merged, not replaced.
    
    This is CRITICAL for sub-agent management where multiple sub-agents
    may be added to the same parent session concurrently.
    """
    session = await session_manager.create_session(user_id="user1")
    sid = session["session_id"]
    
    # Add first sub-agent
    await session_manager.update_session_metadata(
        "user1", sid,
        {"sub_agents": {
            "sub1": {"agent_type": "analyzer", "status": "active", "created_at": "2025-01-01T00:00:00Z"}
        }}
    )
    
    # Add second sub-agent - should merge, NOT replace
    await session_manager.update_session_metadata(
        "user1", sid,
        {"sub_agents": {
            "sub2": {"agent_type": "writer", "status": "active", "created_at": "2025-01-01T00:01:00Z"}
        }}
    )
    
    loaded = await session_manager.load_session("user1", sid)
    sub_agents = loaded["metadata"]["sub_agents"]
    
    # Both sub-agents must exist
    assert "sub1" in sub_agents, "First sub-agent was lost during merge!"
    assert "sub2" in sub_agents, "Second sub-agent was not added!"
    assert sub_agents["sub1"]["agent_type"] == "analyzer"
    assert sub_agents["sub2"]["agent_type"] == "writer"


@pytest.mark.asyncio
async def test_update_session_metadata_concurrent_updates(session_manager):
    """Test that concurrent metadata updates don't cause lost updates.
    
    This simulates the real-world scenario where:
    - Main agent is saving conversation history
    - Sub-agent manager is updating sub_agents metadata
    Both happen concurrently to the same parent session.
    """
    session = await session_manager.create_session(user_id="user1")
    sid = session["session_id"]
    
    # Simulate concurrent updates from different tasks
    async def update_sub_agent(name: str, delay: float):
        await asyncio.sleep(delay)
        await session_manager.update_session_metadata(
            "user1", sid,
            {"sub_agents": {name: {"status": "active", "created_at": f"time_{name}"}}}
        )
    
    async def update_custom_field(field: str, value: str, delay: float):
        await asyncio.sleep(delay)
        await session_manager.update_session_metadata(
            "user1", sid,
            {field: value}
        )
    
    # Run 5 concurrent updates
    await asyncio.gather(
        update_sub_agent("agent_a", 0.0),
        update_sub_agent("agent_b", 0.01),
        update_sub_agent("agent_c", 0.02),
        update_custom_field("priority", "high", 0.005),
        update_custom_field("category", "research", 0.015),
    )
    
    loaded = await session_manager.load_session("user1", sid)
    
    # All updates must be present
    sub_agents = loaded["metadata"]["sub_agents"]
    assert "agent_a" in sub_agents, "agent_a lost in concurrent update"
    assert "agent_b" in sub_agents, "agent_b lost in concurrent update"
    assert "agent_c" in sub_agents, "agent_c lost in concurrent update"
    assert loaded["metadata"]["priority"] == "high", "priority lost in concurrent update"
    assert loaded["metadata"]["category"] == "research", "category lost in concurrent update"


@pytest.mark.asyncio
async def test_update_session_metadata_not_found(session_manager):
    """Test that updating non-existent session raises SessionNotFoundError."""
    with pytest.raises(SessionNotFoundError):
        await session_manager.update_session_metadata(
            "user1",
            "nonexistent_session_id",
            {"tags": ["test"]}
        )


@pytest.mark.asyncio
async def test_update_session_metadata_wrong_user_not_found(session_manager):
    """Test that updating with wrong user_id raises SessionNotFoundError.
    
    Sessions are stored under user-specific paths ({user_id}/{session_id}),
    so accessing with wrong user_id means the path doesn't exist.
    This is the intended security model - user isolation by directory structure.
    """
    session = await session_manager.create_session(user_id="user1")
    
    # user2 trying to access user1's session - path won't exist
    with pytest.raises(SessionNotFoundError):
        await session_manager.update_session_metadata(
            "user2",  # Wrong user - path user2/{session_id} doesn't exist
            session["session_id"],
            {"tags": ["hacked"]}
        )


@pytest.mark.asyncio
async def test_update_session_metadata_preserves_existing_fields(session_manager):
    """Test that metadata update preserves fields not in the update."""
    session = await session_manager.create_session(user_id="user1")
    sid = session["session_id"]
    
    # Set initial metadata
    await session_manager.update_session_metadata(
        "user1", sid,
        {"field_a": "value_a", "field_b": "value_b", "nested": {"x": 1, "y": 2}}
    )
    
    # Update only field_a and nested.z
    await session_manager.update_session_metadata(
        "user1", sid,
        {"field_a": "updated_a", "nested": {"z": 3}}
    )
    
    loaded = await session_manager.load_session("user1", sid)
    
    # field_a should be updated
    assert loaded["metadata"]["field_a"] == "updated_a"
    # field_b should be preserved
    assert loaded["metadata"]["field_b"] == "value_b"
    # nested should be deep-merged
    assert loaded["metadata"]["nested"]["x"] == 1, "nested.x was lost"
    assert loaded["metadata"]["nested"]["y"] == 2, "nested.y was lost"
    assert loaded["metadata"]["nested"]["z"] == 3, "nested.z was not added"


@pytest.mark.asyncio
async def test_update_session_metadata_updates_timestamp(session_manager):
    """Test that metadata update also updates the session timestamp."""
    session = await session_manager.create_session(user_id="user1")
    sid = session["session_id"]
    original_updated_at = session["updated_at"]
    
    # Small delay to ensure timestamp difference
    await asyncio.sleep(0.01)
    
    await session_manager.update_session_metadata(
        "user1", sid,
        {"tags": ["test"]}
    )
    
    loaded = await session_manager.load_session("user1", sid)
    assert loaded["updated_at"] > original_updated_at, "Timestamp was not updated"


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
async def test_a_file_another_process_wrote_is_not_served_from_the_cache(temp_storage):
    # A run woken by session presence continues a session a long-lived API
    # process has cached; the API's next request must see what that run wrote.
    api = SessionManager(storage_path=temp_storage)
    session = await api.create_session(user_id="user1")
    await api.load_session("user1", session["session_id"])
    await asyncio.sleep(0.05)  # file times on Windows advance in ~16 ms steps

    woken = SessionManager(storage_path=temp_storage)
    written = await woken.load_session("user1", session["session_id"])
    written["messages"].append({"role": "user", "content": "from the woken run"})
    await woken.save_session(written)

    loaded = await api.load_session("user1", session["session_id"])

    assert loaded["messages"] == written["messages"]


@pytest.mark.asyncio
async def test_changed_on_disk_only_reports_what_another_process_wrote(temp_storage):
    # What the API asks before it re-reads a session for an append: its own
    # writes are not a change, or a run whose newest messages are still only in
    # memory would be sent back to the older file for them.
    api = SessionManager(storage_path=temp_storage)
    session = await api.create_session(user_id="user1")
    sid = session["session_id"]

    other = SessionManager(storage_path=temp_storage)
    assert other.changed_on_disk("user1", sid) is None, "a file it never read is not an answer"

    session["messages"].append({"role": "user", "content": "from this process"})
    await api.save_session(session)
    await asyncio.sleep(0.05)  # file times on Windows advance in ~16 ms steps

    assert api.changed_on_disk("user1", sid) is False

    woken = SessionManager(storage_path=temp_storage)
    written = await woken.load_session("user1", sid)
    written["messages"].append({"role": "user", "content": "from the woken run"})
    await woken.save_session(written)

    assert api.changed_on_disk("user1", sid) is True


@pytest.mark.asyncio
async def test_a_delete_waits_for_a_save_of_the_session_already_under_way(session_manager):
    """The save writes first and the delete comes after it -- not a delete that the save then undoes."""
    session = await session_manager.create_session(user_id="user1", session_id="saving_one")
    entered, release = asyncio.Event(), asyncio.Event()
    write = session_manager._atomic_write_async

    async def slow_write(path, data):
        if Path(path).name == "saving_one.json":
            entered.set()
            await release.wait()
        await write(path, data)

    session_manager._atomic_write_async = slow_write
    session["messages"].append({"role": "user", "content": "late"})
    save = asyncio.create_task(session_manager.save_session(session))
    await entered.wait()
    delete = asyncio.create_task(session_manager.delete_session("user1", "saving_one"))
    await asyncio.sleep(0.05)
    assert not delete.done(), "the delete did not wait for the save under way"
    release.set()
    await save
    await delete
    with pytest.raises(SessionNotFoundError):
        await session_manager.load_session("user1", "saving_one", bypass_cache=True)


@pytest.mark.asyncio
async def test_a_save_that_had_the_session_before_its_delete_does_not_write_it_again(session_manager):
    session = await session_manager.create_session(user_id="user1", session_id="loaded_before")
    session["messages"].append({"role": "user", "content": "from a run still going"})
    await session_manager.delete_session("user1", "loaded_before")

    with pytest.raises(SessionDeletedError):
        await session_manager.save_session(session)
    with pytest.raises(SessionNotFoundError):
        await session_manager.load_session("user1", "loaded_before", bypass_cache=True)


@pytest.mark.asyncio
async def test_a_deleted_session_written_again_by_another_process_is_a_session_again(temp_storage):
    api = SessionManager(storage_path=temp_storage)
    await api.create_session(user_id="user1", session_id="written_again")
    await api.delete_session("user1", "written_again")
    assert api.is_deleted("written_again")

    cli = SessionManager(storage_path=temp_storage)  # agent-cli, say
    await cli.create_session(user_id="user1", session_id="written_again")

    assert not api.is_deleted("written_again")
    await api.update_session_metadata("user1", "written_again", {"tags": ["kept"]})
    assert (await api.load_session("user1", "written_again", bypass_cache=True))["metadata"]["tags"] == ["kept"]


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


# ---------------------------------------------------------------------------
# Cache-based session lookup (race condition prevention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_session_owner_async_uses_cache(session_manager):
    """_find_session_owner_async should find sessions via in-memory cache,
    even if the filesystem scan would miss them (race prevention)."""
    await session_manager.create_session(
        user_id="user1",
        session_id="cached_sid_001",
        title="Cached"
    )
    # Session is in cache now. Remove the file on disk to prove cache lookup works.
    user_dir = Path(session_manager.storage_path) / "user1"
    session_file = user_dir / "cached_sid_001.json"
    session_file.unlink()

    owner = await session_manager._find_session_owner_async("cached_sid_001")
    assert owner == "user1"


@pytest.mark.asyncio
async def test_find_session_owner_sync_uses_cache(session_manager):
    """Sync variant should also consult the cache first."""
    await session_manager.create_session(
        user_id="user1",
        session_id="cached_sid_002",
        title="Cached"
    )
    user_dir = Path(session_manager.storage_path) / "user1"
    (user_dir / "cached_sid_002.json").unlink()

    owner = session_manager._find_session_owner("cached_sid_002")
    assert owner == "user1"


@pytest.mark.asyncio
async def test_session_id_exists_globally_uses_cache(session_manager):
    """_session_id_exists_globally should detect cached sessions."""
    await session_manager.create_session(
        user_id="user1",
        session_id="cached_sid_003",
        title="Cached"
    )
    user_dir = Path(session_manager.storage_path) / "user1"
    (user_dir / "cached_sid_003.json").unlink()

    assert session_manager._session_id_exists_globally("cached_sid_003") is True


@pytest.mark.asyncio
async def test_save_session_preserves_parent_session(session_manager):
    """save_session must not strip parent_session (sub-agent metadata)."""
    session = await session_manager.create_session(
        user_id="user1",
        session_id="sub_agent_001",
        title="Sub-agent"
    )
    session["parent_session"] = {"session_id": "parent_001", "created_at": "2025-01-01T00:00:00Z"}
    session["depth"] = 2
    await session_manager.save_session(session)

    loaded = await session_manager.load_session("user1", "sub_agent_001")
    assert loaded["parent_session"]["session_id"] == "parent_001"
    assert loaded["depth"] == 2


class TestResolveSessionRef:
    """A person types the name they gave a session; an id is machine-made.

    The id cannot be renamed -- it is the key the usage tracker, the message
    debugger, the context stores, the sub-session indexes and the presence
    locks file their rows under -- so the title is the name, and --session
    and /resume take it.
    """

    async def _titled(self, manager, title, session_id, updated_at=None):
        session = await manager.create_session(
            user_id="u", session_id=session_id, title=title,
            agent_name="a", llm_profile="p")
        if updated_at:
            session["updated_at"] = updated_at
        await manager.save_session(session)
        return session

    @pytest.mark.asyncio
    async def test_an_id_wins_over_a_title_that_looks_like_one(self, session_manager):
        await self._titled(session_manager, "fpga", "abc123")
        await self._titled(session_manager, "Quartus", "fpga")

        assert await session_manager.resolve_session_ref("u", "fpga") == "fpga"

    @pytest.mark.asyncio
    async def test_a_title_finds_its_session_whatever_the_case(self, session_manager):
        await self._titled(session_manager, "FPGA Quartus", "2332j2kj22k")

        assert await session_manager.resolve_session_ref("u", "fpga quartus") == "2332j2kj22k"

    @pytest.mark.asyncio
    async def test_the_same_title_many_times_means_the_newest(self, session_manager):
        """A pipeline writes hundreds of "Bewerte Kapitel 3" -- the one the
        person means is the one they last worked in."""
        await self._titled(session_manager, "Bewerte Kapitel 3", "old1",
                           updated_at="2026-09-01T10:00:00+00:00")
        await self._titled(session_manager, "Bewerte Kapitel 3", "new1",
                           updated_at="2026-09-24T10:00:00+00:00")

        assert await session_manager.resolve_session_ref("u", "Bewerte Kapitel 3") == "new1"

    @pytest.mark.asyncio
    async def test_the_start_of_a_title_is_enough(self, session_manager):
        await self._titled(session_manager, "FPGA Quartus Prime", "xyz789")

        assert await session_manager.resolve_session_ref("u", "FPGA Qua") == "xyz789"

    @pytest.mark.asyncio
    async def test_a_prefix_that_fits_two_sessions_names_neither(self, session_manager):
        """`--session build` creates a session called "build"; joining a
        stranger's conversation because the first letters matched is worse."""
        await self._titled(session_manager, "Build pipeline v4", "aaa111")
        await self._titled(session_manager, "Build the panel", "bbb222")

        assert await session_manager.resolve_session_ref("u", "Build") is None

    @pytest.mark.asyncio
    async def test_a_name_nobody_gave_resolves_to_nothing(self, session_manager):
        await self._titled(session_manager, "FPGA Quartus", "xyz789")

        assert await session_manager.resolve_session_ref("u", "Amiga") is None
        assert await session_manager.resolve_session_ref("u", "   ") is None

    @pytest.mark.asyncio
    async def test_another_users_session_is_not_found_by_its_title(self, session_manager):
        await self._titled(session_manager, "FPGA Quartus", "xyz789")

        assert await session_manager.resolve_session_ref("somebody_else", "FPGA Quartus") is None
