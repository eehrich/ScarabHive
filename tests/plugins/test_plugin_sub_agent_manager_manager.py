"""Unit tests for SubAgentManager."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.sub_agent_manager.manager import SubAgentManager


@pytest.fixture
def mock_session_service():
    """Mock SessionService."""
    service = MagicMock()
    service.session_manager = MagicMock()
    service.session_manager.create_session = AsyncMock()
    service.session_manager.load_session = AsyncMock()
    service.session_manager.save_session = AsyncMock()
    service.session_manager.update_session_metadata = AsyncMock()  # Required for _link_sub_to_parent
    # Mock _session_id_exists_globally to always return False (ID is available)
    service.session_manager._session_id_exists_globally = MagicMock(return_value=False)
    return service


@pytest.fixture
def mock_registry():
    """Mock MCPRegistry."""
    return MagicMock()


import re


@pytest.fixture
def manager(mock_session_service, mock_registry):
    """Create SubAgentManager instance."""
    return SubAgentManager(mock_session_service, mock_registry)


@pytest.mark.asyncio
async def test_create_sub_session_generates_unique_id(manager, mock_session_service):
    """Test that create_sub_session generates unique IDs."""
    # Setup mocks - parent session with depth
    mock_session_service.session_manager.load_session.return_value = {
        "session_id": "parent123",
        "depth": 1,  # Root session
        "metadata": {}
    }

    # Create first sub-session
    sub_id1 = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="Search for AI news"
    )

    # Check format: sub_web_research_{counter} where counter is a number
    assert re.match(r"sub_web_research_\d+", sub_id1), f"Expected pattern sub_web_research_<digits>, got {sub_id1}"

    # Create second sub-session with same type
    sub_id2 = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="Search for ML papers"
    )

    # Check format and uniqueness (counter should increment)
    assert re.match(r"sub_web_research_\d+", sub_id2), f"Expected pattern sub_web_research_<digits>, got {sub_id2}"
    assert sub_id1 != sub_id2, "Sub-session IDs should be unique"


@pytest.mark.asyncio
async def test_create_sub_session_creates_session_file(manager, mock_session_service):
    """Test that create_sub_session calls SessionManager.create_session."""
    mock_session_service.session_manager.load_session.return_value = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {}
    }

    await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="financial_analyst",
        initial_message="Analyze TSLA stock"
    )

    # Verify create_session was called
    mock_session_service.session_manager.create_session.assert_called_once()
    call_kwargs = mock_session_service.session_manager.create_session.call_args.kwargs

    assert call_kwargs["user_id"] == "anonymous"  # No user_id injected, falls back to anonymous
    # Each test gets fresh manager, so counter starts at 1
    assert call_kwargs["session_id"].startswith("sub_financial_analyst_")
    assert call_kwargs["agent_name"] == "financial_analyst"
    assert "Analyze TSLA stock" in call_kwargs["title"]


@pytest.mark.asyncio
async def test_create_sub_session_links_to_parent(manager, mock_session_service):
    """Test that parent metadata includes sub-agent references."""
    sub_session_data = {
        "session_id": "sub_web_research_004",  # Next in sequence
        "metadata": {}
    }
    parent_session_data = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {}
    }

    # Mock load_session to return different data for sub vs parent
    async def load_session_side_effect(user_id, session_id):
        if session_id.startswith("sub_"):
            return sub_session_data.copy()
        else:
            return parent_session_data.copy()

    mock_session_service.session_manager.load_session.side_effect = load_session_side_effect

    sub_id = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="Search AI"
    )

    # Verify update_session_metadata was called to link sub-agent to parent
    mock_session_service.session_manager.update_session_metadata.assert_called()
    
    # Get the call args - parent session should have sub_agents metadata
    call_args = mock_session_service.session_manager.update_session_metadata.call_args
    assert call_args is not None
    user_id, parent_id, metadata = call_args.args
    assert parent_id == "parent123"
    assert "sub_agents" in metadata
    
    # The sub_id should be in the sub_agents dict
    sub_agents = metadata["sub_agents"]
    assert sub_id in sub_agents
    
    sub_metadata = sub_agents[sub_id]
    assert sub_metadata["agent_type"] == "web_research"
    assert sub_metadata["status"] == "active"
    assert "Search AI" in sub_metadata["task_summary"]
    assert sub_metadata["depth"] == 2  # Parent is depth 1, child is 2


@pytest.mark.asyncio
async def test_list_sub_sessions_filters_completed(manager, mock_session_service):
    """Test list_sub_sessions filters by status."""
    parent_data = {
        "session_id": "parent123",
        "metadata": {
            "sub_agents": {
                "parent123_sub_web_001": {
                    "instance_id": "parent123_sub_web_001",
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                    "last_used": datetime.now(UTC).isoformat(),
                    "task_summary": "Task 1"
                },
                "parent123_sub_web_002": {
                    "instance_id": "parent123_sub_web_002",
                    "agent_type": "web_research",
                    "status": "archived",
                    "created_at": datetime.now(UTC).isoformat(),
                    "last_used": datetime.now(UTC).isoformat(),
                    "task_summary": "Task 2"
                }
            }
        }
    }

    mock_session_service.session_manager.load_session.return_value = parent_data

    # List only active
    active_only = await manager.list_sub_sessions("parent123", include_completed=False)
    assert len(active_only) == 1
    assert active_only[0]["instance_id"] == "parent123_sub_web_001"

    # List all
    all_sessions = await manager.list_sub_sessions("parent123", include_completed=True)
    assert len(all_sessions) == 2


@pytest.mark.asyncio
async def test_list_sub_sessions_returns_metadata(manager, mock_session_service):
    """Test list_sub_sessions returns correct metadata."""
    now = datetime.now(UTC).isoformat()
    parent_data = {
        "session_id": "parent123",
        "metadata": {
            "sub_agents": {
                "parent123_sub_web_001": {
                    "instance_id": "parent123_sub_web_001",
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": now,
                    "last_used": now,
                    "task_summary": "Search for AI news"
                }
            }
        }
    }

    mock_session_service.session_manager.load_session.return_value = parent_data

    result = await manager.list_sub_sessions("parent123")

    assert len(result) == 1
    metadata = result[0]
    assert metadata["instance_id"] == "parent123_sub_web_001"
    assert metadata["agent_type"] == "web_research"
    assert metadata["status"] == "active"
    assert metadata["task_summary"] == "Search for AI news"


@pytest.mark.asyncio
async def test_generate_instance_id_increments_counter(manager):
    """Test global counter increments."""
    id1 = await manager._generate_instance_id("web_research", None)
    # Check format: sub_web_research_{digits}
    assert re.match(r"sub_web_research_\d+", id1), f"Expected pattern sub_web_research_<digits>, got {id1}"
    # Extract the counter from id1
    counter1 = int(id1.split("_")[-1])

    id2 = await manager._generate_instance_id("web_research", None)
    assert re.match(r"sub_web_research_\d+", id2), f"Expected pattern sub_web_research_<digits>, got {id2}"
    counter2 = int(id2.split("_")[-1])
    assert counter2 > counter1, f"Counter should increment: {counter2} should be > {counter1}"

    # Different agent type uses same global counter
    id3 = await manager._generate_instance_id("financial_analyst", None)
    assert re.match(r"sub_financial_analyst_\d+", id3), f"Expected pattern sub_financial_analyst_<digits>, got {id3}"
    counter3 = int(id3.split("_")[-1])
    assert counter3 > counter2, f"Counter should increment: {counter3} should be > {counter2}"


@pytest.mark.asyncio
async def test_generate_instance_id_with_label(manager):
    """Test instance ID generation with custom label."""
    id1 = await manager._generate_instance_id("web_research", "my_research")
    # Check format with custom label: sub_my_research_{digits}
    assert re.match(r"sub_my_research_\d+", id1), f"Expected pattern sub_my_research_<digits>, got {id1}"
    counter1 = int(id1.split("_")[-1])

    # Sanitize label - special chars should become underscores
    id2 = await manager._generate_instance_id("web", "task#2@test")
    assert re.match(r"sub_task_2_test_\d+", id2), f"Expected pattern sub_task_2_test_<digits>, got {id2}"
    counter2 = int(id2.split("_")[-1])
    assert counter2 > counter1, f"Counter should increment: {counter2} should be > {counter1}"


def test_extract_user_id_handles_formats(manager):
    """Test user ID extraction."""
    # Falls back to 'anonymous' when no user_id found
    assert manager._extract_user_id("any_session_id") == "anonymous"
    assert manager._extract_user_id("another_session") == "anonymous"

    # With injected params, uses the provided user_id
    params_with_user = {"_user_id": "test_user"}
    assert manager._extract_user_id("session_123", params_with_user) == "test_user"


@pytest.mark.asyncio
async def test_create_sub_session_enforces_max_per_type_limit(mock_session_service, mock_registry):
    """Test that create_sub_session enforces max_sub_agents_per_type limit."""
    # Create manager with max_sub_agents_per_type=2
    manager = SubAgentManager(mock_session_service, mock_registry, max_nesting_depth=5, max_sub_agents_per_type=2)
    
    # Setup parent session with 2 active sub-agents of type 'web_research'
    mock_session_service.session_manager.load_session.return_value = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_web_research_001": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat()
                },
                "sub_web_research_002": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat()
                }
            }
        }
    }
    
    # Attempting to create 3rd sub-agent of same type should fail
    with pytest.raises(ValueError, match="Maximum number of active sub-agents of type 'web_research'"):
        await manager.create_sub_session(
            parent_session_id="parent123",
            agent_type="web_research",
            initial_message="Third research task"
        )


@pytest.mark.asyncio
async def test_create_sub_session_allows_different_types(mock_session_service, mock_registry):
    """Test that max_sub_agents_per_type limit only applies per type."""
    manager = SubAgentManager(mock_session_service, mock_registry, max_nesting_depth=5, max_sub_agents_per_type=2)
    
    # Setup parent session with 2 active sub-agents of type 'web_research'
    mock_session_service.session_manager.load_session.return_value = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_web_research_001": {"agent_type": "web_research", "status": "active"},
                "sub_web_research_002": {"agent_type": "web_research", "status": "active"}
            }
        }
    }
    
    # Mock registry to return agent with llm_profile
    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)
    
    # Creating sub-agent of different type should succeed
    sub_id = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="financial_analyst",
        initial_message="Analyze stocks"
    )
    
    assert sub_id.startswith("sub_financial_analyst_")


@pytest.mark.asyncio
async def test_create_sub_session_ignores_completed_agents_in_limit(mock_session_service, mock_registry):
    """Test that completed sub-agents don't count towards the limit."""
    manager = SubAgentManager(mock_session_service, mock_registry, max_nesting_depth=5, max_sub_agents_per_type=2)
    
    # Setup parent session with 2 sub-agents: 1 active, 1 completed
    mock_session_service.session_manager.load_session.return_value = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_web_research_001": {"agent_type": "web_research", "status": "active"},
                "sub_web_research_002": {"agent_type": "web_research", "status": "completed"}
            }
        }
    }
    
    # Mock registry
    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)
    
    # Creating another sub-agent should succeed (only 1 active)
    sub_id = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="New research task"
    )
    
    assert sub_id.startswith("sub_web_research_")


@pytest.mark.asyncio
async def test_create_sub_session_enforces_max_per_session_limit(mock_session_service, mock_registry):
    """Test that create_sub_session enforces max_sub_agents_per_session limit."""
    # Create manager with max_sub_agents_per_session=3
    manager = SubAgentManager(
        mock_session_service, 
        mock_registry, 
        max_nesting_depth=5, 
        max_sub_agents_per_type=10,  # High per-type limit
        max_sub_agents_per_session=3  # Low session limit
    )
    
    # Setup parent session with 3 active sub-agents of DIFFERENT types
    mock_session_service.session_manager.load_session.return_value = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_web_research_001": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat()
                },
                "sub_financial_analyst_001": {
                    "agent_type": "financial_analyst",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat()
                },
                "sub_code_reviewer_001": {
                    "agent_type": "code_reviewer",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat()
                }
            }
        }
    }
    
    # Attempting to create 4th sub-agent should fail (even though it's a different type)
    with pytest.raises(ValueError, match="Maximum number of active sub-agents per session"):
        await manager.create_sub_session(
            parent_session_id="parent123",
            agent_type="data_analyst",
            initial_message="Fourth agent task"
        )
