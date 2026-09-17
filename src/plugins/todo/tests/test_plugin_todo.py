"""
TODO Management Plugin - Comprehensive Test Suite

Tests cover:
- Task lifecycle (create, update, status transitions)
- Dependency management (circular detection, cascade delete)
- Query/filtering (status, priority, tags, unblocked)
- Persistence (save/load, JSON integrity)
- Integration (sequential_thinking linkage)
- Multi-mode todo() tool dispatch
- Edge cases and error handling

Target: >90% code coverage
"""

import json
import pytest
from pathlib import Path
from typing import Dict, Any
from unittest.mock import MagicMock

from plugins.todo.server import (
    TodoServer,
    Task,
    TaskStatus,
    TaskPriority,
    TaskCollection,
    TodoError,
    ValidationError,
    DependencyError,
    StorageError,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory for tests"""
    storage = tmp_path / "todos"
    storage.mkdir()
    return storage


@pytest.fixture
def mock_system_config() -> MagicMock:
    """Mock AgentSystemConfig for testing"""
    return MagicMock()


@pytest.fixture
def mock_server_config(temp_storage: Path) -> MagicMock:
    """Mock ToolServerConfig with TODO plugin settings"""
    config = MagicMock()
    config.storage_path = str(temp_storage)
    config.max_tasks_per_session = 100
    config.enable_dependencies = True
    config.auto_save = True
    return config


@pytest.fixture
def server(mock_system_config: MagicMock, mock_server_config: MagicMock) -> TodoServer:
    """TodoServer instance"""
    return TodoServer(
        name="todo",
        system_config=mock_system_config,
        server_config=mock_server_config,
    )


@pytest.fixture
def mock_context() -> Dict[str, Any]:
    """Mock tool call context"""
    return {
        "session_id": "test_session_001",
        "agent_name": "test_agent",
    }

# =============================================================================
# Test: Task Creation
# =============================================================================

@pytest.mark.asyncio
async def test_create_todo_basic(server: TodoServer, mock_context: Dict[str, Any]):
    """Test basic task creation"""
    result = await server.create_todo(
        title="Test Task",
        description="Test description",
        priority="high",
        tags=["test", "backend"],
        context=mock_context,
    )
    
    assert result["status"] == "created"
    assert "task_id" in result
    assert result["task"]["title"] == "Test Task"
    assert result["task"]["status"] == TaskStatus.NOT_STARTED.value
    assert result["task"]["priority"] == TaskPriority.HIGH.value
    assert result["task"]["progress"] == 0
    assert "test" in result["task"]["tags"]
    assert result["task"]["session_id"] == "test_session_001"


@pytest.mark.asyncio
async def test_create_todo_with_dependencies(server: TodoServer, mock_context: Dict[str, Any]):
    """Test task creation with dependencies"""
    # Create parent task
    parent = await server.create_todo(
        title="Parent Task",
        context=mock_context,
    )
    parent_id = parent["task_id"]
    
    # Create dependent task
    result = await server.create_todo(
        title="Dependent Task",
        depends_on=[parent_id],
        context=mock_context,
    )
    
    assert result["status"] == "created"
    assert parent_id in result["task"]["depends_on"]
    assert result["is_blocked"] is True  # Blocked because parent not complete
    assert result["task"]["status"] == TaskStatus.BLOCKED.value


@pytest.mark.asyncio
async def test_create_todo_invalid_dependency(server: TodoServer, mock_context: Dict[str, Any]):
    """Test creation with non-existent dependency"""
    result = await server.create_todo(
        title="Test Task",
        depends_on=["nonexistent_task"],
        context=mock_context,
    )
    
    # Should return validation_failed status, not raise exception
    assert result["status"] == "validation_failed"
    assert "does not exist" in result["message"].lower() or "nonexistent" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_todo_max_tasks_limit(server: TodoServer, mock_context: Dict[str, Any]):
    """Test max tasks per session limit"""
    # Create 100 tasks (the limit)
    for i in range(100):
        await server.create_todo(
            title=f"Task {i}",
            context=mock_context,
        )
    
    # 101st task should return limit_reached status
    result = await server.create_todo(
        title="Overflow Task",
        context=mock_context,
    )
    
    assert result["status"] == "limit_reached"
    assert "max tasks limit" in result["message"].lower()


# =============================================================================
# Test: Task Updates
# =============================================================================

@pytest.mark.asyncio
async def test_update_todo_status(server: TodoServer, mock_context: Dict[str, Any]):
    """Test status update with valid transition"""
    # Create task
    created = await server.create_todo(
        title="Test Task",
        context=mock_context,
    )
    task_id = created["task_id"]
    
    # Update to in-progress
    result = await server.update_todo(
        task_id=task_id,
        new_status="in-progress",
        context=mock_context,
    )
    
    assert result["status"] == "updated"
    assert result["task"]["status"] == TaskStatus.IN_PROGRESS.value
    assert result["task"]["started_at"] is not None
    assert "status" in result["changes"]


@pytest.mark.asyncio
async def test_update_todo_invalid_transition(server: TodoServer, mock_context: Dict[str, Any]):
    """Test invalid status transition"""
    # Create completed task
    created = await server.create_todo(title="Test", context=mock_context)
    task_id = created["task_id"]
    
    # Complete it
    await server.update_todo(task_id=task_id, new_status="in-progress", context=mock_context)
    await server.update_todo(task_id=task_id, new_status="completed", context=mock_context)
    
    # Status transitions are unrestricted - can move from completed to in-progress
    result = await server.update_todo(
        task_id=task_id,
        new_status="in-progress",
        context=mock_context,
    )
    assert result["task"]["status"] == "in-progress"


@pytest.mark.asyncio
async def test_update_todo_only_from_changes_nothing_in_another_status(server: TodoServer, mock_context: Dict[str, Any]):
    """A change decided on an out-of-date view: the task finished meanwhile stays finished, and says what it is now."""
    task_id = (await server.create_todo(title="Finished meanwhile", context=mock_context))["task_id"]
    await server.update_todo(task_id=task_id, new_status="completed", context=mock_context)

    refused = await server.update_todo(task_id=task_id, new_status="in-progress", progress=10, only_from=["not-started"],
                                       context=mock_context)
    assert refused == {"task_id": task_id, "status": "status_changed", "message": f"Task '{task_id}' is completed now"}
    task = (await server.get_todo(task_id, context=mock_context))["task"]
    assert (task["status"], task["progress"]) == ("completed", 100)

    applied = await server.update_todo(task_id=task_id, new_status="in-progress", only_from=["completed"], context=mock_context)
    assert applied["task"]["status"] == "in-progress"


@pytest.mark.asyncio
async def test_update_todo_progress(server: TodoServer, mock_context: Dict[str, Any]):
    """Test progress update"""
    created = await server.create_todo(title="Test", context=mock_context)
    task_id = created["task_id"]
    
    result = await server.update_todo(
        task_id=task_id,
        progress=75,
        context=mock_context,
    )
    
    assert result["task"]["progress"] == 75
    assert "progress" in result["changes"]


@pytest.mark.asyncio
async def test_update_todo_add_note(server: TodoServer, mock_context: Dict[str, Any]):
    """Test adding notes to task"""
    created = await server.create_todo(title="Test", context=mock_context)
    task_id = created["task_id"]
    
    result = await server.update_todo(
        task_id=task_id,
        add_note="First note",
        context=mock_context,
    )
    
    assert len(result["task"]["notes"]) == 1
    assert "First note" in result["task"]["notes"][0]
    
    # Add another note
    result = await server.update_todo(
        task_id=task_id,
        add_note="Second note",
        context=mock_context,
    )
    
    assert len(result["task"]["notes"]) == 2


@pytest.mark.asyncio
async def test_update_todo_tags(server: TodoServer, mock_context: Dict[str, Any]):
    """Test tag management"""
    created = await server.create_todo(
        title="Test",
        tags=["initial"],
        context=mock_context,
    )
    task_id = created["task_id"]
    
    # Add tags
    result = await server.update_todo(
        task_id=task_id,
        add_tags=["new1", "new2"],
        context=mock_context,
    )
    
    assert "new1" in result["task"]["tags"]
    assert "new2" in result["task"]["tags"]
    assert "initial" in result["task"]["tags"]
    
    # Remove tags
    result = await server.update_todo(
        task_id=task_id,
        remove_tags=["initial"],
        context=mock_context,
    )
    
    assert "initial" not in result["task"]["tags"]
    assert "new1" in result["task"]["tags"]


@pytest.mark.asyncio
async def test_update_todo_completion_auto_progress(server: TodoServer, mock_context: Dict[str, Any]):
    """Test auto-set progress to 100 on completion"""
    created = await server.create_todo(title="Test", context=mock_context)
    task_id = created["task_id"]
    
    await server.update_todo(task_id=task_id, new_status="in-progress", context=mock_context)
    result = await server.update_todo(
        task_id=task_id,
        new_status="completed",
        context=mock_context,
    )
    
    assert result["task"]["progress"] == 100
    assert result["task"]["completed_at"] is not None


# =============================================================================
# Test: Dependency Management
# =============================================================================

@pytest.mark.asyncio
async def test_blocked_status_auto_update(server: TodoServer, mock_context: Dict[str, Any]):
    """Test auto-blocking when dependency incomplete"""
    # Create parent
    parent = await server.create_todo(title="Parent", context=mock_context)
    parent_id = parent["task_id"]
    
    # Create dependent (should be blocked)
    dependent = await server.create_todo(
        title="Dependent",
        depends_on=[parent_id],
        context=mock_context,
    )
    dep_id = dependent["task_id"]
    
    assert dependent["is_blocked"] is True
    
    # Complete parent
    await server.update_todo(parent_id, new_status="in-progress", context=mock_context)
    await server.update_todo(parent_id, new_status="completed", context=mock_context)
    
    # Note: Auto-unblocking happens on next update, not immediately
    # So we update the dependent task
    updated = await server.update_todo(dep_id, new_status="in-progress", context=mock_context)
    assert updated["task"]["status"] == TaskStatus.IN_PROGRESS.value


@pytest.mark.asyncio
async def test_cascade_delete(server: TodoServer, mock_context: Dict[str, Any]):
    """Test cascade delete of dependent tasks"""
    # Create parent and child
    parent = await server.create_todo(title="Parent", context=mock_context)
    parent_id = parent["task_id"]
    
    child = await server.create_todo(
        title="Child",
        depends_on=[parent_id],
        context=mock_context,
    )
    child_id = child["task_id"]
    
    # Delete parent with cascade
    result = await server._delete_todo_impl(
        task_id=parent_id,
        cascade=True,
        context=mock_context,
    )
    
    assert result["task_id"] == parent_id
    assert child_id in result["cascade_deleted"]
    
    # Verify both deleted
    tasks = await server.list_todos(context=mock_context)
    assert len(tasks["tasks"]) == 0


@pytest.mark.asyncio
async def test_delete_without_cascade(server: TodoServer, mock_context: Dict[str, Any]):
    """Test delete fails when dependents exist (no cascade)"""
    parent = await server.create_todo(title="Parent", context=mock_context)
    parent_id = parent["task_id"]
    
    await server.create_todo(
        title="Child",
        depends_on=[parent_id],
        context=mock_context,
    )
    
    # Try to delete parent without cascade - should return rejected status
    result = await server._delete_todo_impl(
        task_id=parent_id,
        cascade=False,
        context=mock_context,
    )
    
    assert result["status"] == "rejected"
    assert result["reason"] == "has_dependents"
    assert "depend on it" in result["message"].lower()


# =============================================================================
# Test: Query & Filtering
# =============================================================================

@pytest.mark.asyncio
async def test_list_todos_all(server: TodoServer, mock_context: Dict[str, Any]):
    """Test list all tasks"""
    # Create multiple tasks
    await server.create_todo(title="Task 1", priority="high", context=mock_context)
    await server.create_todo(title="Task 2", priority="low", context=mock_context)
    await server.create_todo(title="Task 3", priority="medium", context=mock_context)
    
    result = await server.list_todos(context=mock_context)
    
    assert result["total_count"] == 3
    assert result["returned_count"] == 3
    assert len(result["tasks"]) == 3


@pytest.mark.asyncio
async def test_list_todos_filter_status(server: TodoServer, mock_context: Dict[str, Any]):
    """Test filter by status"""
    task1 = await server.create_todo(title="Task 1", context=mock_context)
    await server.create_todo(title="Task 2", context=mock_context)
    
    # Update one to in-progress
    await server.update_todo(task1["task_id"], new_status="in-progress", context=mock_context)
    
    # Filter for in-progress
    result = await server.list_todos(
        filter_status=["in-progress"],
        context=mock_context,
    )
    
    assert result["returned_count"] == 1
    assert result["tasks"][0]["status"] == TaskStatus.IN_PROGRESS.value


@pytest.mark.asyncio
async def test_list_todos_filter_priority(server: TodoServer, mock_context: Dict[str, Any]):
    """Test filter by priority"""
    await server.create_todo(title="High", priority="high", context=mock_context)
    await server.create_todo(title="Low", priority="low", context=mock_context)
    await server.create_todo(title="Critical", priority="critical", context=mock_context)
    
    result = await server.list_todos(
        filter_priority=["high", "critical"],
        context=mock_context,
    )
    
    assert result["returned_count"] == 2


@pytest.mark.asyncio
async def test_list_todos_filter_tags(server: TodoServer, mock_context: Dict[str, Any]):
    """Test filter by tags"""
    await server.create_todo(title="Backend", tags=["backend"], context=mock_context)
    await server.create_todo(title="Frontend", tags=["frontend"], context=mock_context)
    await server.create_todo(title="Full Stack", tags=["backend", "frontend"], context=mock_context)
    
    result = await server.list_todos(
        filter_tags=["backend"],
        context=mock_context,
    )
    
    assert result["returned_count"] == 2


@pytest.mark.asyncio
async def test_list_todos_only_unblocked(server: TodoServer, mock_context: Dict[str, Any]):
    """Test filter for unblocked tasks"""
    parent = await server.create_todo(title="Parent", context=mock_context)
    await server.create_todo(
        title="Blocked",
        depends_on=[parent["task_id"]],
        context=mock_context,
    )
    await server.create_todo(title="Unblocked", context=mock_context)
    
    result = await server.list_todos(
        only_unblocked=True,
        context=mock_context,
    )
    
    # Should return parent + unblocked (2 tasks), blocked child excluded
    assert result["returned_count"] == 2


@pytest.mark.asyncio
async def test_list_todos_limit(server: TodoServer, mock_context: Dict[str, Any]):
    """Test result limiting"""
    for i in range(10):
        await server.create_todo(title=f"Task {i}", context=mock_context)
    
    result = await server.list_todos(limit=5, context=mock_context)
    
    assert result["total_count"] == 10
    assert result["returned_count"] == 5
    assert len(result["tasks"]) == 5


# =============================================================================
# Test: Get Task & Summary
# =============================================================================

@pytest.mark.asyncio
async def test_get_todo(server: TodoServer, mock_context: Dict[str, Any]):
    """Test get single task details"""
    created = await server.create_todo(
        title="Test Task",
        description="Test description",
        context=mock_context,
    )
    task_id = created["task_id"]
    
    result = await server.get_todo(task_id=task_id, context=mock_context)
    
    assert result["task"]["task_id"] == task_id
    assert result["task"]["title"] == "Test Task"
    assert "dependency_info" in result


@pytest.mark.asyncio
async def test_get_todo_nonexistent(server: TodoServer, mock_context: Dict[str, Any]):
    """Test get non-existent task"""
    result = await server.get_todo(task_id="nonexistent", context=mock_context)
    
    assert result["status"] == "not_found"
    assert "not found" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_progress_summary(server: TodoServer, mock_context: Dict[str, Any]):
    """Test progress summary statistics"""
    # Create tasks in different states
    task1 = await server.create_todo(title="Task 1", priority="high", context=mock_context)
    task2 = await server.create_todo(title="Task 2", priority="low", context=mock_context)
    await server.create_todo(title="Task 3", priority="high", context=mock_context)
    
    await server.update_todo(task1["task_id"], new_status="in-progress", progress=50, context=mock_context)
    await server.update_todo(task2["task_id"], new_status="in-progress", context=mock_context)
    await server.update_todo(task2["task_id"], new_status="completed", context=mock_context)
    
    result = await server.get_progress_summary(context=mock_context)
    
    assert result["total_tasks"] == 3
    assert result["by_status"]["completed"] == 1
    assert result["by_status"]["in-progress"] == 1
    assert result["by_status"]["not-started"] == 1
    assert result["by_priority"]["high"] == 2
    assert result["by_priority"]["low"] == 1
    assert result["overall_progress"] > 0


# =============================================================================
# Test: Multi-Mode todo() Tool
# =============================================================================

@pytest.mark.asyncio
async def test_todo_mode_create(server: TodoServer, mock_context: Dict[str, Any]):
    """Test todo() CREATE mode"""
    result = await server.call("todo", {
        "operation": "create",
        "title": "New Task",
        "description": "Description",
        "priority": "high",
        "context": mock_context,
    })
    
    assert result["status"] == "created"
    assert "task_id" in result


@pytest.mark.asyncio
async def test_todo_mode_update(server: TodoServer, mock_context: Dict[str, Any]):
    """Test todo() UPDATE mode"""
    created = await server.call("todo", {"operation": "create", "title": "Task", "context": mock_context})
    task_id = created["task_id"]
    
    result = await server.call("todo", {
        "operation": "update",
        "task_id": task_id,
        "status": "in-progress",
        "progress": 50,
        "context": mock_context,
    })
    
    assert result["status"] == "updated"
    assert result["task"]["progress"] == 50


@pytest.mark.asyncio
async def test_todo_mode_list(server: TodoServer, mock_context: Dict[str, Any]):
    """Test todo() LIST mode"""
    await server.call("todo", {"operation": "create", "title": "Task 1", "priority": "high", "context": mock_context})
    await server.call("todo", {"operation": "create", "title": "Task 2", "priority": "low", "context": mock_context})
    
    result = await server.call("todo", {
        "operation": "list",
        "filter_priority": ["high"],
        "context": mock_context,
    })
    
    assert "tasks" in result
    assert result["returned_count"] == 1


@pytest.mark.asyncio
async def test_todo_mode_get(server: TodoServer, mock_context: Dict[str, Any]):
    """Test todo() GET mode"""
    created = await server.call("todo", {"operation": "create", "title": "Task", "context": mock_context})
    task_id = created["task_id"]
    
    result = await server.call("todo", {"operation": "get", "task_id": task_id, "context": mock_context})
    
    assert result["task"]["task_id"] == task_id


@pytest.mark.asyncio
async def test_todo_mode_summary(server: TodoServer, mock_context: Dict[str, Any]):
    """Test todo() SUMMARY mode"""
    await server.call("todo", {"operation": "create", "title": "Task 1", "context": mock_context})
    await server.call("todo", {"operation": "create", "title": "Task 2", "context": mock_context})
    
    result = await server.call("todo", {"operation": "summary", "context": mock_context})
    
    assert "total_tasks" in result
    assert result["total_tasks"] == 2


@pytest.mark.asyncio
async def test_todo_invalid_args(server: TodoServer, mock_context: Dict[str, Any]):
    """Test todo() with invalid arguments"""
    # Missing operation should raise ValidationError
    with pytest.raises(Exception) as exc_info:
        await server.call("todo", {"context": mock_context})
    assert "operation" in str(exc_info.value).lower()


# =============================================================================
# Test: Duplicate Detection
# =============================================================================

@pytest.mark.asyncio
async def test_duplicate_exact_title(server: TodoServer, mock_context: Dict[str, Any]):
    """Test exact duplicate title returns existing task"""
    # Create first task
    result1 = await server.create_todo(
        title="Implement authentication",
        priority="high",
        context=mock_context,
    )
    task_id1 = result1["task_id"]
    
    # Try to create duplicate (exact same title)
    result2 = await server.create_todo(
        title="Implement authentication",
        priority="medium",  # Different priority but same title
        context=mock_context,
    )
    
    # Should return existing task
    assert result2["status"] == "exists"
    assert result2["reason"] == "duplicate_title"
    assert result2["task_id"] == task_id1
    assert result2["similarity"] >= 0.95


@pytest.mark.asyncio
async def test_duplicate_similar_title(server: TodoServer, mock_context: Dict[str, Any]):
    """Test very similar titles (95%+) return existing task"""
    # Create first task
    result1 = await server.create_todo(
        title="Implement user authentication",
        context=mock_context,
    )
    task_id1 = result1["task_id"]
    
    # Try with very similar title (one word different)
    result2 = await server.create_todo(
        title="Implement user authentification",  # Typo: authentification vs authentication
        context=mock_context,
    )
    
    # Should return existing task (high similarity)
    assert result2["status"] == "exists"
    assert result2["task_id"] == task_id1


@pytest.mark.asyncio
async def test_allow_duplicates_flag(server: TodoServer, mock_context: Dict[str, Any]):
    """Test allow_duplicates=True creates new task despite similarity"""
    # Create first task
    result1 = await server.create_todo(
        title="Write tests",
        context=mock_context,
    )
    task_id1 = result1["task_id"]
    
    # Create duplicate with allow_duplicates=True
    result2 = await server.create_todo(
        title="Write tests",
        allow_duplicates=True,
        context=mock_context,
    )
    
    # Should create new task (not return existing)
    assert result2["status"] == "created"
    assert result2["task_id"] != task_id1


@pytest.mark.asyncio
async def test_idempotency_key(server: TodoServer, mock_context: Dict[str, Any]):
    """Test idempotency key prevents duplicate creation"""
    # Create task with idempotency key
    result1 = await server.create_todo(
        title="Deploy to production",
        idempotency_key="deploy-v1.2.3",
        context=mock_context,
    )
    task_id1 = result1["task_id"]
    
    # Retry with same idempotency key (simulates retry)
    result2 = await server.create_todo(
        title="Deploy to production (retry)",  # Different title
        idempotency_key="deploy-v1.2.3",  # Same key
        context=mock_context,
    )
    
    # Should return existing task (idempotency key match)
    assert result2["status"] == "exists"
    assert result2["reason"] == "idempotency_key"
    assert result2["task_id"] == task_id1


@pytest.mark.asyncio
async def test_duplicate_ignores_completed(server: TodoServer, mock_context: Dict[str, Any]):
    """Test duplicate detection ignores completed tasks"""
    # Create and complete task
    result1 = await server.create_todo(
        title="Fix bug #123",
        context=mock_context,
    )
    task_id1 = result1["task_id"]
    await server.update_todo(
        task_id=task_id1,
        new_status="completed",
        context=mock_context,
    )
    
    # Create new task with same title (should create, not return completed)
    result2 = await server.create_todo(
        title="Fix bug #123",
        context=mock_context,
    )
    
    # Should create new task (completed tasks excluded from duplicate check)
    assert result2["status"] == "created"
    assert result2["task_id"] != task_id1


@pytest.mark.asyncio
async def test_similar_but_different_tasks(server: TodoServer, mock_context: Dict[str, Any]):
    """Test tasks with <80% similarity create separate tasks"""
    # Create first task
    await server.create_todo(
        title="Implement JWT authentication",
        context=mock_context,
    )
    
    # Create different task (< 80% similar)
    result2 = await server.create_todo(
        title="Write unit tests",
        context=mock_context,
    )
    
    # Should create new task (different enough)
    assert result2["status"] == "created"


# =============================================================================
# Test: Persistence
# =============================================================================

@pytest.mark.asyncio
async def test_session_persistence(server: TodoServer, mock_context: Dict[str, Any]):
    """Test tasks persist to JSON file"""
    await server.create_todo(title="Persisted Task", context=mock_context)
    
    # Check file exists
    session_id = mock_context["session_id"]
    file_path = server._get_storage_path(session_id)
    assert file_path.exists()
    
    # Load and verify
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    assert data["session_id"] == session_id
    assert len(data["tasks"]) == 1


@pytest.mark.asyncio
async def test_session_reload(
    temp_storage: Path,
    mock_system_config: MagicMock,
    mock_server_config: MagicMock,
    mock_context: Dict[str, Any],
):
    """Test session reloads from disk"""
    # Create tasks with first server instance
    server1 = TodoServer(
        name="todo",
        system_config=mock_system_config,
        server_config=mock_server_config,
    )
    await server1.create_todo(title="Task 1", context=mock_context)
    await server1.create_todo(title="Task 2", context=mock_context)
    
    # Create new server instance (simulates restart)
    server2 = TodoServer(
        name="todo",
        system_config=mock_system_config,
        server_config=mock_server_config,
    )
    
    # Load tasks
    result = await server2.list_todos(context=mock_context)
    
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_corrupt_file_handling(server: TodoServer, mock_context: Dict[str, Any]):
    """Test handling of corrupt JSON file"""
    # Create corrupt file
    session_id = mock_context["session_id"]
    file_path = server._get_storage_path(session_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(file_path, "w") as f:
        f.write("{ corrupt json")
    
    # Should raise StorageError
    with pytest.raises(StorageError):
        server._load_session(session_id)


# =============================================================================
# Test: Edge Cases
# =============================================================================

@pytest.mark.asyncio
async def test_task_id_generation_uniqueness(server: TodoServer, mock_context: Dict[str, Any]):
    """Test task IDs are unique and sequential"""
    task_ids = []
    for i in range(10):
        result = await server.create_todo(title=f"Task {i}", context=mock_context)
        task_ids.append(result["task_id"])
    
    # All unique
    assert len(set(task_ids)) == 10
    
    # Sequential pattern
    assert task_ids[0] == "task_001"
    assert task_ids[9] == "task_010"


@pytest.mark.asyncio
async def test_session_isolation(server: TodoServer):
    """Test tasks are isolated per session"""
    context1 = {"session_id": "session_001"}
    context2 = {"session_id": "session_002"}
    
    # Create tasks in different sessions
    await server.create_todo(title="Session 1 Task", context=context1)
    await server.create_todo(title="Session 2 Task", context=context2)
    
    # Verify isolation
    result1 = await server.list_todos(context=context1)
    result2 = await server.list_todos(context=context2)
    
    assert result1["total_count"] == 1
    assert result2["total_count"] == 1
    assert result1["tasks"][0]["title"] != result2["tasks"][0]["title"]


@pytest.mark.asyncio
async def test_empty_session(server: TodoServer, mock_context: Dict[str, Any]):
    """Test operations on empty session"""
    result = await server.list_todos(context=mock_context)
    assert result["total_count"] == 0
    
    summary = await server.get_progress_summary(context=mock_context)
    assert summary["total_tasks"] == 0


@pytest.mark.asyncio
async def test_unicode_handling(server: TodoServer, mock_context: Dict[str, Any]):
    """Test Unicode in task titles and descriptions"""
    result = await server.create_todo(
        title="Aufgabe mit Umlauten: äöü ß",
        description="Emoji test: 🚀 ✅ 📝",
        tags=["日本語", "中文"],
        context=mock_context,
    )
    
    assert "Umlauten" in result["task"]["title"]
    assert "🚀" in result["task"]["description"]
    assert "日本語" in result["task"]["tags"]


@pytest.mark.asyncio
async def test_long_text_fields(server: TodoServer, mock_context: Dict[str, Any]):
    """Test text field length limits"""
    # Title max 200 chars
    long_title = "x" * 200
    result = await server.create_todo(title=long_title, context=mock_context)
    assert len(result["task"]["title"]) == 200
    
    # Description max 2000 chars
    long_desc = "x" * 2000
    result = await server.create_todo(
        title="Test",
        description=long_desc,
        context=mock_context,
    )
    assert len(result["task"]["description"]) == 2000


@pytest.mark.asyncio
async def test_multiple_dependencies(server: TodoServer, mock_context: Dict[str, Any]):
    """Test task with multiple dependencies"""
    task1 = await server.create_todo(title="Dep 1", context=mock_context)
    task2 = await server.create_todo(title="Dep 2", context=mock_context)
    task3 = await server.create_todo(title="Dep 3", context=mock_context)
    
    # Create task depending on all three
    result = await server.create_todo(
        title="Multi Dep",
        depends_on=[task1["task_id"], task2["task_id"], task3["task_id"]],
        context=mock_context,
    )
    
    assert len(result["task"]["depends_on"]) == 3
    assert result["is_blocked"] is True


@pytest.mark.asyncio
async def test_task_lifecycle_complete_flow(server: TodoServer, mock_context: Dict[str, Any]):
    """Test complete task lifecycle"""
    # Create
    created = await server.create_todo(
        title="Lifecycle Test",
        priority="high",
        context=mock_context,
    )
    task_id = created["task_id"]
    
    assert created["task"]["status"] == TaskStatus.NOT_STARTED.value
    
    # Start work
    updated = await server.update_todo(
        task_id=task_id,
        new_status="in-progress",
        progress=25,
        add_note="Started implementation",
        context=mock_context,
    )
    
    assert updated["task"]["status"] == TaskStatus.IN_PROGRESS.value
    assert updated["task"]["started_at"] is not None
    
    # Update progress
    updated = await server.update_todo(
        task_id=task_id,
        progress=75,
        add_note="Almost done",
        context=mock_context,
    )
    
    assert updated["task"]["progress"] == 75
    
    # Complete
    updated = await server.update_todo(
        task_id=task_id,
        new_status="completed",
        context=mock_context,
    )
    
    assert updated["task"]["status"] == TaskStatus.COMPLETED.value
    assert updated["task"]["progress"] == 100
    assert updated["task"]["completed_at"] is not None
    
    # Status transitions are unrestricted - can move from completed back to in-progress
    result = await server.update_todo(
        task_id=task_id,
        new_status="in-progress",
        context=mock_context,
    )
    assert result["task"]["status"] == TaskStatus.IN_PROGRESS.value


# =============================================================================
# Coverage Helpers
# =============================================================================

def test_task_model_defaults():
    """Test Task model default values"""
    task = Task(task_id="test_001", title="Test")
    
    assert task.status == TaskStatus.NOT_STARTED
    assert task.priority == TaskPriority.MEDIUM
    assert task.progress == 0
    assert task.tags == []
    assert task.depends_on == []
    assert task.blocks == []
    assert task.notes == []


def test_task_collection_model():
    """Test TaskCollection model"""
    collection = TaskCollection(session_id="test")
    
    assert collection.tasks == {}
    assert collection.metadata == {}


def test_exception_hierarchy():
    """Test exception classes"""
    base = TodoError("base")
    assert isinstance(base, Exception)
    
    val = ValidationError("validation")
    assert isinstance(val, TodoError)
    
    dep = DependencyError("dependency")
    assert isinstance(dep, TodoError)
    
    store = StorageError("storage")
    assert isinstance(store, TodoError)


# =============================================================================
# Performance Tests (Optional)
# =============================================================================

@pytest.mark.asyncio
async def test_large_session_performance(server: TodoServer, mock_context: Dict[str, Any]):
    """Test performance with many tasks (50+)"""
    # Create 50 tasks
    for i in range(50):
        await server.create_todo(
            title=f"Task {i}",
            priority="medium" if i % 2 == 0 else "high",
            tags=[f"tag_{i % 5}"],
            context=mock_context,
        )
    
    # List all
    result = await server.list_todos(context=mock_context)
    assert result["total_count"] == 50
    
    # Filter
    filtered = await server.list_todos(
        filter_priority=["high"],
        context=mock_context,
    )
    assert filtered["returned_count"] == 25


# =============================================================================
# Hook Integration Tests
# =============================================================================

@pytest.mark.asyncio
async def test_hook_inject_tasks_into_prompt(server: TodoServer, mock_context: Dict[str, Any]):
    """Test that hook injects tasks into system prompt"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    # Create some tasks
    await server.create_todo(
        title="Task 1: Implement feature",
        priority="high",
        context=mock_context,
    )
    
    result2 = await server.create_todo(
        title="Task 2: Write tests",
        priority="medium",
        context=mock_context,
    )
    
    # Update one to in-progress
    await server.update_todo(
        task_id=result2["task_id"],
        new_status="in-progress",
        context=mock_context,
    )
    
    # Create hook context
    messages = [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="Hello"),
    ]
    
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_001",
        messages=messages,
        session_id=mock_context["session_id"],
        agent=None,
    )
    
    # Execute hook
    result = await server.on_pre_llm_call(hook_context)
    
    assert result.success is True
    assert result.modified is True
    assert len(result.context.messages) == 3  # system + injected + user
    
    # Check injected message
    injected_msg = result.context.messages[1]
    assert injected_msg.role == "system"
    assert "TODO Tool Available" in injected_msg.content
    assert "Task 1: Implement feature" in injected_msg.content
    assert "Task 2: Write tests" in injected_msg.content
    assert "HIGH" in injected_msg.content


