"""
End-to-end integration tests for SubAgentManager.

Tests real-world multi-stage workflows with actual agent interactions.
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from agent_system.services.session_service import SessionService
from agent_system.services.session_manager import SessionManager
from agent_system.mcp.base import MCPRegistry
from plugins.sub_agent_manager.manager import SubAgentManager


@pytest.fixture
def temp_storage(tmp_path):
    """Create temporary storage directory."""
    storage = tmp_path / "sessions"
    storage.mkdir()
    return storage


@pytest.fixture
async def session_service(temp_storage):
    """Create SessionService with temporary storage."""
    session_manager = SessionManager(storage_path=temp_storage)
    service = SessionService(session_manager=session_manager)
    return service


class MockAgentConfig:
    """Serializable mock for agent config."""
    def __init__(self, llm_profile="normal"):
        self.default_llm_profile = llm_profile
        self.llm_profile = llm_profile


class MockAgent:
    """Serializable mock for agent."""
    def __init__(self, name, llm_profile="normal"):
        self.name = name
        self.agent_config = MockAgentConfig(llm_profile)


@pytest.fixture
def mock_registry():
    """Create mock registry with multiple agent types."""
    registry = MagicMock(spec=MCPRegistry)

    def create_mock_agent(name, llm_profile="normal"):
        return MockAgent(name, llm_profile)

    # Register multiple agent types
    agents = {
        "web_research_agent": create_mock_agent("web_research_agent", "chat"),
        "financial_analyst_agent": create_mock_agent("financial_analyst_agent", "turbo"),
        "code_reviewer_agent": create_mock_agent("code_reviewer_agent", "normal"),
        "project_manager_agent": create_mock_agent("project_manager_agent", "normal"),
    }

    registry.get.side_effect = lambda name: agents.get(name)
    return registry


@pytest.fixture
async def sub_agent_manager(session_service, mock_registry):
    """Create SubAgentManager instance."""
    return SubAgentManager(
        session_service=session_service,
        registry=mock_registry,
        max_nesting_depth=5,
        max_sub_agents_per_type=20,  # High limit for integration tests
        max_sub_agents_per_session=20  # High limit for integration tests
    )


@pytest.mark.asyncio
async def test_e2e_create_and_continue_sub_agent(sub_agent_manager, session_service):
    """
    E2E Test: Create sub-agent and continue conversation.

    Workflow:
    1. Meta-agent creates research sub-agent
    2. Continue conversation with follow-up questions
    3. Verify context preservation
    """
    user_id = "test_user"
    parent_session_id = "meta_001"

    # Create parent session
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=parent_session_id,
        title="Meta Agent Session",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Stage 1: Create research sub-agent
    sub_id = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_session_id,
        agent_type="web_research_agent",
        initial_message="Research quantum computing companies",
        params=params
    )

    assert sub_id.startswith("sub_web_research_agent_")

    # Verify sub-agent was registered in parent metadata
    parent_data = await session_service.session_manager.load_session(user_id, parent_session_id)
    assert "sub_agents" in parent_data["metadata"]
    assert sub_id in parent_data["metadata"]["sub_agents"]

    sub_meta = parent_data["metadata"]["sub_agents"][sub_id]
    assert sub_meta["agent_type"] == "web_research_agent"
    assert sub_meta["status"] == "active"
    assert sub_meta["task_summary"] == "Research quantum computing companies"

    # Stage 2: Update last_used timestamp (simulate continue)
    await sub_agent_manager.update_sub_session_metadata(
        parent_session_id=parent_session_id,
        sub_session_id=sub_id,
        last_used="2025-11-02T12:00:00Z"
    )

    # Verify update
    parent_data = await session_service.session_manager.load_session(user_id, parent_session_id)
    assert parent_data["metadata"]["sub_agents"][sub_id]["last_used"] == "2025-11-02T12:00:00Z"

    # Stage 3: List sub-agents
    sub_agents = await sub_agent_manager.list_sub_sessions(
        parent_session_id=parent_session_id,
        include_completed=False
    )

    assert len(sub_agents) == 1
    assert sub_agents[0]["instance_id"] == sub_id


@pytest.mark.asyncio
async def test_e2e_multiple_sub_agents(sub_agent_manager, session_service):
    """
    E2E Test: Create multiple parallel sub-agents.

    Workflow:
    1. Create 3 different sub-agents in parallel
    2. Verify all are tracked independently
    3. Update and list all sub-agents
    """
    user_id = "test_user"
    parent_session_id = "meta_002"

    # Create parent session
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=parent_session_id,
        title="Multi-Agent Session",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Create 3 parallel sub-agents
    sub_ids = []
    agent_types = ["web_research_agent", "financial_analyst_agent", "code_reviewer_agent"]
    tasks = ["Research task", "Analysis task", "Review task"]

    for agent_type, task in zip(agent_types, tasks):
        sub_id = await sub_agent_manager.create_sub_session(
            parent_session_id=parent_session_id,
            agent_type=agent_type,
            initial_message=task,
            params=params
        )
        sub_ids.append(sub_id)

    # Verify all 3 sub-agents created
    assert len(sub_ids) == 3
    assert len(set(sub_ids)) == 3  # All unique

    # List all sub-agents
    sub_agents = await sub_agent_manager.list_sub_sessions(
        parent_session_id=parent_session_id,
        include_completed=True
    )

    assert len(sub_agents) == 3

    # Verify each agent type is present
    agent_types_in_list = [s["agent_type"] for s in sub_agents]
    assert "web_research_agent" in agent_types_in_list
    assert "financial_analyst_agent" in agent_types_in_list
    assert "code_reviewer_agent" in agent_types_in_list


@pytest.mark.asyncio
async def test_e2e_nested_sub_agents(sub_agent_manager, session_service):
    """
    E2E Test: Nested sub-agent creation (3 levels).

    Workflow:
    1. Meta-agent creates project_manager sub-agent (Level 2)
    2. Project manager creates web_research sub-agent (Level 3)
    3. Web research creates code_reviewer sub-agent (Level 4)
    4. Verify depth tracking at each level
    """
    user_id = "test_user"
    root_session_id = "root_003"

    # Create root session
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=root_session_id,
        title="Root Session",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Level 2: Meta creates project_manager
    level2_id = await sub_agent_manager.create_sub_session(
        parent_session_id=root_session_id,
        agent_type="project_manager_agent",
        initial_message="Manage project",
        params=params
    )

    # Verify Level 2 depth
    level2_data = await session_service.session_manager.load_session(user_id, level2_id)
    assert level2_data["depth"] == 2
    assert level2_data["parent_session"]["session_id"] == root_session_id

    # Level 3: Project manager creates web_research
    level3_id = await sub_agent_manager.create_sub_session(
        parent_session_id=level2_id,
        agent_type="web_research_agent",
        initial_message="Research requirements",
        params=params
    )

    # Verify Level 3 depth
    level3_data = await session_service.session_manager.load_session(user_id, level3_id)
    assert level3_data["depth"] == 3
    assert level3_data["parent_session"]["session_id"] == level2_id

    # Level 4: Web research creates code_reviewer
    level4_id = await sub_agent_manager.create_sub_session(
        parent_session_id=level3_id,
        agent_type="code_reviewer_agent",
        initial_message="Review code",
        params=params
    )

    # Verify Level 4 depth
    level4_data = await session_service.session_manager.load_session(user_id, level4_id)
    assert level4_data["depth"] == 4
    assert level4_data["parent_session"]["session_id"] == level3_id

    # Verify hierarchy chain
    assert level4_data["parent_session"]["session_id"] == level3_id
    assert level3_data["parent_session"]["session_id"] == level2_id
    assert level2_data["parent_session"]["session_id"] == root_session_id


@pytest.mark.asyncio
async def test_e2e_max_nesting_depth_enforcement(sub_agent_manager, session_service):
    """
    E2E Test: Verify max nesting depth is enforced.

    Workflow:
    1. Create nested sub-agents up to max depth (5)
    2. Attempt to exceed max depth
    3. Verify rejection with clear error
    """
    user_id = "test_user"
    root_session_id = "root_004"

    # Create root session (depth 1)
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=root_session_id,
        title="Root Session",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Create nested chain up to the full budget
    current_parent = root_session_id
    for depth in range(1, sub_agent_manager.max_nesting_depth + 1):
        sub_id = await sub_agent_manager.create_sub_session(
            parent_session_id=current_parent,
            agent_type="web_research_agent",
            initial_message=f"Level {depth}",
            params=params
        )

        # Verify depth
        sub_data = await session_service.session_manager.load_session(user_id, sub_id)
        assert sub_data["depth"] == depth + 1

        current_parent = sub_id

    # Attempt to exceed max depth - should fail
    with pytest.raises(ValueError, match="Maximum nesting depth"):
        await sub_agent_manager.create_sub_session(
            parent_session_id=current_parent,
            agent_type="web_research_agent",
            initial_message="Too deep",
            params=params
        )


@pytest.mark.asyncio
async def test_e2e_session_file_structure(sub_agent_manager, session_service, temp_storage):
    """
    E2E Test: Verify session file structure is correct.

    Workflow:
    1. Create sub-agent
    2. Inspect both parent and sub-agent session files
    3. Verify metadata links are bidirectional
    """
    user_id = "test_user"
    parent_session_id = "parent_005"

    # Create parent session
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=parent_session_id,
        title="Parent Session",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Create sub-agent
    sub_id = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_session_id,
        agent_type="web_research_agent",
        initial_message="Research task",
        params=params
    )

    # Load parent session file
    parent_file = temp_storage / user_id / f"{parent_session_id}.json"
    assert parent_file.exists()

    parent_data = await session_service.session_manager.load_session(user_id, parent_session_id)

    # Verify parent has sub-agent metadata
    assert "metadata" in parent_data
    assert "sub_agents" in parent_data["metadata"]
    assert sub_id in parent_data["metadata"]["sub_agents"]

    sub_meta = parent_data["metadata"]["sub_agents"][sub_id]
    assert "instance_id" in sub_meta
    assert "agent_type" in sub_meta
    assert "created_at" in sub_meta
    assert "last_used" in sub_meta
    assert "status" in sub_meta
    assert "task_summary" in sub_meta
    assert "depth" in sub_meta

    # Load sub-agent session file
    sub_file = temp_storage / user_id / f"{sub_id}.json"
    assert sub_file.exists()

    sub_data = await session_service.session_manager.load_session(user_id, sub_id)

    # Verify sub-agent has parent link
    assert "parent_session" in sub_data
    assert sub_data["parent_session"]["session_id"] == parent_session_id
    assert "created_at" in sub_data["parent_session"]

    # Verify sub-agent has depth
    assert "depth" in sub_data
    assert sub_data["depth"] == 2


@pytest.mark.asyncio
async def test_e2e_list_filtering(sub_agent_manager, session_service):
    """
    E2E Test: List filtering with include_completed flag.

    Workflow:
    1. Create 3 sub-agents
    2. Mark 1 as archived
    3. Test list with/without include_completed
    """
    user_id = "test_user"
    parent_session_id = "parent_006"

    # Create parent session
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=parent_session_id,
        title="Parent Session",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Create 3 sub-agents
    sub1 = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_session_id,
        agent_type="web_research_agent",
        initial_message="Task 1",
        params=params
    )

    sub2 = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_session_id,
        agent_type="financial_analyst_agent",
        initial_message="Task 2",
        params=params
    )

    sub3 = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_session_id,
        agent_type="code_reviewer_agent",
        initial_message="Task 3",
        params=params
    )

    # Mark sub2 as archived
    await sub_agent_manager.update_sub_session_metadata(
        parent_session_id=parent_session_id,
        sub_session_id=sub2,
        status="archived"
    )

    # List active only (default)
    active_list = await sub_agent_manager.list_sub_sessions(
        parent_session_id=parent_session_id,
        include_completed=False
    )

    assert len(active_list) == 2
    active_ids = [s["instance_id"] for s in active_list]
    assert sub1 in active_ids
    assert sub2 not in active_ids  # Archived, excluded
    assert sub3 in active_ids

    # List all including archived
    all_list = await sub_agent_manager.list_sub_sessions(
        parent_session_id=parent_session_id,
        include_completed=True
    )

    assert len(all_list) == 3
    all_ids = [s["instance_id"] for s in all_list]
    assert sub1 in all_ids
    assert sub2 in all_ids  # Now included
    assert sub3 in all_ids


@pytest.mark.asyncio
async def test_e2e_concurrent_sub_agent_creation(sub_agent_manager, session_service):
    """
    E2E Test: Create 10+ sub-agents concurrently.

    Verifies thread-safety and ID uniqueness.
    """
    user_id = "test_user"
    parent_session_id = "parent_007"

    # Create parent session
    await session_service.session_manager.create_session(
        user_id=user_id,
        session_id=parent_session_id,
        title="Concurrent Test",
        agent_name="meta_agent",
        llm_profile="normal"
    )

    parent_agent = MockAgent("meta_agent", "normal")
    params = {"_user_id": user_id, "_agent": parent_agent}

    # Create 5 sub-agents concurrently
    tasks = [
        sub_agent_manager.create_sub_session(
            parent_session_id=parent_session_id,
            agent_type="web_research_agent",
            initial_message=f"Concurrent task {i}",
            params=params
        )
        for i in range(15)
    ]

    sub_ids = await asyncio.gather(*tasks)

    # Verify all unique
    assert len(sub_ids) == 15
    assert len(set(sub_ids)) == 15

    # Verify all tracked in parent
    parent_data = await session_service.session_manager.load_session(user_id, parent_session_id)
    assert len(parent_data["metadata"]["sub_agents"]) == 15

    # Verify all have correct structure
    for sub_id in sub_ids:
        assert sub_id in parent_data["metadata"]["sub_agents"]
        sub_meta = parent_data["metadata"]["sub_agents"][sub_id]
        assert sub_meta["status"] == "active"
        assert sub_meta["agent_type"] == "web_research_agent"
