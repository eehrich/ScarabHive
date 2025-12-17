"""Tests for Sequential Thinking Plugin.

This test suite covers:
- Basic thought sequences (linear reasoning)
- Branching (exploring alternative paths)
- Revisions (refining previous thoughts)
- Session management (creation, TTL, cleanup)
- Edge cases (empty content, invalid params, memory limits)
- Integration scenarios
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock

from agent_system.config.models import AgentSystemConfig, MCPConfig
from plugins.sequential_thinking.server import SequentialThinkingServer


# ===== Fixtures =====

@pytest.fixture
def system_config():
    """Provide test system config."""
    return AgentSystemConfig()


@pytest.fixture
def mcp_config():
    """Provide test MCP config with default settings."""
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.max_history_size = 100
    config.session_ttl_seconds = 3600
    config.enable_branching = True
    config.enable_revisions = True
    config.max_summary_thoughts = 10
    return config


@pytest.fixture
def mcp_config_no_branching():
    """Provide MCP config with branching disabled."""
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.max_history_size = 100
    config.session_ttl_seconds = 3600
    config.enable_branching = False
    config.enable_revisions = True
    config.max_summary_thoughts = 10
    return config


@pytest.fixture
def mcp_config_no_revisions():
    """Provide MCP config with revisions disabled."""
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.max_history_size = 100
    config.session_ttl_seconds = 3600
    config.enable_branching = True
    config.enable_revisions = False
    config.max_summary_thoughts = 10
    return config


@pytest.fixture
def server(system_config, mcp_config):
    """Provide SequentialThinkingServer instance."""
    return SequentialThinkingServer("sequential_thinking", system_config, mcp_config)


@pytest.fixture
def mock_status():
    """Provide mock status object."""
    status = AsyncMock()
    status.start = AsyncMock()
    status.progress = AsyncMock()
    status.end = AsyncMock()
    status.error = AsyncMock()
    return status


# ===== Initialization Tests =====

def test_init_default_config(system_config, mcp_config):
    """Test server initialization with default config."""
    server = SequentialThinkingServer("sequential_thinking", system_config, mcp_config)
    
    assert server.name == "sequential_thinking"
    assert server.max_history_size == 100
    assert server.session_ttl_seconds == 3600
    assert server.enable_branching is True
    assert server.enable_revisions is True
    assert server.max_summary_thoughts == 10
    assert len(server._sessions) == 0


def test_init_custom_config(system_config):
    """Test server initialization with custom config."""
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.max_history_size = 50
    config.session_ttl_seconds = 1800
    config.enable_branching = False
    config.enable_revisions = False
    config.max_summary_thoughts = 5
    
    server = SequentialThinkingServer("test", system_config, config)
    
    assert server.max_history_size == 50
    assert server.session_ttl_seconds == 1800
    assert server.enable_branching is False
    assert server.enable_revisions is False
    assert server.max_summary_thoughts == 5


# ===== Basic Operations Tests =====

@pytest.mark.asyncio
async def test_basic_thought_sequence(server, mock_status):
    """Test adding basic linear thought sequence."""
    # Thought 1
    result1 = await server.execute({
        "thought": "First, analyze the problem requirements",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result1["status"] == "success"
    assert "session_id" in result1
    assert result1["current_thought_number"] == 1
    assert result1["total_thoughts_estimate"] == 3
    assert result1["next_thought_needed"] is True
    
    session_id = result1["session_id"]
    
    # Thought 2
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Next, design the solution architecture",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result2["status"] == "success"
    assert result2["session_id"] == session_id
    assert result2["current_thought_number"] == 2
    
    # Thought 3 (final)
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "Finally, implement and test the solution",
        "thought_number": 3,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result3["status"] == "success"
    assert result3["next_thought_needed"] is False
    
    # Verify session state
    session = server._sessions[session_id]
    assert len(session.thoughts) == 3
    assert session.actual_thoughts == 3


@pytest.mark.asyncio
async def test_adaptive_complexity(server, mock_status):
    """Test adjusting total_thoughts estimate mid-reasoning."""
    result1 = await server.execute({
        "thought": "Start with initial estimate of 3 thoughts",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    assert result1["total_thoughts_estimate"] == 3
    
    # Realize we need more thoughts
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Actually, this is more complex than expected",
        "thought_number": 2,
        "total_thoughts": 7,  # Increased from 3
        "needs_more_thoughts": True,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result2["status"] == "success"
    assert result2["total_thoughts_estimate"] == 7
    
    # Verify status.progress was called for complexity adjustment
    mock_status.progress.assert_any_call("Adjusting complexity: 3 → 7 thoughts")


@pytest.mark.asyncio
async def test_auto_session_creation(server, mock_status):
    """Test automatic session creation when session_id is omitted."""
    result1 = await server.execute({
        "thought": "First thought without session_id",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result1["status"] == "success"
    assert "session_id" in result1
    
    session_id1 = result1["session_id"]
    
    # Another thought without session_id creates new session
    result2 = await server.execute({
        "thought": "Another first thought",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id2 = result2["session_id"]
    
    assert session_id1 != session_id2
    assert len(server._sessions) == 2


# ===== Branching Tests =====

@pytest.mark.asyncio
async def test_create_branch(server, mock_status):
    """Test creating branch to explore alternative."""
    # Create main branch thoughts
    result1 = await server.execute({
        "thought": "Main approach: Use JWT for authentication",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Implement token generation service",
        "thought_number": 2,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Create branch from thought 1
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "Alternative: What if we use OAuth2 instead?",
        "thought_number": 3,
        "total_thoughts": 5,
        "branch_from_thought": 1,
        "branch_id": "oauth_alternative",
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result3["status"] == "success"
    assert result3["branch"] == "oauth_alternative"
    
    # Verify branch creation
    session = server._sessions[session_id]
    assert "oauth_alternative" in session.branches
    assert session.current_branch == "oauth_alternative"
    assert session.branches["oauth_alternative"].parent_branch == "main"
    assert session.branches["oauth_alternative"].branched_from_thought == 1
    
    # Verify status.progress was called for branch creation
    mock_status.progress.assert_any_call(
        "Creating branch 'oauth_alternative' from thought 1"
    )


@pytest.mark.asyncio
async def test_multiple_branches(server, mock_status):
    """Test creating multiple branches."""
    result = await server.execute({
        "thought": "Root thought",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Branch 1
    await server.execute({
        "session_id": session_id,
        "thought": "Branch 1: Approach A",
        "thought_number": 2,
        "total_thoughts": 5,
        "branch_from_thought": 1,
        "branch_id": "approach_a",
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Branch 2
    await server.execute({
        "session_id": session_id,
        "thought": "Branch 2: Approach B",
        "thought_number": 3,
        "total_thoughts": 5,
        "branch_from_thought": 1,
        "branch_id": "approach_b",
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session = server._sessions[session_id]
    assert len(session.branches) == 3  # main + 2 branches
    assert "approach_a" in session.branches
    assert "approach_b" in session.branches


@pytest.mark.asyncio
async def test_branching_disabled(system_config, mcp_config_no_branching, mock_status):
    """Test branching is rejected when disabled."""
    server = SequentialThinkingServer("test", system_config, mcp_config_no_branching)
    
    result = await server.execute({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Attempt to branch
    result_branch = await server.execute({
        "session_id": session_id,
        "thought": "Branch attempt",
        "thought_number": 2,
        "total_thoughts": 3,
        "branch_from_thought": 1,
        "branch_id": "test_branch",
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result_branch["status"] == "error"
    assert "Branching is disabled" in result_branch["error"]


# ===== Revision Tests =====

@pytest.mark.asyncio
async def test_revise_thought(server, mock_status):
    """Test revising previous thought."""
    result1 = await server.execute({
        "thought": "Initial thought: Use PostgreSQL",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Continue with schema design",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Revise thought 1
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "Correction: MongoDB is better for this use case",
        "thought_number": 1,
        "total_thoughts": 3,
        "is_revision": True,
        "revises_thought": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result3["status"] == "success"
    
    # Verify revision tracking
    session = server._sessions[session_id]
    revised_thought = [t for t in session.thoughts if t.number == 1 and t.is_revision][0]
    assert revised_thought.is_revision is True
    assert revised_thought.revises_thought == 1
    assert len(revised_thought.revision_history) > 0
    
    # Verify status message
    mock_status.progress.assert_any_call("Revising thought 1 with new insights")


@pytest.mark.asyncio
async def test_revision_history_tracking(server, mock_status):
    """Test revision history is properly tracked."""
    result = await server.execute({
        "thought": "Original thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # First revision
    await server.execute({
        "session_id": session_id,
        "thought": "First revision",
        "thought_number": 1,
        "total_thoughts": 1,
        "is_revision": True,
        "revises_thought": 1,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Second revision
    await server.execute({
        "session_id": session_id,
        "thought": "Second revision",
        "thought_number": 1,
        "total_thoughts": 1,
        "is_revision": True,
        "revises_thought": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session = server._sessions[session_id]
    latest_revision = [t for t in session.thoughts if t.number == 1][-1]
    
    # Should have 2 entries in revision history
    assert len(latest_revision.revision_history) == 2


@pytest.mark.asyncio
async def test_revisions_disabled(system_config, mcp_config_no_revisions, mock_status):
    """Test revisions are rejected when disabled."""
    server = SequentialThinkingServer("test", system_config, mcp_config_no_revisions)
    
    result = await server.execute({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Attempt to revise
    result_revision = await server.execute({
        "session_id": session_id,
        "thought": "Revision attempt",
        "thought_number": 1,
        "total_thoughts": 1,
        "is_revision": True,
        "revises_thought": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result_revision["status"] == "error"
    assert "Revisions are disabled" in result_revision["error"]


# ===== Session Management Tests =====

@pytest.mark.asyncio
async def test_session_ttl_cleanup(system_config, mock_status):
    """Test expired sessions are cleaned up."""
    # Use very short TTL for testing
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.session_ttl_seconds = 1  # 1 second
    
    server = SequentialThinkingServer("test", system_config, config)
    
    result = await server.execute({
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    assert session_id in server._sessions
    
    # Wait for TTL to expire
    await asyncio.sleep(1.5)
    
    # Trigger cleanup by adding new thought
    await server.execute({
        "thought": "New thought (triggers cleanup)",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Old session should be cleaned up
    assert session_id not in server._sessions


@pytest.mark.asyncio
async def test_session_last_accessed_update(server, mock_status):
    """Test last_accessed timestamp is updated on access."""
    result1 = await server.execute({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    session = server._sessions[session_id]
    first_access = session.last_accessed
    
    await asyncio.sleep(0.1)
    
    # Access again
    await server.execute({
        "session_id": session_id,
        "thought": "Second thought",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    second_access = server._sessions[session_id].last_accessed
    assert second_access > first_access


@pytest.mark.asyncio
async def test_clear_specific_session(server, mock_status):
    """Test clearing specific session."""
    # Create 2 sessions
    result1 = await server.execute({
        "thought": "Session 1",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    result2 = await server.execute({
        "thought": "Session 2",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id1 = result1["session_id"]
    session_id2 = result2["session_id"]
    
    assert len(server._sessions) == 2
    
    # Clear session 1
    clear_result = await server.clear_history({
        "session_id": session_id1,
        "_status": mock_status
    })
    
    assert clear_result["status"] == "success"
    assert clear_result["cleared_sessions"] == 1
    assert session_id1 not in server._sessions
    assert session_id2 in server._sessions


@pytest.mark.asyncio
async def test_clear_all_sessions(server, mock_status):
    """Test clearing all sessions."""
    # Create 3 sessions
    for i in range(3):
        await server.execute({
            "thought": f"Session {i}",
            "thought_number": 1,
            "total_thoughts": 1,
            "next_thought_needed": False,
            "_status": mock_status
        })
    
    assert len(server._sessions) == 3
    
    # Clear all
    clear_result = await server.clear_history({
        "_status": mock_status
    })
    
    assert clear_result["status"] == "success"
    assert clear_result["cleared_sessions"] == 3
    assert len(server._sessions) == 0


@pytest.mark.asyncio
async def test_clear_nonexistent_session(server, mock_status):
    """Test clearing nonexistent session returns error."""
    result = await server.clear_history({
        "session_id": "nonexistent-session-id",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "not found" in result["error"]


# ===== Summary Tests =====

@pytest.mark.asyncio
async def test_get_thought_summary(server, mock_status):
    """Test getting thought summary."""
    # Create session with thoughts
    result = await server.execute({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    await server.execute({
        "session_id": session_id,
        "thought": "Second thought",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    await server.execute({
        "session_id": session_id,
        "thought": "Third thought",
        "thought_number": 3,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Get summary
    summary = await server.get_summary({
        "session_id": session_id,
        "_status": mock_status
    })
    
    assert summary["status"] == "success"
    assert summary["session_id"] == session_id
    assert summary["total_thoughts"] == 3
    assert len(summary["thoughts"]) == 3
    assert summary["current_branch"] == "main"
    assert summary["branches"] is not None


@pytest.mark.asyncio
async def test_summary_max_thoughts_limit(server, mock_status):
    """Test summary respects max_thoughts limit."""
    # Create session with 15 thoughts
    result = await server.execute({
        "thought": "Thought 1",
        "thought_number": 1,
        "total_thoughts": 15,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    for i in range(2, 16):
        await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 15,
            "next_thought_needed": i < 15,
            "_status": mock_status
        })
    
    # Get summary with limit of 5
    summary = await server.get_summary({
        "session_id": session_id,
        "max_thoughts": 5,
        "_status": mock_status
    })
    
    assert summary["status"] == "success"
    assert summary["total_thoughts"] == 15
    assert len(summary["thoughts"]) == 5  # Limited to 5
    # Should return last 5 thoughts (11-15)
    assert summary["thoughts"][0]["number"] == 11


@pytest.mark.asyncio
async def test_summary_without_branches(server, mock_status):
    """Test summary can exclude branch info."""
    result = await server.execute({
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    summary = await server.get_summary({
        "session_id": session_id,
        "include_branches": False,
        "_status": mock_status
    })
    
    assert summary["status"] == "success"
    assert summary["branches"] is None


@pytest.mark.asyncio
async def test_summary_nonexistent_session(server, mock_status):
    """Test summary for nonexistent session returns error."""
    result = await server.get_summary({
        "session_id": "nonexistent",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "not found" in result["error"]


# ===== Memory Management Tests =====

@pytest.mark.asyncio
async def test_memory_limit_enforcement(system_config, mock_status):
    """Test memory limit removes oldest thoughts."""
    # Use small limit for testing
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.max_history_size = 5
    
    server = SequentialThinkingServer("test", system_config, config)
    
    # Add 10 thoughts (exceeds limit of 5)
    result = await server.execute({
        "thought": "Thought 1",
        "thought_number": 1,
        "total_thoughts": 10,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    for i in range(2, 11):
        await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 10,
            "next_thought_needed": i < 10,
            "_status": mock_status
        })
    
    session = server._sessions[session_id]
    
    # Should only have 5 thoughts (newest ones)
    assert len(session.thoughts) == 5
    # Should have thoughts 6-10
    assert session.thoughts[0].number == 6
    assert session.thoughts[-1].number == 10


@pytest.mark.asyncio
async def test_memory_warning_threshold(server, mock_status):
    """Test warning when approaching memory limit."""
    # Add thoughts up to 85% of limit
    result = await server.execute({
        "thought": "Start",
        "thought_number": 1,
        "total_thoughts": 100,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Add 84 more thoughts (total 85, which is 85% of 100 limit)
    for i in range(2, 86):
        result = await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 100,
            "next_thought_needed": True,
            "_status": mock_status
        })
    
    # Last result should have warning in status
    # Check that progress was called with warning level
    warning_calls = [
        call for call in mock_status.progress.call_args_list
        if "Memory usage" in str(call)
    ]
    assert len(warning_calls) > 0


# ===== Edge Cases & Validation Tests =====

@pytest.mark.asyncio
async def test_empty_thought_content(server, mock_status):
    """Test empty thought content is rejected."""
    result = await server.execute({
        "thought": "",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "cannot be empty" in result["error"]


@pytest.mark.asyncio
async def test_invalid_thought_number(server, mock_status):
    """Test invalid thought number is rejected."""
    result = await server.execute({
        "thought": "Test",
        "thought_number": 0,  # Invalid (must be >= 1)
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "must be >= 1" in result["error"]


@pytest.mark.asyncio
async def test_invalid_total_thoughts(server, mock_status):
    """Test invalid total_thoughts is rejected."""
    result = await server.execute({
        "thought": "Test",
        "thought_number": 1,
        "total_thoughts": 0,  # Invalid
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "must be >= 1" in result["error"]


@pytest.mark.asyncio
async def test_whitespace_only_thought(server, mock_status):
    """Test whitespace-only thought is rejected."""
    result = await server.execute({
        "thought": "   \n\t   ",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "cannot be empty" in result["error"]


@pytest.mark.asyncio
async def test_duplicate_branch_id(server, mock_status):
    """Test creating branch with duplicate ID fails."""
    result = await server.execute({
        "thought": "Root",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Create first branch
    await server.execute({
        "session_id": session_id,
        "thought": "Branch A",
        "thought_number": 2,
        "total_thoughts": 3,
        "branch_from_thought": 1,
        "branch_id": "test_branch",
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Attempt to create duplicate branch
    result_dup = await server.execute({
        "session_id": session_id,
        "thought": "Duplicate branch",
        "thought_number": 3,
        "total_thoughts": 3,
        "branch_from_thought": 1,
        "branch_id": "test_branch",  # Duplicate
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result_dup["status"] == "error"
    assert "already exists" in result_dup["error"]


# ===== Status Message Tests =====

@pytest.mark.asyncio
async def test_status_messages_start_end(server, mock_status):
    """Test status messages are sent."""
    result = await server.execute({
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Verify end was called (start doesn't exist in StatusScope)
    mock_status.end.assert_called_once()
    end_call = mock_status.end.call_args[0][0]
    assert "Thought #1 added" in end_call  # Server uses #N format
    assert "Complete" in end_call


@pytest.mark.asyncio
async def test_status_error_message(server, mock_status):
    """Test ERROR status message on failure."""
    result = await server.execute({
        "thought": "",  # Invalid
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    mock_status.error.assert_called_once()
    error_call = mock_status.error.call_args[0][0]
    assert "cannot be empty" in error_call


# ===== Integration Tests =====

@pytest.mark.asyncio
async def test_template_vars(server):
    """Test template variables are provided."""
    vars = server.get_template_vars()
    
    assert vars["name"] == "sequential_thinking"
    assert vars["max_history_size"] == 100
    assert vars["session_ttl_seconds"] == 3600
    assert vars["enable_branching"] is True
    assert vars["enable_revisions"] is True


# ===== Phase 1 Improvement Tests =====

@pytest.mark.asyncio
async def test_branch_parent_from_thought_branch(server, mock_status):
    """Test that branch parent is derived from branched_from_thought's branch, not current_branch."""
    # Create initial thoughts on main
    result1 = await server.execute({
        "thought": "Main thought 1",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Main thought 2",
        "thought_number": 2,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Create branch 'alternative' from thought 2
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "Alternative approach",
        "thought_number": 3,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "branch_id": "alternative",
        "branch_from_thought": 2,
        "_status": mock_status
    })
    
    # Now create branch 'edge_cases' from thought 1 (which is on main, not alternative)
    # This is the critical test: we're on 'alternative' branch but branching from thought 1
    result4 = await server.execute({
        "session_id": session_id,
        "thought": "Edge case exploration",
        "thought_number": 4,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "branch_id": "edge_cases",
        "branch_from_thought": 1,  # Thought 1 is on 'main' branch
        "_status": mock_status
    })
    
    # Verify branch_summary shows correct parent
    branch_summary = result4["branch_summary"]
    assert "edge_cases" in branch_summary
    # Parent should be 'main' (from thought 1), NOT 'alternative' (current branch)
    assert branch_summary["edge_cases"]["parent"] == "main"
    assert branch_summary["edge_cases"]["branched_from"] == 1


