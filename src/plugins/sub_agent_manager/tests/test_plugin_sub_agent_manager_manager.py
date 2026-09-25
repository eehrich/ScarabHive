"""Unit tests for SubAgentManager."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.sub_agent_manager.manager import CallerMistake, SubAgentLimitReached, SubAgentManager


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
    """Mock ToolServerRegistry."""
    return MagicMock()


import re


@pytest.fixture
def manager(mock_session_service, mock_registry):
    """Create SubAgentManager instance."""
    return SubAgentManager(mock_session_service, mock_registry)


def test_two_processes_do_not_start_counting_at_the_same_id(monkeypatch):
    """Parallel agent-cli runs of a batch start within one second; seeded from the time of day, they counted
    through the same ids."""
    import time

    from plugins.sub_agent_manager import manager as sam_manager

    monkeypatch.setattr(time, "time", lambda: 1_758_000_000.0)  # one and the same second for every process

    assert len({sam_manager._first_counter() for _ in range(3)}) == 3


@pytest.mark.asyncio
async def test_the_first_id_counts_on_from_where_the_process_started(manager, monkeypatch):
    from plugins.sub_agent_manager import manager as sam_manager

    monkeypatch.setattr(SubAgentManager, "_class_counter", None)  # a process that has made no id yet
    monkeypatch.setattr(sam_manager, "_first_counter", lambda: 4_812_000)

    assert await manager._generate_instance_id("worker", None) == "sub_worker_4812001"
    assert await manager._generate_instance_id("worker", None) == "sub_worker_4812002"


@pytest.mark.asyncio
async def test_one_archiving_makes_room_at_both_limits(mock_session_service, mock_registry):
    """At the session's limit and the type's at once, the oldest of the type makes room under both. The session's
    limit was checked first: it archived the oldest of any type, and with the type still full, a second one."""
    manager = SubAgentManager(mock_session_service, mock_registry, max_nesting_depth=5,
                              max_sub_agents_per_type=3, max_sub_agents_per_session=10, auto_archive_on_limit=True)
    existing = {f"s{i}": {"agent_type": "planner" if i < 7 else "reviewer", "status": "active",
                          "created_at": f"2026-01-01T0{i}:00:00+00:00"} for i in range(10)}
    archived: list[str] = []

    async def archive(parent_session_id, sub_session_id):
        archived.append(sub_session_id)
        return True
    manager._archive_sub_agent = archive

    await manager._make_room("parent123", existing, "reviewer")

    assert archived == ["s7"], "the oldest reviewer alone"


@pytest.mark.asyncio
async def test_a_create_for_an_agent_that_is_not_there_archives_nothing(mock_session_service):
    """At the limit, making room archives the oldest sub-agent -- one the caller may be waiting on.
    A misspelled type did that first and failed afterwards; the real registry raises KeyError for a
    name it does not know, so the check behind it never even ran."""
    from agent_system.tools.base import ToolServerRegistry

    manager = SubAgentManager(mock_session_service, ToolServerRegistry(), max_nesting_depth=5,
                              max_sub_agents_per_type=1, max_sub_agents_per_session=1,
                              auto_archive_on_limit=True)
    mock_session_service.session_manager.load_session = AsyncMock(return_value={
        "session_id": "parent123", "depth": 1,
        "metadata": {"sub_agents": {"sub_research_1": {
            "agent_type": "web_research", "status": "active",
            "created_at": "2026-01-01T08:00:00+00:00"}}}})
    manager._write_sub_agent = AsyncMock(return_value=True)

    with pytest.raises(CallerMistake, match="not found in registry"):
        await manager.create_sub_session(parent_session_id="parent123", agent_type="planer_typo",
                                         initial_message="x")

    assert manager._write_sub_agent.await_args_list == [], "archived before the agent was known"


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


# ---------------------------------------------------------------------------
# Auto-archive tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_archive_on_per_type_limit(mock_session_service, mock_registry):
    """When auto_archive_on_limit=True and per-type limit hit, oldest sub-agent is archived."""
    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=2,
        auto_archive_on_limit=True
    )

    older_ts = "2026-01-01T10:00:00+00:00"
    newer_ts = "2026-01-01T11:00:00+00:00"

    parent_data = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_research_001": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": older_ts,
                },
                "sub_research_002": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": newer_ts,
                },
            }
        },
    }
    mock_session_service.session_manager.load_session = AsyncMock(return_value=parent_data)

    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)

    result = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="Third task",
    )

    # Should succeed and return a valid ID
    assert result.startswith("sub_web_research_")

    # update_session_metadata must have been called to archive the oldest sub-agent
    calls = mock_session_service.session_manager.update_session_metadata.call_args_list
    archive_calls = [
        c for c in calls
        if "sub_research_001" in str(c) and "archived" in str(c)
    ]
    assert len(archive_calls) >= 1, "Oldest sub-agent (sub_research_001) should have been archived"


@pytest.mark.asyncio
async def test_auto_archive_archives_oldest_not_newest(mock_session_service, mock_registry):
    """Auto-archive picks the oldest sub-agent (smallest created_at), not the newest."""
    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=2,
        auto_archive_on_limit=True
    )

    parent_data = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_research_newest": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": "2026-06-01T12:00:00+00:00",
                },
                "sub_research_oldest": {
                    "agent_type": "web_research",
                    "status": "active",
                    "created_at": "2026-01-01T08:00:00+00:00",
                },
            }
        },
    }
    mock_session_service.session_manager.load_session = AsyncMock(return_value=parent_data)

    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)

    await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="New task",
    )

    calls = mock_session_service.session_manager.update_session_metadata.call_args_list
    # sub_research_oldest should be archived, not sub_research_newest
    oldest_archived = any(
        "sub_research_oldest" in str(c) and "archived" in str(c) for c in calls
    )
    newest_archived = any(
        "sub_research_newest" in str(c) and "archived" in str(c) for c in calls
    )
    assert oldest_archived, "Oldest sub-agent should be archived"
    assert not newest_archived, "Newest sub-agent should NOT be archived"


@pytest.mark.asyncio
async def test_auto_archive_says_which_instance_it_took(mock_session_service, mock_registry):
    """Making room happens down here, but what an archived instance leaves behind lives above.

    The server holds the background jobs, and an auto-archived id never reaches it: the tool's
    `delete` goes through the server, this does not. Its job would then keep the whole result
    text for the life of the process -- which is what the writer's coordinators do all day, with
    ``auto_archive_on_limit`` on and hundreds of sub-agent branches per book.
    """
    archived: list[str] = []

    async def remember(instance_id: str) -> None:
        archived.append(instance_id)

    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=1,
        auto_archive_on_limit=True,
        on_archived=remember,
    )

    mock_session_service.session_manager.load_session = AsyncMock(return_value={
        "session_id": "parent123",
        "depth": 1,
        "metadata": {"sub_agents": {"sub_research_1": {
            "agent_type": "web_research", "status": "active",
            "created_at": "2026-01-01T08:00:00+00:00"}}},
    })
    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)

    await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="New task",
    )

    assert archived == ["sub_research_1"]


@pytest.mark.parametrize("later", [
    {"session_id": "parent123", "metadata": {}},
    {"session_id": "parent123", "metadata": {"sub_agents": {}}},
    OSError("the sessions directory is gone"),
], ids=["no-sub-agents-metadata", "id-not-among-them", "parent-unreadable"])
@pytest.mark.asyncio
async def test_nothing_is_reported_when_nothing_was_archived(mock_session_service, mock_registry, later):
    """`update_sub_session_metadata` gives up quietly on three paths, and these are they.

    Nothing says archived anywhere then: the instance is still listed, still counted and still
    pollable. Two things must not happen. Nobody may tidy up after it -- that would take a result
    away from a caller who can still ask for it. And the spawn that was making room must not go
    ahead: no room was made, so it would put the session over the limit it asked for, quietly."""
    archived: list[str] = []

    async def remember(instance_id: str) -> None:
        archived.append(instance_id)

    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=1,
        auto_archive_on_limit=True,
        on_archived=remember,
    )

    # The parent that create() loads first carries the sub-agent; every load after it is the one
    # the metadata write does, and gives it nothing to change.
    seen = []

    parent = {"session_id": "parent123", "depth": 1,
              "metadata": {"sub_agents": {"sub_research_1": {
                  "agent_type": "web_research", "status": "active",
                  "created_at": "2026-01-01T08:00:00+00:00"}}}}

    async def load(user_id, session_id):
        seen.append(session_id)
        if len(seen) == 1:
            return parent
        if len(seen) == 2:  # the archiving's own read; the create goes on reading afterwards
            if isinstance(later, Exception):
                raise later
            return later
        return parent

    mock_session_service.session_manager.load_session = AsyncMock(side_effect=load)
    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)

    with pytest.raises(ValueError, match="could not be archived"):
        await manager.create_sub_session(
            parent_session_id="parent123",
            agent_type="web_research",
            initial_message="New task",
        )

    assert archived == [], "nobody tidies up after an instance that is still there"


@pytest.mark.asyncio
async def test_a_slip_in_that_report_does_not_cost_the_room_it_made(mock_session_service, mock_registry):
    """The archiving is done and written by the time anyone is told. Failing the create over the
    bookkeeping would trade a job left in memory for a spawn that does not happen at all."""
    async def fails(instance_id: str) -> None:
        raise RuntimeError("no")

    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=1,
        auto_archive_on_limit=True,
        on_archived=fails,
    )

    mock_session_service.session_manager.load_session = AsyncMock(return_value={
        "session_id": "parent123",
        "depth": 1,
        "metadata": {"sub_agents": {"sub_research_1": {
            "agent_type": "web_research", "status": "active",
            "created_at": "2026-01-01T08:00:00+00:00"}}},
    })
    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)

    sub_session_id = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="web_research",
        initial_message="New task",
    )

    assert sub_session_id.startswith("sub_"), "the spawn it made room for went through"


@pytest.mark.asyncio
async def test_auto_archive_on_session_limit(mock_session_service, mock_registry):
    """When auto_archive_on_limit=True and session limit hit, oldest sub-agent is archived."""
    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=10,
        max_sub_agents_per_session=3,
        auto_archive_on_limit=True
    )

    parent_data = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_type_a_001": {
                    "agent_type": "type_a",
                    "status": "active",
                    "created_at": "2026-01-01T09:00:00+00:00",
                },
                "sub_type_b_001": {
                    "agent_type": "type_b",
                    "status": "active",
                    "created_at": "2026-01-01T10:00:00+00:00",
                },
                "sub_type_c_001": {
                    "agent_type": "type_c",
                    "status": "active",
                    "created_at": "2026-01-01T11:00:00+00:00",
                },
            }
        },
    }
    mock_session_service.session_manager.load_session = AsyncMock(return_value=parent_data)

    mock_agent = MagicMock()
    mock_agent.agent_config.default_llm_profile = "gpt-4"
    mock_registry.get = MagicMock(return_value=mock_agent)

    result = await manager.create_sub_session(
        parent_session_id="parent123",
        agent_type="type_d",
        initial_message="Fourth task",
    )

    assert result.startswith("sub_type_d_")

    calls = mock_session_service.session_manager.update_session_metadata.call_args_list
    # Oldest overall (sub_type_a_001) should be archived
    oldest_archived = any(
        "sub_type_a_001" in str(c) and "archived" in str(c) for c in calls
    )
    assert oldest_archived, "Oldest session sub-agent should be archived on session limit"


@pytest.mark.asyncio
async def test_auto_archive_disabled_still_raises(mock_session_service, mock_registry):
    """When auto_archive_on_limit=False (default), limit still raises ValueError."""
    manager = SubAgentManager(
        mock_session_service, mock_registry,
        max_nesting_depth=5, max_sub_agents_per_type=2,
        auto_archive_on_limit=False
    )

    parent_data = {
        "session_id": "parent123",
        "depth": 1,
        "metadata": {
            "sub_agents": {
                "sub_research_001": {"agent_type": "web_research", "status": "active", "created_at": "2026-01-01T10:00:00+00:00"},
                "sub_research_002": {"agent_type": "web_research", "status": "active", "created_at": "2026-01-01T11:00:00+00:00"},
            }
        },
    }
    mock_session_service.session_manager.load_session = AsyncMock(return_value=parent_data)

    with pytest.raises(ValueError, match="Maximum number of active sub-agents of type 'web_research'"):
        await manager.create_sub_session(
            parent_session_id="parent123",
            agent_type="web_research",
            initial_message="Third task",
        )


# --- reopening takes a place ---------------------------------------------------------------------

def _parent_with(mock_session_service, **sub_agents):
    """A parent whose registry holds these instances, all of one type, oldest first."""
    mock_session_service.session_manager.load_session = AsyncMock(return_value={
        "session_id": "parent123", "depth": 1, "metadata": {"sub_agents": {
            instance_id: {"instance_id": instance_id, "agent_type": "reviewer", "status": status,
                          "created_at": f"2026-01-01T10:0{n}:00+00:00"}
            for n, (instance_id, status) in enumerate(sub_agents.items())}}})


def _statuses_written(mock_session_service) -> list[tuple[str, str]]:
    return [(instance_id, entry.get("status"))
            for call in mock_session_service.session_manager.update_session_metadata.await_args_list
            for instance_id, entry in call.args[2]["sub_agents"].items()]


@pytest.mark.asyncio
async def test_a_reopen_takes_a_place_like_a_new_one(mock_session_service, mock_registry):
    """A continue reopened a failed or archived instance past both limits, and the session
    stayed over them for good: the limits count only what is open."""
    manager = SubAgentManager(mock_session_service, mock_registry, max_sub_agents_per_type=1)
    _parent_with(mock_session_service, sub_open="active", sub_failed="failed")

    with pytest.raises(ValueError, match="of type 'reviewer'"):
        await manager.reopen_sub_session("parent123", "sub_failed")

    assert _statuses_written(mock_session_service) == []


@pytest.mark.asyncio
async def test_a_reopen_makes_room_the_way_a_create_does(mock_session_service, mock_registry):
    manager = SubAgentManager(mock_session_service, mock_registry, max_sub_agents_per_type=1,
                              auto_archive_on_limit=True)
    _parent_with(mock_session_service, sub_open="active", sub_failed="failed")

    await manager.reopen_sub_session("parent123", "sub_failed")

    assert _statuses_written(mock_session_service) == [("sub_open", "archived"), ("sub_failed", "active")]


@pytest.mark.asyncio
async def test_an_active_instance_already_holds_its_place(mock_session_service, mock_registry):
    """Counted against itself, an active instance of a full session could never be continued."""
    manager = SubAgentManager(mock_session_service, mock_registry, max_sub_agents_per_type=1)
    _parent_with(mock_session_service, sub_open="active")

    await manager.reopen_sub_session("parent123", "sub_open")

    assert _statuses_written(mock_session_service) == [("sub_open", "active")]


# --- one parent's entries, written by several at once --------------------------------------------

async def _stored_manager(tmp_path, **limits):
    """A manager on a real SessionManager: parent ``s-1`` of user ``ada``, spawning ``worker``."""
    from agent_system.config.models import AgentConfig
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService
    from agent_system.tools.base import ToolServerRegistry

    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
    await service.session_manager.create_session(user_id="ada", session_id="s-1", title="The coordinator",
                                                 agent_name="coordinator", llm_profile="normal")
    registry = ToolServerRegistry()
    worker = MagicMock()
    worker.name = "worker"
    worker.agent_config = AgentConfig(llm_profile="normal")
    registry.register("worker", worker)
    return service, SubAgentManager(service, registry, **limits)


def _spawn(manager, n=0):
    return manager.create_sub_session("s-1", "worker", f"task {n}", params={"_user_id": "ada"})


async def _entries(service):
    return (await service.session_manager.load_session("ada", "s-1"))["metadata"]["sub_agents"]


@pytest.mark.asyncio
async def test_a_fan_out_of_creates_keeps_the_limit(tmp_path):
    """The creates of one step all read the same count, and six of them passed a limit of three."""
    service, manager = await _stored_manager(tmp_path, max_sub_agents_per_session=3, max_sub_agents_per_type=3)

    spawned = await asyncio.gather(*(_spawn(manager, n) for n in range(6)), return_exceptions=True)

    assert sum(isinstance(result, SubAgentLimitReached) for result in spawned) == 3, spawned
    assert [entry["status"] for entry in (await _entries(service)).values()] == ["active"] * 3


@pytest.mark.asyncio
async def test_a_continue_beside_a_create_keeps_the_limit(tmp_path):
    """A reopen takes a place like a create does -- from the same count."""
    service, manager = await _stored_manager(tmp_path, max_sub_agents_per_session=2, max_sub_agents_per_type=2)
    failed = await _spawn(manager)
    await manager.update_sub_session_metadata("s-1", failed, status="failed")
    await _spawn(manager, 1)

    outcome = await asyncio.gather(manager.reopen_sub_session("s-1", failed), _spawn(manager, 2),
                                   return_exceptions=True)

    assert sum(isinstance(result, SubAgentLimitReached) for result in outcome) == 1, outcome
    assert sum(entry["status"] == "active" for entry in (await _entries(service)).values()) == 2


@pytest.mark.asyncio
async def test_an_archive_beside_an_activity_update_stays(tmp_path):
    """An entry is written whole: an update that had read it before the archive landed wrote the
    archive away -- and a running sub-agent writes its activity at every tool call."""
    service, manager = await _stored_manager(tmp_path)
    instance = await _spawn(manager)

    await asyncio.gather(manager.update_sub_session_metadata("s-1", instance, status="archived"),
                         manager.update_sub_agent_activity("s-1", instance, "Running tool: search"))

    entry = (await _entries(service))[instance]
    assert (entry["status"], entry["current_activity"]) == ("archived", "Running tool: search")


@pytest.mark.asyncio
async def test_only_a_reopen_opens_an_archived_instance_again(tmp_path):
    """A clean ending writes "active", a list that finds a run orphaned "interrupted" -- either
    over an archive that landed while the run went on took back the room it made. A run's verdict
    is written, and a continue reopens, through the limits."""
    service, manager = await _stored_manager(tmp_path)
    instance = await _spawn(manager)

    async def stored_after(**updates):
        await manager.update_sub_session_metadata("s-1", instance, **updates)
        return (await _entries(service))[instance]

    await manager.update_sub_session_metadata("s-1", instance, status="archived")
    ending = await stored_after(status="active", last_used="then")
    assert (ending["status"], ending["last_used"]) == ("archived", "then"), "the rest of the ending is written"
    assert (await stored_after(status="interrupted"))["status"] == "archived"
    assert (await stored_after(status="failed"))["status"] == "failed"

    await manager.update_sub_session_metadata("s-1", instance, status="archived")
    await manager.reopen_sub_session("s-1", instance)
    assert (await _entries(service))[instance]["status"] == "active"