@pytest.mark.asyncio
async def test_hook_no_tasks_injects_reminder(server: TodoServer, mock_context: Dict[str, Any]):
    """Test that hook injects TODO tool reminder even when no tasks exist"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    messages = [
        ChatMessage(role="system", content="You are a helpful assistant."),
        ChatMessage(role="user", content="Hello"),
    ]
    
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_002",
        messages=messages,
        session_id=mock_context["session_id"],
        agent=None,
    )
    
    result = await server.on_pre_llm_call(hook_context)
    
    # Now we ALWAYS inject a reminder, even when no tasks exist
    assert result.success is True
    assert result.modified is True  # Changed: now injects TODO tool reminder
    assert len(result.context.messages) == 3  # Changed: system + reminder + user
    
    # Verify reminder was injected
    injected_msg = result.context.messages[1]
    assert "TODO Tool Available" in injected_msg.content
    assert "todo()" in injected_msg.content


@pytest.mark.asyncio
async def test_hook_filter_status_config(server: TodoServer, mock_context: Dict[str, Any]):
    """Test that hook respects filter_status config"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    # Create tasks with different statuses
    await server.create_todo(title="Not started", context=mock_context)
    task2 = await server.create_todo(title="In progress", context=mock_context)
    task3 = await server.create_todo(title="Completed", context=mock_context)
    
    await server.update_todo(task2["task_id"], new_status="in-progress", context=mock_context)
    await server.update_todo(task3["task_id"], new_status="completed", context=mock_context)
    
    # Hook should only show not-started and in-progress (default filter)
    messages = [ChatMessage(role="system", content="Test")]
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_003",
        messages=messages,
        session_id=mock_context["session_id"],
        agent=None,
    )
    
    result = await server.on_pre_llm_call(hook_context)
    
    assert result.modified is True
    injected = result.context.messages[1].content
    assert "Not started" in injected
    assert "In progress" in injected
    assert "Completed" not in injected  # Filtered out