@pytest.mark.asyncio
async def test_progress_auto_clamp(server, mock_status):
    """Test that total_thoughts_estimate is auto-clamped to >= actual_thoughts."""
    # Start with estimate of 10
    result1 = await server.execute({
        "thought": "Start",
        "thought_number": 1,
        "total_thoughts": 10,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Add thoughts up to 12 (exceeding original estimate)
    for i in range(2, 13):
        result = await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 10,  # Still claiming 10
            "next_thought_needed": True,
            "_status": mock_status
        })
    
    # Now try to set estimate to 8 (less than actual 12)
    result_clamp = await server.execute({
        "session_id": session_id,
        "thought": "Thought 13",
        "thought_number": 13,
        "total_thoughts": 8,  # Invalid: less than actual
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Should be auto-clamped to 13 (actual thoughts)
    assert result_clamp["total_thoughts_estimate"] == 13
    assert result_clamp["warnings"] is not None
    assert any("adjusted to" in w for w in result_clamp["warnings"])
    # Progress should never show X/Y with X > Y
    assert "13/13" in result_clamp["progress"] or "13/" in result_clamp["progress"]


@pytest.mark.asyncio
async def test_progress_display_consistency(server, mock_status):
    """Test that progress display is always consistent (never X/Y with X > Y)."""
    result1 = await server.execute({
        "thought": "Start",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Add thoughts beyond estimate
    for i in range(2, 8):
        result = await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 5,  # Not updating estimate
            "next_thought_needed": i < 7,
            "_status": mock_status
        })
        
        # Extract progress numbers
        progress = result["progress"]
        # Format: "Thought X/Y"
        parts = progress.split()
        if len(parts) >= 2:
            numbers = parts[1].split("/")
            if len(numbers) == 2:
                current = int(numbers[0])
                total = int(numbers[1])
                # Current should never exceed total in display
                assert current <= total, f"Progress {progress} is inconsistent"


@pytest.mark.asyncio
async def test_warnings_array_on_inconsistent_params(server, mock_status):
    """Test that warnings array is populated on inconsistent parameters."""
    result1 = await server.execute({
        "thought": "Start",
        "thought_number": 1,
        "total_thoughts": 10,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Add thoughts to 5
    for i in range(2, 6):
        await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 10,
            "next_thought_needed": True,
            "_status": mock_status
        })
    
    # Now: needs_more_thoughts=true but estimate NOT increased
    result_warning = await server.execute({
        "session_id": session_id,
        "thought": "Need more but not increasing estimate",
        "thought_number": 6,
        "total_thoughts": 10,  # Same as before
        "next_thought_needed": True,
        "needs_more_thoughts": True,  # But claiming need more
        "_status": mock_status
    })
    
    # Should have warning about needs_more_thoughts
    assert result_warning["warnings"] is not None
    assert any("not increased" in w for w in result_warning["warnings"])


@pytest.mark.asyncio
async def test_recorded_thoughts_count_field(server, mock_status):
    """Test that recorded_thoughts_count field is present and correct."""
    result1 = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    assert "recorded_thoughts_count" in result1
    assert result1["recorded_thoughts_count"] == 1
    
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Second",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    assert result2["recorded_thoughts_count"] == 2
    
    # Revise thought 1
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "First (revised)",
        "thought_number": 3,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "is_revision": True,
        "revises_thought": 1,
        "_status": mock_status
    })
    
    # Recorded count should be 3 (original thought 1, thought 2, revision of thought 1)
    assert result3["recorded_thoughts_count"] == 3


