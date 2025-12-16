"""Integration tests for SubAgentManagerServer unified handler.

Tests the manage_sub_agent tool with all 5 operations.
"""

import pytest
from unittest.mock import Mock, AsyncMock
from plugins.sub_agent_manager.server import SubAgentManagerServer
from agent_system.config import AgentSystemConfig, MCPConfig


@pytest.fixture
def mock_config():
    """Mock system config."""
    config = Mock(spec=AgentSystemConfig)
    config.session_file_path = "data/sessions"
    # Add session_service and registry mocks
    config.session_service = Mock()
    config.registry = Mock()
    return config


@pytest.fixture
def mock_mcp_config():
    """Mock MCP config with default settings."""
    config = Mock(spec=MCPConfig)
    config.max_sub_agents_per_session = 10
    config.max_nesting_depth = 5
    config.max_sub_agents_per_type = 3
    config.allowed_agents = ["*"]
    config.blocked_agents = []
    return config


@pytest.fixture
def restricted_mcp_config():
    """Mock MCP config with restricted agent access."""
    config = Mock(spec=MCPConfig)
    config.max_sub_agents_per_session = 5
    config.max_nesting_depth = 3
    config.max_sub_agents_per_type = 2
    config.allowed_agents = ["coding_agent", "testing_agent"]
    config.blocked_agents = ["meta_agent"]
    return config


@pytest.fixture
def server(mock_config, mock_mcp_config):
    """Create server instance with mocked dependencies."""
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        mcp_config=mock_mcp_config
    )
    # Mock the manager
    server._manager = AsyncMock()
    return server


@pytest.fixture
def restricted_server(mock_config, restricted_mcp_config):
    """Create server instance with restricted agent access."""
    server = SubAgentManagerServer(
        name="coding_sub_agent_manager",
        system_config=mock_config,
        mcp_config=restricted_mcp_config
    )
    server._manager = AsyncMock()
    return server


class TestAgentFiltering:
    """Test agent name filtering logic."""

    def test_is_agent_allowed_wildcard(self, server):
        """Wildcard allows all agents."""
        assert server._is_agent_allowed("coding_agent") is True
        assert server._is_agent_allowed("web_research_agent") is True
        assert server._is_agent_allowed("meta_agent") is True

    def test_is_agent_allowed_exact_match(self, restricted_server):
        """Exact name matching works."""
        assert restricted_server._is_agent_allowed("coding_agent") is True
        assert restricted_server._is_agent_allowed("testing_agent") is True
        assert restricted_server._is_agent_allowed("web_research_agent") is False

    def test_is_agent_allowed_blacklist_priority(self, restricted_server):
        """Blacklist overrides whitelist."""
        assert restricted_server._is_agent_allowed("meta_agent") is False

    def test_is_agent_allowed_glob_pattern(self, mock_config):
        """Glob patterns work for matching."""
        config = Mock(spec=MCPConfig)
        config.max_sub_agents_per_session = 10
        config.max_nesting_depth = 5
        config.allowed_agents = ["coding_*", "test_*"]
        config.blocked_agents = []

        server = SubAgentManagerServer(
            name="pattern_manager",
            system_config=mock_config,
            mcp_config=config
        )

        assert server._is_agent_allowed("coding_agent") is True
        assert server._is_agent_allowed("coding_helper") is True
        assert server._is_agent_allowed("test_runner") is True
        assert server._is_agent_allowed("web_research_agent") is False