@pytest.mark.asyncio
async def test_hook_config_from_schema(server: TodoServer):
    """Test that hook config is loaded from schema.yaml"""
    # Server should have loaded config from schema.yaml
    assert hasattr(server, 'config')
    
    # Check default values from schema.yaml
    assert server.config.get("max_tasks") == 20
    assert server.config.get("filter_status") == ["not-started", "in-progress", "blocked"]
    assert server.config.get("include_completed") is False
    assert server.config.get("format") == "markdown"


@pytest.mark.asyncio
async def test_hook_no_session_id_skips(server: TodoServer):
    """Test that hook skips when no session_id in context"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    messages = [ChatMessage(role="system", content="Test")]
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_004",
        messages=messages,
        session_id=None,  # No session
        agent=None,
    )
    
    result = await server.on_pre_llm_call(hook_context)
    
    assert result.success is True
    assert result.modified is False


@pytest.mark.asyncio
async def test_hook_format_markdown(server: TodoServer, mock_context: Dict[str, Any]):
    """Test markdown formatting of injected tasks"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    # Create task with dependencies
    task1 = await server.create_todo(title="Parent task", priority="high", context=mock_context)
    await server.create_todo(
        title="Child task",
        priority="low",
        depends_on=[task1["task_id"]],
        context=mock_context,
    )
    
    messages = [ChatMessage(role="system", content="Test")]
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_005",
        messages=messages,
        session_id=mock_context["session_id"],
        agent=None,
    )
    
    result = await server.on_pre_llm_call(hook_context)
    injected = result.context.messages[1].content
    
    # Check markdown formatting
    assert "## TODO Tool Available" in injected
    assert "**task_" in injected  # Bold task IDs