@pytest.mark.asyncio
async def test_memory_usage_warning_in_warnings_array(server, mock_status):
    """Test that memory usage warnings appear in warnings array."""
    # Set small limit
    server.max_history_size = 10
    
    result = await server.execute({
        "thought": "Start",
        "thought_number": 1,
        "total_thoughts": 15,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result["session_id"]
    
    # Add thoughts to 9 (90% of limit)
    for i in range(2, 10):
        result = await server.execute({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 15,
            "next_thought_needed": True,
            "_status": mock_status
        })
    
    # Last result should have memory warning
    assert result["warnings"] is not None
    assert any("Memory usage" in w for w in result["warnings"])


# ===== Phase 2 Feature Tests (Event IDs + Idempotency) =====

@pytest.mark.asyncio
async def test_event_id_present_in_response(server, mock_status):
    """Test that event_id is present in responses."""
    result = await server.execute({
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert "event_id" in result
    assert result["event_id"] is not None
    assert len(result["event_id"]) > 0


@pytest.mark.asyncio
async def test_event_id_in_thought_history(server, mock_status):
    """Test that event_id appears in thought_history."""
    result1 = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Second",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Check thought_history has event_id
    assert len(result2["thought_history"]) == 2
    for t in result2["thought_history"]:
        assert "event_id" in t
        assert t["event_id"] is not None


@pytest.mark.asyncio
async def test_event_id_in_summary(server, mock_status):
    """Test that event_id appears in summary."""
    result = await server.execute({
        "thought": "Test",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    summary = await server.get_summary({
        "session_id": session_id,
        "_status": mock_status
    })
    
    assert len(summary["thoughts"]) == 1
    assert "event_id" in summary["thoughts"][0]
    assert summary["thoughts"][0]["event_id"] is not None


@pytest.mark.asyncio
async def test_idempotency_basic(server, mock_status):
    """Test basic idempotency with idempotency_key."""
    idempotency_key = "test-key-12345"
    
    # First call with idempotency_key
    result1 = await server.execute({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "idempotency_key": idempotency_key,
        "_status": mock_status
    })
    
    event_id1 = result1["event_id"]
    session_id = result1["session_id"]
    
    # Second call with same idempotency_key
    result2 = await server.execute({
        "thought": "Different thought (should be ignored)",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "idempotency_key": idempotency_key,
        "_status": mock_status
    })
    
    # Should return same event_id
    assert result2["event_id"] == event_id1
    assert result2["warnings"] is not None
    assert any("Idempotent" in w for w in result2["warnings"])
    
    # Session should only have 1 thought (not duplicated)
    summary = await server.get_summary({
        "session_id": session_id,
        "_status": mock_status
    })
    assert len(summary["thoughts"]) == 1


@pytest.mark.asyncio
async def test_idempotency_different_keys(server, mock_status):
    """Test that different idempotency_keys create different events."""
    result1 = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "idempotency_key": "key-1",
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    event_id1 = result1["event_id"]
    
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Second",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "idempotency_key": "key-2",
        "_status": mock_status
    })
    
    event_id2 = result2["event_id"]
    
    # Different keys → different event_ids
    assert event_id1 != event_id2
    
    # Should have 2 thoughts
    summary = await server.get_summary({
        "session_id": session_id,
        "_status": mock_status
    })
    assert len(summary["thoughts"]) == 2


# ===== Edge Case Validation Tests =====

@pytest.mark.asyncio
async def test_revision_without_revises_thought_fails(server, mock_status):
    """Test that is_revision without revises_thought is rejected."""
    result = await server.execute({
        "thought": "Test",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "is_revision": True,  # Missing revises_thought!
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "revises_thought is required" in result["error"]


@pytest.mark.asyncio
async def test_nonexistent_branch_switch_fails(server, mock_status):
    """Test that switching to nonexistent branch fails."""
    result = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Try to switch to non-existent branch
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Second",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "branch_id": "nonexistent",  # Doesn't exist!
        "_status": mock_status
    })
    
    assert result2["status"] == "error"
    assert "not found" in result2["error"]


@pytest.mark.asyncio
async def test_revise_nonexistent_thought_fails(server, mock_status):
    """Test that revising nonexistent thought fails."""
    result = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Try to revise thought that doesn't exist
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Revise nonexistent",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "is_revision": True,
        "revises_thought": 99,  # Doesn't exist!
        "_status": mock_status
    })
    
    assert result2["status"] == "error"
    assert "not found" in result2["error"]


@pytest.mark.asyncio
async def test_branch_id_already_exists_fails(server, mock_status):
    """Test that creating branch with existing ID fails."""
    result = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Create branch
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Branch",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "branch_id": "test_branch",
        "branch_from_thought": 1,
        "_status": mock_status
    })
    
    assert result2["status"] == "success"
    
    # Try to create same branch again
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "Another",
        "thought_number": 3,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "branch_id": "test_branch",  # Already exists!
        "branch_from_thought": 1,
        "_status": mock_status
    })
    
    assert result3["status"] == "error"
    assert "already exists" in result3["error"]