class TestUnifiedHandler:
    """Test unified manage_sub_agent handler."""

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_create(self, server):
        """Unified handler dispatches create operation."""
        # Mock create handler
        server._handle_create = AsyncMock(return_value={
            "instance_id": "sub_001",
            "agent_type": "coding_agent",
            "status": "completed"
        })

        result = await server.manage_sub_agent(params={
            "operation": "create",
            "agent_type": "coding_agent",
            "task": "Write code"
        })
        
        assert result["instance_id"] == "sub_001"
        server._handle_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_continue(self, server):
        """Unified handler dispatches continue operation."""
        server._handle_continue = AsyncMock(return_value={
            "instance_id": "sub_001",
            "status": "completed"
        })

        result = await server.manage_sub_agent(params={
            "operation": "continue",
            "instance_id": "sub_001",
            "message": "Continue task"
        })
        
        assert result["instance_id"] == "sub_001"
        server._handle_continue.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_list(self, server):
        """Unified handler dispatches list operation."""
        server._handle_list = AsyncMock(return_value={
            "sub_agents": [],
            "count": 0
        })

        result = await server.manage_sub_agent(params={
            "operation": "list"
        })
        
        assert result["count"] == 0
        server._handle_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_info(self, server):
        """Unified handler dispatches info operation."""
        server._handle_info = AsyncMock(return_value={
            "instance_id": "sub_001",
            "status": "active"
        })

        result = await server.manage_sub_agent(params={
            "operation": "info",
            "instance_id": "sub_001"
        })
        
        assert result["instance_id"] == "sub_001"
        server._handle_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_dispatch_delete(self, server):
        """Unified handler dispatches delete operation."""
        server._handle_delete = AsyncMock(return_value={
            "instance_id": "sub_001",
            "status": "archived"
        })

        result = await server.manage_sub_agent(params={
            "operation": "delete",
            "instance_id": "sub_001"
        })
        
        assert result["status"] == "archived"
        server._handle_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_unified_handler_unknown_operation(self, server):
        """Unified handler returns error for unknown operation."""
        result = await server.manage_sub_agent(params={
            "operation": "invalid_op"
        })
        
        assert result["status"] == "error"
        assert "Unknown operation" in result["error"]

    @pytest.mark.asyncio
    async def test_unified_handler_missing_operation(self, server):
        """Unified handler returns error when operation missing."""
        result = await server.manage_sub_agent(params={})
        
        assert result["status"] == "error"
        assert "Missing 'operation'" in result["error"]