@pytest.mark.asyncio
async def test_hook_session_isolation(server: TodoServer, mock_context: Dict[str, Any]):
    """Test that hook only injects tasks from current session"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    # Create task in session 1
    await server.create_todo(title="Session 1 task", context={"session_id": "session_1"})
    
    # Create task in session 2
    await server.create_todo(title="Session 2 task", context={"session_id": "session_2"})
    
    # Hook for session 1
    messages = [ChatMessage(role="system", content="Test")]
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_006",
        messages=messages,
        session_id="session_1",
        agent=None,
    )
    
    result = await server.on_pre_llm_call(hook_context)
    injected = result.context.messages[1].content
    
    assert "Session 1 task" in injected
    assert "Session 2 task" not in injected  # Different session


@pytest.mark.asyncio
async def test_hook_max_tasks_limit(server: TodoServer, mock_context: Dict[str, Any]):
    """Test that hook respects max_tasks config"""
    from agent_system.hooks.plugin_hook import HookContext, HookType
    from agent_system.llm.models import ChatMessage
    
    # Create more than max_tasks (20) tasks
    for i in range(25):
        await server.create_todo(title=f"Task {i}", context=mock_context)
    
    messages = [ChatMessage(role="system", content="Test")]
    hook_context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_007",
        messages=messages,
        session_id=mock_context["session_id"],
        agent=None,
    )
    
    result = await server.on_pre_llm_call(hook_context)
    injected = result.context.messages[1].content
    
    # Count task entries (should be limited to 20)
    task_count = injected.count("**task_")
    assert task_count <= 20



if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=plugins.todo.server", "--cov-report=html"])