@pytest.mark.asyncio
async def test_invalid_thought_number_zero_fails(server, mock_status):
    """Test that thought_number=0 is rejected."""
    result = await server.execute({
        "thought": "Test",
        "thought_number": 0,  # Invalid!
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "must be >= 1" in result["error"]


@pytest.mark.asyncio
async def test_invalid_total_thoughts_zero_fails(server, mock_status):
    """Test that total_thoughts=0 is rejected."""
    result = await server.execute({
        "thought": "Test",
        "thought_number": 1,
        "total_thoughts": 0,  # Invalid!
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "must be >= 1" in result["error"]




# ===== Phase 2a: Edge Cases & Validation Tests =====

@pytest.mark.asyncio
async def test_thought_history_has_consistent_fields(server, mock_status):
    """Test that thought_history always includes revises_thought and timestamp."""
    result1 = await server.execute({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Check all required fields in thought_history
    for thought in result1["thought_history"]:
        assert "number" in thought
        assert "content" in thought
        assert "branch" in thought
        assert "is_revision" in thought
        # revises_thought is omitted when None (Gemini compatibility fix)
        # assert "revises_thought" in thought 
        assert "timestamp" in thought              # ✅ Always present (v1.0.2)
    
    # Add revision
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Revised first thought",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "is_revision": True,
        "revises_thought": 1,
        "_status": mock_status
    })
    
    # Check revision entry
    for thought in result2["thought_history"]:
        assert "timestamp" in thought
        if thought["is_revision"]:
            # When is_revision=true, revises_thought must be present and not None
            assert "revises_thought" in thought
            assert thought["revises_thought"] is not None
        else:
            # When is_revision=false, revises_thought should be omitted (Gemini compat fix)
            # It's ok if it's present but None, but preferred to omit
            pass


@pytest.mark.asyncio
async def test_revision_without_revises_thought_error(server, mock_status):
    """Test clear error when is_revision=true but revises_thought missing."""
    result1 = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Try revision without revises_thought
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Should fail",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "is_revision": True,
        # Missing revises_thought!
        "_status": mock_status
    })
    
    assert result2["status"] == "error"
    assert "revises_thought is required" in result2["error"]


@pytest.mark.asyncio
async def test_switch_to_nonexistent_branch_error(server, mock_status):
    """Test clear error when switching to non-existent branch."""
    result1 = await server.execute({
        "thought": "First on main",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Try to switch to non-existent branch
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Switch to nowhere",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "branch_id": "nonexistent_branch",
        "_status": mock_status
    })
    
    assert result2["status"] == "error"
    assert "Branch 'nonexistent_branch' not found" in result2["error"]


@pytest.mark.asyncio
async def test_revise_nonexistent_thought_error(server, mock_status):
    """Test clear error when revising thought that doesn't exist."""
    result1 = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 2,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Try to revise thought #99 (doesn't exist)
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Revise non-existent",
        "thought_number": 2,
        "total_thoughts": 2,
        "next_thought_needed": False,
        "is_revision": True,
        "revises_thought": 99,              # Doesn't exist!
        "_status": mock_status
    })
    
    assert result2["status"] == "error"
    assert "Thought #99 not found" in result2["error"]