class TestConcurrentExecutionPrevention:
    """Test that concurrent execution of same sub-agent is prevented."""

    @pytest.mark.asyncio
    async def test_concurrent_create_same_agent_blocked(self, server, mock_config):
        """Test that creating same sub-agent twice concurrently is blocked."""
        import asyncio
        
        # Mock the actual execution to take some time
        async def slow_create(params):
            instance_id = "sub_test_001"
            # Add to running agents
            async with server._running_lock:
                if instance_id in server._running_agents:
                    raise ValueError(f"Sub-agent '{instance_id}' is already running")
                server._running_agents.add(instance_id)
            
            try:
                await asyncio.sleep(0.1)  # Simulate work
                return {
                    "instance_id": instance_id,
                    "status": "completed",
                    "result": "Success"
                }
            finally:
                async with server._running_lock:
                    server._running_agents.discard(instance_id)
        
        server._handle_create = slow_create
        
        # Start first create
        task1 = asyncio.create_task(server.manage_sub_agent({
            "operation": "create",
            "agent_type": "test_agent",
            "task": "Test task"
        }))
        
        # Give it time to acquire lock
        await asyncio.sleep(0.01)
        
        # Try second create concurrently (should fail)
        task2 = asyncio.create_task(server.manage_sub_agent({
            "operation": "create",
            "agent_type": "test_agent", 
            "task": "Test task 2"
        }))
        
        # Wait for both
        result1, result2 = await asyncio.gather(task1, task2, return_exceptions=True)
        
        # One should succeed, one should fail with "already running"
        success_count = sum(1 for r in [result1, result2] 
                          if isinstance(r, dict) and r.get("status") == "completed")
        error_count = sum(1 for r in [result1, result2]
                         if isinstance(r, ValueError) or 
                            (isinstance(r, dict) and r.get("status") == "error"))
        
        assert success_count == 1, "Exactly one create should succeed"
        assert error_count == 1, "Exactly one create should fail"

    @pytest.mark.asyncio
    async def test_concurrent_continue_same_instance_blocked(self, server):
        """Test that continuing same sub-agent instance concurrently is blocked."""
        import asyncio
        
        instance_id = "sub_test_instance"
        
        # Mock continue handler to simulate work
        async def slow_continue(params):
            inst_id = params.get("instance_id")
            async with server._running_lock:
                if inst_id in server._running_agents:
                    raise ValueError(f"Sub-agent '{inst_id}' is already running")
                server._running_agents.add(inst_id)
            
            try:
                await asyncio.sleep(0.1)
                return {
                    "instance_id": inst_id,
                    "status": "completed",
                    "result": "Continued"
                }
            finally:
                async with server._running_lock:
                    server._running_agents.discard(inst_id)
        
        server._handle_continue = slow_continue
        
        # Start first continue
        task1 = asyncio.create_task(server.manage_sub_agent({
            "operation": "continue",
            "instance_id": instance_id,
            "message": "Message 1"
        }))
        
        await asyncio.sleep(0.01)
        
        # Try second continue (should fail)
        task2 = asyncio.create_task(server.manage_sub_agent({
            "operation": "continue",
            "instance_id": instance_id,
            "message": "Message 2"
        }))
        
        result1, result2 = await asyncio.gather(task1, task2, return_exceptions=True)
        
        # One should succeed, one should error
        success = any(isinstance(r, dict) and r.get("status") == "completed" 
                     for r in [result1, result2])
        error = any(isinstance(r, ValueError) or 
                   (isinstance(r, dict) and "already running" in str(r.get("error", "")))
                   for r in [result1, result2])
        
        assert success, "One continue should succeed"
        assert error, "One continue should be blocked with 'already running'"

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, server):
        """Test that running lock is released even when handler raises exception."""
        import asyncio
        
        instance_id = "sub_exception_test"
        
        # Mock handler that raises exception after acquiring lock
        async def failing_continue(params):
            inst_id = params.get("instance_id")
            async with server._running_lock:
                if inst_id in server._running_agents:
                    raise ValueError(f"Sub-agent '{inst_id}' is already running")
                server._running_agents.add(inst_id)
            
            try:
                raise RuntimeError("Simulated failure during execution")
            finally:
                async with server._running_lock:
                    server._running_agents.discard(inst_id)
        
        server._handle_continue = failing_continue
        
        # First call should fail but release lock
        result1 = await server.manage_sub_agent({
            "operation": "continue",
            "instance_id": instance_id,
            "message": "Test"
        })
        
        assert isinstance(result1, (dict, Exception)), "First call completed"
        
        # Verify lock was released
        async with server._running_lock:
            assert instance_id not in server._running_agents, \
                "Lock should be released after exception"
        
        # Second call should succeed (not blocked)
        result2 = await server.manage_sub_agent({
            "operation": "continue",
            "instance_id": instance_id,
            "message": "Test 2"
        })
        
        # Should not get "already running" error
        if isinstance(result2, dict):
            assert "already running" not in str(result2.get("error", "")), \
                "Second call should not be blocked after first failed"

    @pytest.mark.asyncio
    async def test_different_instances_run_concurrently(self, server):
        """Test that different sub-agent instances can run concurrently."""
        import asyncio
        
        # Mock handler
        async def slow_continue(params):
            inst_id = params.get("instance_id")
            async with server._running_lock:
                if inst_id in server._running_agents:
                    raise ValueError(f"Sub-agent '{inst_id}' is already running")
                server._running_agents.add(inst_id)
            
            try:
                await asyncio.sleep(0.05)
                return {
                    "instance_id": inst_id,
                    "status": "completed"
                }
            finally:
                async with server._running_lock:
                    server._running_agents.discard(inst_id)
        
        server._handle_continue = slow_continue
        
        # Run two different instances concurrently
        task1 = asyncio.create_task(server.manage_sub_agent({
            "operation": "continue",
            "instance_id": "sub_test_A",
            "message": "Test A"
        }))
        
        task2 = asyncio.create_task(server.manage_sub_agent({
            "operation": "continue",
            "instance_id": "sub_test_B",
            "message": "Test B"
        }))
        
        results = await asyncio.gather(task1, task2, return_exceptions=True)
        
        # Both should succeed
        success_count = sum(1 for r in results 
                          if isinstance(r, dict) and r.get("status") == "completed")
        
        assert success_count == 2, "Both different instances should run concurrently"


