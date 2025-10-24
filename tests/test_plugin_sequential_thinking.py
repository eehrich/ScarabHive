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
    result1 = await server.sequentialthinking({
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
    result2 = await server.sequentialthinking({
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
    result3 = await server.sequentialthinking({
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
    result1 = await server.sequentialthinking({
        "thought": "Start with initial estimate of 3 thoughts",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    assert result1["total_thoughts_estimate"] == 3
    
    # Realize we need more thoughts
    result2 = await server.sequentialthinking({
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
    result1 = await server.sequentialthinking({
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
    result2 = await server.sequentialthinking({
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
    result1 = await server.sequentialthinking({
        "thought": "Main approach: Use JWT for authentication",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    
    result2 = await server.sequentialthinking({
        "session_id": session_id,
        "thought": "Implement token generation service",
        "thought_number": 2,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Create branch from thought 1
    result3 = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
        "thought": "Root thought",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Branch 1
    await server.sequentialthinking({
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
    await server.sequentialthinking({
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
    
    result = await server.sequentialthinking({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Attempt to branch
    result_branch = await server.sequentialthinking({
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
    result1 = await server.sequentialthinking({
        "thought": "Initial thought: Use PostgreSQL",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result1["session_id"]
    
    result2 = await server.sequentialthinking({
        "session_id": session_id,
        "thought": "Continue with schema design",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    # Revise thought 1
    result3 = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
        "thought": "Original thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # First revision
    await server.sequentialthinking({
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
    await server.sequentialthinking({
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
    
    result = await server.sequentialthinking({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Attempt to revise
    result_revision = await server.sequentialthinking({
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
    
    result = await server.sequentialthinking({
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
    await server.sequentialthinking({
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
    result1 = await server.sequentialthinking({
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
    await server.sequentialthinking({
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
    result1 = await server.sequentialthinking({
        "thought": "Session 1",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    result2 = await server.sequentialthinking({
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
        await server.sequentialthinking({
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
    result = await server.sequentialthinking({
        "thought": "First thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    await server.sequentialthinking({
        "session_id": session_id,
        "thought": "Second thought",
        "thought_number": 2,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    await server.sequentialthinking({
        "session_id": session_id,
        "thought": "Third thought",
        "thought_number": 3,
        "total_thoughts": 3,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Get summary
    summary = await server.get_thought_summary({
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
    result = await server.sequentialthinking({
        "thought": "Thought 1",
        "thought_number": 1,
        "total_thoughts": 15,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    for i in range(2, 16):
        await server.sequentialthinking({
            "session_id": session_id,
            "thought": f"Thought {i}",
            "thought_number": i,
            "total_thoughts": 15,
            "next_thought_needed": i < 15,
            "_status": mock_status
        })
    
    # Get summary with limit of 5
    summary = await server.get_thought_summary({
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
    result = await server.sequentialthinking({
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    summary = await server.get_thought_summary({
        "session_id": session_id,
        "include_branches": False,
        "_status": mock_status
    })
    
    assert summary["status"] == "success"
    assert summary["branches"] is None


@pytest.mark.asyncio
async def test_summary_nonexistent_session(server, mock_status):
    """Test summary for nonexistent session returns error."""
    result = await server.get_thought_summary({
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
    result = await server.sequentialthinking({
        "thought": "Thought 1",
        "thought_number": 1,
        "total_thoughts": 10,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    for i in range(2, 11):
        await server.sequentialthinking({
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
    result = await server.sequentialthinking({
        "thought": "Start",
        "thought_number": 1,
        "total_thoughts": 100,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Add 84 more thoughts (total 85, which is 85% of 100 limit)
    for i in range(2, 86):
        result = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
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
    result = await server.sequentialthinking({
        "thought": "Root",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": mock_status
    })
    
    session_id = result["session_id"]
    
    # Create first branch
    await server.sequentialthinking({
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
    result_dup = await server.sequentialthinking({
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
    """Test START and END status messages are sent."""
    result = await server.sequentialthinking({
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 1,
        "next_thought_needed": False,
        "_status": mock_status
    })
    
    # Verify start was called
    mock_status.start.assert_called_once()
    start_call = mock_status.start.call_args[0][0]
    assert "Adding thought" in start_call
    assert "1/1" in start_call
    
    # Verify end was called
    mock_status.end.assert_called_once()
    end_call = mock_status.end.call_args[0][0]
    assert "Thought 1 added" in end_call
    assert "Complete" in end_call


@pytest.mark.asyncio
async def test_status_error_message(server, mock_status):
    """Test ERROR status message on failure."""
    result = await server.sequentialthinking({
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