@pytest.mark.asyncio
async def test_create_branch_with_existing_id_error(server, mock_status):
    """Test error when trying to create branch with existing ID."""
    result1 = await server.execute({
        "thought": "First",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    session_id = result1["session_id"]
    
    # Create branch
    result2 = await server.execute({
        "session_id": session_id,
        "thought": "Create alternative",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "branch_from_thought": 1,
        "branch_id": "alternative",
        "_status": mock_status
    })
    assert result2["status"] == "success"
    
    # Try to create same branch again
    result3 = await server.execute({
        "session_id": session_id,
        "thought": "Try to recreate alternative",
        "thought_number": 3,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "branch_from_thought": 2,
        "branch_id": "alternative",           # Already exists!
        "_status": mock_status
    })
    
    assert result3["status"] == "error"
    assert "Branch 'alternative' already exists" in result3["error"]


@pytest.mark.asyncio
async def test_invalid_thought_number_error(server, mock_status):
    """Test error with invalid thought_number values."""
    # thought_number < 1
    result = await server.execute({
        "thought": "Invalid",
        "thought_number": 0,                  # ❌ Invalid
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    assert result["status"] == "error"
    assert "thought_number must be >= 1" in result["error"]


@pytest.mark.asyncio
async def test_invalid_total_thoughts_error(server, mock_status):
    """Test error with invalid total_thoughts values."""
    # total_thoughts < 1
    result = await server.execute({
        "thought": "Invalid",
        "thought_number": 1,
        "total_thoughts": 0,                  # ❌ Invalid
        "next_thought_needed": False,
        "_status": mock_status
    })
    assert result["status"] == "error"
    assert "total_thoughts must be >= 1" in result["error"]