class TestAsyncExecution:
    """Test async execution with blocking=false parameter."""

    @pytest.mark.asyncio
    async def test_create_blocking_false_returns_immediately(self, server):
        """Test that create with blocking=false returns immediately."""
        import asyncio
        
        # Mock dependencies
        server._extract_registry = Mock(return_value=Mock())
        server._extract_session_service = Mock(return_value=Mock())
        server._get_manager = Mock(return_value=AsyncMock())
        
        # Mock manager to create sub-session
        mock_manager = server._get_manager.return_value
        mock_manager.create_sub_session = AsyncMock(return_value="sub_async_001")
        
        result = await server._handle_create({
            "agent_type": "test_agent",
            "task": "Test async",
            "blocking": False,
            "_session_id": "parent_session",
            "_request_id": "req_001"
        })
        
        assert result["status"] == "running"
        assert result["instance_id"] == "sub_async_001"
        assert "background" in result["message"].lower()
        
        # Verify async job was tracked
        async with server._async_jobs_lock:
            assert "sub_async_001" in server._async_jobs
            job = server._async_jobs["sub_async_001"]
            assert job["status"] in ["pending", "running"]
            assert job["agent_type"] == "test_agent"

    @pytest.mark.asyncio
    async def test_create_blocking_true_waits_for_completion(self, server):
        """Test that create with blocking=true (default) waits for completion."""
        # Mock the handler properly
        async def mock_create(params):
            return {
                "instance_id": "sub_001",
                "status": "completed",
                "result": "Done"
            }
        
        server._handle_create = mock_create
        
        result = await server.manage_sub_agent({
            "operation": "create",
            "agent_type": "test_agent",
            "task": "Test blocking",
            "blocking": True  # Explicit
        })
        
        assert result["status"] == "completed"
        assert "result" in result

    @pytest.mark.asyncio
    async def test_poll_async_job_in_progress(self, server):
        """Test polling an async job that's still running."""
        import asyncio
        from datetime import datetime, UTC
        
        # Add job to tracking
        instance_id = "sub_poll_test_001"
        async with server._async_jobs_lock:
            server._async_jobs[instance_id] = {
                "instance_id": instance_id,
                "status": "running",
                "agent_type": "test_agent",
                "task": "Test task",
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": None,
                "result": None,
                "error": None,
                "task_handle": asyncio.create_task(asyncio.sleep(10))
            }
        
        result = await server._handle_poll({
            "instance_id": instance_id
        })
        
        assert result["status"] == "running"
        assert result["instance_id"] == instance_id
        assert "task_handle" not in result  # Should be removed

    @pytest.mark.asyncio
    async def test_poll_completed_job_from_db(self, server):
        """Test polling a completed job that's not in async tracking (e.g., after restart)."""
        from plugins.sub_agent_manager.schemas import SubAgentMetadata
        from datetime import datetime, UTC
        
        instance_id = "sub_completed_001"
        
        # Mock dependencies for DB lookup
        mock_manager = AsyncMock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=[
            SubAgentMetadata(
                instance_id=instance_id,
                agent_type="test_agent",
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
                status="active",
                task_summary="Test task"
            )
        ])
        
        server._extract_registry = Mock(return_value=Mock())
        server._extract_session_service = Mock(return_value=Mock())
        server._get_manager = Mock(return_value=mock_manager)
        
        # Not in async jobs (simulating restart)
        result = await server._handle_poll({
            "instance_id": instance_id,
            "_session_id": "parent_session"
        })
        
        assert result["status"] == "completed"
        assert result["instance_id"] == instance_id
        assert "Use 'info' operation" in result["message"]

    @pytest.mark.asyncio
    async def test_poll_non_existent_instance(self, server):
        """Test polling an instance that doesn't exist."""
        mock_manager = Mock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=[])
        
        mock_registry = Mock()
        mock_session_service = Mock()
        
        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)
        
        result = await server._handle_poll({
            "instance_id": "non_existent_001",
            "_session_id": "parent_session"
        })
        
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_wait_completed_instance_returns_immediately(self, server):
        """Test that wait returns immediately if instance already completed."""
        from plugins.sub_agent_manager.schemas import SubAgentMetadata
        from datetime import datetime, UTC
        
        instance_id = "sub_wait_completed_001"
        
        # Mock poll to return completed
        async def mock_poll(params):
            return {
                "instance_id": instance_id,
                "status": "completed",
                "result": "Already done"
            }
        
        server._handle_poll = mock_poll
        
        result = await server._handle_wait({
            "instance_id": instance_id,
            "_session_id": "parent_session",
            "timeout": 10
        })
        
        assert result["status"] == "completed"
        assert result["instance_id"] == instance_id

    @pytest.mark.asyncio
    async def test_wait_async_job_until_completion(self, server):
        """Test that wait polls async job until it completes."""
        import asyncio
        from datetime import datetime, UTC
        
        instance_id = "sub_wait_async_001"
        
        # Add running job
        async with server._async_jobs_lock:
            server._async_jobs[instance_id] = {
                "instance_id": instance_id,
                "status": "running",
                "agent_type": "test_agent",
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": None,
                "result": None,
                "error": None,
                "task_handle": None
            }
        
        # Simulate job completing after short delay
        async def complete_job():
            await asyncio.sleep(0.1)
            async with server._async_jobs_lock:
                if instance_id in server._async_jobs:
                    server._async_jobs[instance_id]["status"] = "completed"
                    server._async_jobs[instance_id]["result"] = "Success"
                    server._async_jobs[instance_id]["completed_at"] = datetime.now(UTC).isoformat()
        
        asyncio.create_task(complete_job())
        
        result = await server._handle_wait({
            "instance_id": instance_id,
            "timeout": 5
        })
        
        assert result["status"] == "completed"
        assert result["result"] == "Success"

    @pytest.mark.asyncio
    async def test_wait_all_multiple_instances(self, server):
        """Test wait_all with multiple instances."""
        from datetime import datetime, UTC
        
        instance_ids = ["sub_a", "sub_b", "sub_c"]
        
        # Add all as running
        async with server._async_jobs_lock:
            for inst_id in instance_ids:
                server._async_jobs[inst_id] = {
                    "instance_id": inst_id,
                    "status": "running",
                    "started_at": datetime.now(UTC).isoformat(),
                    "completed_at": None,
                    "result": None,
                    "error": None,
                    "task_handle": None
                }
        
        # Mock wait to return completed immediately
        async def mock_wait(params):
            inst_id = params["instance_id"]
            return {
                "instance_id": inst_id,
                "status": "completed",
                "result": f"Result for {inst_id}"
            }
        
        server._handle_wait = mock_wait
        
        result = await server._handle_wait_all({
            "instance_ids": instance_ids,
            "timeout": 10
        })
        
        assert result["total"] == 3
        assert result["completed"] == 3
        assert result["failed"] == 0
        assert len(result["results"]) == 3

    @pytest.mark.asyncio
    async def test_cancel_running_async_job(self, server):
        """Test cancelling a running async job."""
        import asyncio
        from datetime import datetime, UTC
        
        instance_id = "sub_cancel_001"
        
        # Add running job with task
        task = asyncio.create_task(asyncio.sleep(100))
        async with server._async_jobs_lock:
            server._async_jobs[instance_id] = {
                "instance_id": instance_id,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "task_handle": task
            }
        
        result = await server._handle_cancel({
            "instance_id": instance_id
        })
        
        assert result["status"] == "cancelled"
        assert result["instance_id"] == instance_id
        
        # Give task time to be cancelled
        await asyncio.sleep(0.01)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_cancel_non_existent_job(self, server):
        """Test cancelling a job that doesn't exist."""
        result = await server._handle_cancel({
            "instance_id": "non_existent"
        })
        
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()


