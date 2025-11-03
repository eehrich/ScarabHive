"""End-to-end integration tests for sub-agent lifecycle.

Tests the complete workflow:
1. Create coordinator session
2. Spawn sub-agent
3. Continue sub-agent conversation
4. Verify session files, metadata, and relationships
"""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
import json

from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.mcp.base import MCPRegistry
from plugins.sub_agent_manager.manager import SubAgentManager


@pytest.fixture
async def temp_session_storage():
    """Create temporary session storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
async def session_manager(temp_session_storage):
    """Create SessionManager with temporary storage."""
    return SessionManager(storage_path=str(temp_session_storage))


@pytest.fixture
async def session_service(session_manager):
    """Create SessionService."""
    service = SessionService(session_manager=session_manager)
    return service


@pytest.fixture
def sub_agent_manager(session_service):
    """Create SubAgentManager."""
    registry = MCPRegistry()
    return SubAgentManager(session_service, registry)


@pytest.mark.asyncio
async def test_e2e_create_and_continue_sub_agent(
    temp_session_storage,
    session_manager,
    session_service,
    sub_agent_manager
):
    """Test complete lifecycle: create coordinator, spawn sub-agent, continue conversation."""
    
    # Step 1: Create coordinator session
    coordinator_session_id = "coord_001"
    await session_manager.create_session(
        user_id="testuser",
        session_id=coordinator_session_id,
        title="Coordinator Session",
        agent_name="meta_agent",
        llm_profile="default"
    )
    
    # Verify coordinator session file exists
    coord_file = temp_session_storage / "testuser" / f"{coordinator_session_id}.json"
    assert coord_file.exists(), "Coordinator session file should exist"
    
    coord_data = await session_manager.load_session("testuser", coordinator_session_id)
    assert coord_data["session_id"] == coordinator_session_id
    assert coord_data["agent_name"] == "meta_agent"
    
    # Step 2: Create sub-agent
    sub_session_id = await sub_agent_manager.create_sub_session(
        parent_session_id=coordinator_session_id,
        agent_type="web_research_agent",
        initial_message="Research AI safety regulations",
        instance_label="research"
    )
    
    # Verify sub-agent session created
    assert sub_session_id is not None
    assert "web_research" in sub_session_id or "research" in sub_session_id
    
    # Verify sub-session file exists
    sub_file = temp_session_storage / "testuser" / f"{sub_session_id}.json"
    assert sub_file.exists(), f"Sub-agent session file should exist: {sub_file}"
    
    # Step 3: Verify parent-child relationship
    sub_data = await session_manager.load_session("testuser", sub_session_id)
    
    # Check parent link in sub-session
    assert "parent_session" in sub_data
    assert sub_data["parent_session"]["session_id"] == coordinator_session_id
    assert sub_data["depth"] == 2  # coordinator=1, sub-agent=2
    
    # Check sub-agent metadata in parent session
    coord_data_updated = await session_manager.load_session("testuser", coordinator_session_id)
    assert "metadata" in coord_data_updated
    assert "sub_agents" in coord_data_updated["metadata"]
    assert sub_session_id in coord_data_updated["metadata"]["sub_agents"]
    
    sub_metadata = coord_data_updated["metadata"]["sub_agents"][sub_session_id]
    assert sub_metadata["agent_type"] == "web_research_agent"
    assert sub_metadata["status"] == "active"
    assert "created_at" in sub_metadata
    assert "last_used" in sub_metadata
    
    # Step 4: Update sub-agent metadata (simulate continue)
    await sub_agent_manager.update_sub_session_metadata(
        parent_session_id=coordinator_session_id,
        sub_session_id=sub_session_id,
        status="active"  # Still active after continue
    )
    
    # Verify metadata updated
    coord_data_final = await session_manager.load_session("testuser", coordinator_session_id)
    sub_metadata_updated = coord_data_final["metadata"]["sub_agents"][sub_session_id]
    assert sub_metadata_updated["status"] == "active"
    
    # Step 5: List sub-agents
    sub_agents = await sub_agent_manager.list_sub_sessions(
        parent_session_id=coordinator_session_id,
        include_completed=False
    )
    
    assert len(sub_agents) == 1
    assert sub_agents[0]["instance_id"] == sub_session_id
    assert sub_agents[0]["agent_type"] == "web_research_agent"
    assert sub_agents[0]["status"] == "active"
    
    print(f"✅ E2E Test Passed:")
    print(f"   Coordinator: {coordinator_session_id}")
    print(f"   Sub-agent: {sub_session_id}")
    print(f"   Files: {len(list(temp_session_storage.rglob('*.json')))} JSON files")
    print(f"   Parent-child link: ✓")
    print(f"   Metadata sync: ✓")


@pytest.mark.asyncio
async def test_e2e_multiple_sub_agents(
    temp_session_storage,
    session_manager,
    session_service,
    sub_agent_manager
):
    """Test creating multiple sub-agents under one coordinator."""
    
    # Create coordinator
    coordinator_id = "coord_multi"
    await session_manager.create_session(
        user_id="admin",
        session_id=coordinator_id,
        title="Multi Sub-Agent Test",
        agent_name="meta_agent",
        llm_profile="default"
    )
    
    # Create multiple sub-agents
    sub_agents = []
    agent_types = [
        ("web_research_agent", "Research web sources"),
        ("financial_analyst_agent", "Analyze stock data"),
        ("code_review_agent", "Review Python code")
    ]
    
    for agent_type, task in agent_types:
        sub_id = await sub_agent_manager.create_sub_session(
            parent_session_id=coordinator_id,
            agent_type=agent_type,
            initial_message=task
        )
        sub_agents.append((sub_id, agent_type))
    
    # Verify all sub-agents created
    assert len(sub_agents) == 3
    
    # Verify all registered in parent metadata
    coord_data = await session_manager.load_session("admin", coordinator_id)
    parent_sub_agents = coord_data["metadata"]["sub_agents"]
    
    assert len(parent_sub_agents) == 3
    
    for sub_id, expected_type in sub_agents:
        assert sub_id in parent_sub_agents
        assert parent_sub_agents[sub_id]["agent_type"] == expected_type
    
    # List active sub-agents
    active_subs = await sub_agent_manager.list_sub_sessions(
        parent_session_id=coordinator_id,
        include_completed=False
    )
    
    assert len(active_subs) == 3
    
    print(f"✅ Multiple Sub-Agents Test Passed:")
    print(f"   Created: {len(sub_agents)} sub-agents")
    print(f"   Active: {len(active_subs)} sub-agents")


@pytest.mark.asyncio
async def test_e2e_nested_sub_agents(
    temp_session_storage,
    session_manager,
    session_service,
    sub_agent_manager
):
    """Test nested sub-agents (sub-agent creating its own sub-agent)."""
    
    # Level 1: Root coordinator
    root_id = "root_001"
    await session_manager.create_session(
        user_id="admin",
        session_id=root_id,
        title="Root Coordinator",
        agent_name="meta_agent",
        llm_profile="default"
    )
    
    # Level 2: First sub-agent
    level2_id = await sub_agent_manager.create_sub_session(
        parent_session_id=root_id,
        agent_type="project_manager_agent",
        initial_message="Manage research project"
    )
    
    # Level 3: Sub-agent of sub-agent
    level3_id = await sub_agent_manager.create_sub_session(
        parent_session_id=level2_id,
        agent_type="researcher_agent",
        initial_message="Deep dive into topic X"
    )
    
    # Verify depths
    root_data = await session_manager.load_session("admin", root_id)
    level2_data = await session_manager.load_session("admin", level2_id)
    level3_data = await session_manager.load_session("admin", level3_id)
    
    assert root_data.get("depth", 1) == 1
    assert level2_data["depth"] == 2
    assert level3_data["depth"] == 3
    
    # Verify parent links
    assert level2_data["parent_session"]["session_id"] == root_id
    assert level3_data["parent_session"]["session_id"] == level2_id
    
    # Verify metadata propagation
    assert level2_id in root_data["metadata"]["sub_agents"]
    assert level3_id in level2_data["metadata"]["sub_agents"]
    
    print(f"✅ Nested Sub-Agents Test Passed:")
    print(f"   Level 1 (root): {root_id} (depth={root_data.get('depth', 1)})")
    print(f"   Level 2: {level2_id} (depth={level2_data['depth']})")
    print(f"   Level 3: {level3_id} (depth={level3_data['depth']})")


@pytest.mark.asyncio
async def test_e2e_max_nesting_depth_enforcement(
    temp_session_storage,
    session_manager,
    session_service
):
    """Test that max nesting depth is enforced."""
    
    # Create manager with max_depth=3
    registry = MCPRegistry()
    manager = SubAgentManager(session_service, registry, max_nesting_depth=3)
    
    # Level 1: Root
    root_id = "depth_root"
    await session_manager.create_session(
        user_id="admin",
        session_id=root_id,
        title="Depth Test Root",
        agent_name="meta_agent",
        llm_profile="default"
    )
    
    # Level 2: OK
    level2_id = await manager.create_sub_session(
        parent_session_id=root_id,
        agent_type="agent_l2",
        initial_message="Level 2"
    )
    
    # Level 3: OK (at max depth)
    level3_id = await manager.create_sub_session(
        parent_session_id=level2_id,
        agent_type="agent_l3",
        initial_message="Level 3"
    )
    
    # Level 4: Should fail (exceeds max depth)
    with pytest.raises(ValueError, match="Maximum nesting depth"):
        await manager.create_sub_session(
            parent_session_id=level3_id,
            agent_type="agent_l4",
            initial_message="Level 4 - should fail"
        )
    
    print(f"✅ Max Nesting Depth Test Passed:")
    print(f"   Max depth: 3")
    print(f"   Level 2 & 3: ✓ Created")
    print(f"   Level 4: ✗ Rejected (as expected)")


@pytest.mark.asyncio
async def test_e2e_session_file_structure(
    temp_session_storage,
    session_manager,
    session_service,
    sub_agent_manager
):
    """Test that session files have correct structure and can be loaded."""
    
    # Create sessions
    parent_id = "file_struct_parent"
    await session_manager.create_session(
        user_id="admin",
        session_id=parent_id,
        title="File Structure Test",
        agent_name="coordinator",
        llm_profile="default"
    )
    
    sub_id = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_id,
        agent_type="worker_agent",
        initial_message="Do work"
    )
    
    # Load and verify parent file structure
    parent_file = temp_session_storage / "admin" / f"{parent_id}.json"
    with open(parent_file, 'r', encoding='utf-8') as f:
        parent_json = json.load(f)
    
    # Required fields in parent
    assert "session_id" in parent_json
    assert "user_id" in parent_json
    assert "agent_name" in parent_json
    assert "messages" in parent_json
    assert "metadata" in parent_json
    assert "sub_agents" in parent_json["metadata"]
    
    # Load and verify sub-agent file structure
    sub_file = temp_session_storage / "admin" / f"{sub_id}.json"
    with open(sub_file, 'r', encoding='utf-8') as f:
        sub_json = json.load(f)
    
    # Required fields in sub-agent
    assert "session_id" in sub_json
    assert "user_id" in sub_json
    assert "agent_name" in sub_json
    assert "parent_session" in sub_json
    assert "depth" in sub_json
    assert sub_json["parent_session"]["session_id"] == parent_id
    assert sub_json["depth"] == 2
    
    # Verify metadata structure in parent
    sub_meta = parent_json["metadata"]["sub_agents"][sub_id]
    assert "instance_id" in sub_meta
    assert "agent_type" in sub_meta
    assert "status" in sub_meta
    assert "created_at" in sub_meta
    assert "last_used" in sub_meta
    assert "task_summary" in sub_meta
    
    print(f"✅ Session File Structure Test Passed:")
    print(f"   Parent file: {parent_file.name}")
    print(f"   Sub-agent file: {sub_file.name}")
    print(f"   All required fields present: ✓")


@pytest.mark.asyncio
async def test_e2e_list_filtering(
    temp_session_storage,
    session_manager,
    session_service,
    sub_agent_manager
):
    """Test listing sub-agents with status filtering."""
    
    # Create parent
    parent_id = "filter_parent"
    await session_manager.create_session(
        user_id="admin",
        session_id=parent_id,
        title="Filter Test",
        agent_name="meta",
        llm_profile="default"
    )
    
    # Create 3 sub-agents
    sub1 = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_id,
        agent_type="agent1",
        initial_message="Task 1"
    )
    
    sub2 = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_id,
        agent_type="agent2",
        initial_message="Task 2"
    )
    
    sub3 = await sub_agent_manager.create_sub_session(
        parent_session_id=parent_id,
        agent_type="agent3",
        initial_message="Task 3"
    )
    
    # Mark one as completed
    await sub_agent_manager.update_sub_session_metadata(
        parent_session_id=parent_id,
        sub_session_id=sub2,
        status="completed"
    )
    
    # List active only (default)
    active_only = await sub_agent_manager.list_sub_sessions(
        parent_session_id=parent_id,
        include_completed=False
    )
    
    assert len(active_only) == 2
    active_ids = [s["instance_id"] for s in active_only]
    assert sub1 in active_ids
    assert sub3 in active_ids
    assert sub2 not in active_ids
    
    # List all (including completed)
    all_subs = await sub_agent_manager.list_sub_sessions(
        parent_session_id=parent_id,
        include_completed=True
    )
    
    assert len(all_subs) == 3
    all_ids = [s["instance_id"] for s in all_subs]
    assert sub1 in all_ids
    assert sub2 in all_ids
    assert sub3 in all_ids
    
    print(f"✅ List Filtering Test Passed:")
    print(f"   Total created: 3")
    print(f"   Active only: {len(active_only)}")
    print(f"   Including completed: {len(all_subs)}")