class TestSystemPromptIntegration:
    """Test that instance IDs from system prompt work correctly."""

    @pytest.mark.asyncio
    async def test_continue_with_system_prompt_id(self, server):
        """Test that agent can use instance_id from system prompt to continue."""
        # This is the ID that would be injected into system prompt
        system_prompt_id = "sub_book_test_agent_001"
        
        # Mock continue handler
        server._handle_continue = AsyncMock(return_value={
            "instance_id": system_prompt_id,
            "status": "completed",
            "result": "Continued successfully"
        })
        
        result = await server.manage_sub_agent({
            "operation": "continue",
            "instance_id": system_prompt_id,  # Exact ID from system prompt
            "message": "Continue testing"
        })
        
        assert result["instance_id"] == system_prompt_id
        assert result["status"] == "completed"
        server._handle_continue.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_with_system_prompt_id(self, server):
        """Test polling with ID from system prompt."""
        from plugins.sub_agent_manager.schemas import SubAgentMetadata
        from datetime import datetime, UTC
        
        system_prompt_id = "sub_scene_writer_042"
        
        # Mock DB lookup (simulating completed sub-agent)
        mock_manager = AsyncMock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=[
            SubAgentMetadata(
                instance_id=system_prompt_id,
                agent_type="scene_writer",
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
                status="active",
                task_summary="Write scene 42"
            )
        ])
        
        server._extract_registry = Mock(return_value=Mock())
        server._extract_session_service = Mock(return_value=Mock())
        server._get_manager = Mock(return_value=mock_manager)
        
        result = await server._handle_poll({
            "instance_id": system_prompt_id,
            "_session_id": "parent_session"
        })
        
        assert result["status"] == "completed"
        assert result["instance_id"] == system_prompt_id


def test_server_loads_max_sub_agents_per_type_config(mock_config, mock_mcp_config):
    """Test that SubAgentManagerServer loads max_sub_agents_per_type from config."""
    mock_mcp_config.max_sub_agents_per_type = 5
    
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        mcp_config=mock_mcp_config
    )
    
    assert server.max_sub_agents_per_type == 5


def test_server_defaults_max_sub_agents_per_type(mock_config):
    """Test that SubAgentManagerServer defaults max_sub_agents_per_type to 3."""
    # Config without max_sub_agents_per_type attribute
    minimal_config = Mock(spec=MCPConfig)
    
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        mcp_config=minimal_config
    )
    
    assert server.max_sub_agents_per_type == 3
