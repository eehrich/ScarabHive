"""Integration tests for SubAgentManagerServer unified handler.

Tests the manage_sub_agent tool with all 5 operations.
"""

import logging
from functools import partial
import pytest
from unittest.mock import Mock, AsyncMock
from plugins.sub_agent_manager.manager import SubAgentManager
from plugins.sub_agent_manager.server import SubAgentManagerServer
from plugins.sub_agent_manager import server as sam_server
from agent_system.config import AgentSystemConfig, ToolServerConfig


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
def mock_server_config():
    """Mock tool server config with default settings."""
    config = Mock(spec=ToolServerConfig)
    config.max_sub_agents_per_session = 10
    config.max_nesting_depth = 5
    config.max_sub_agents_per_type = 3
    config.allowed_agents = ["*"]
    config.blocked_agents = []
    return config


@pytest.fixture
def restricted_server_config():
    """Mock tool server config with restricted agent access."""
    config = Mock(spec=ToolServerConfig)
    config.max_sub_agents_per_session = 5
    config.max_nesting_depth = 3
    config.max_sub_agents_per_type = 2
    config.allowed_agents = ["coding_agent", "testing_agent"]
    config.blocked_agents = ["meta_agent"]
    return config


@pytest.fixture
def server(mock_config, mock_server_config):
    """Create server instance with mocked dependencies."""
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        server_config=mock_server_config
    )
    # Don't set _manager here - tests that need it will mock it themselves
    return server


@pytest.fixture
def restricted_server(mock_config, restricted_server_config):
    """Create server instance with restricted agent access."""
    server = SubAgentManagerServer(
        name="coding_sub_agent_manager",
        system_config=mock_config,
        server_config=restricted_server_config
    )
    # Don't set _manager here - tests that need it will mock it themselves
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
        config = Mock(spec=ToolServerConfig)
        config.max_sub_agents_per_session = 10
        config.max_nesting_depth = 5
        config.allowed_agents = ["coding_*", "test_*"]
        config.blocked_agents = []

        server = SubAgentManagerServer(
            name="pattern_manager",
            system_config=mock_config,
            server_config=config
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


class TestOperationInference:
    """Safe inference of `operation` when the model omits it (saves a turn)."""

    def test_infer_create_from_agent_type(self, server):
        assert server._infer_operation({"agent_type": "coding_agent"}) == "create"
        assert server._infer_operation({"agent_type": "coding_agent", "task": "go"}) == "create"

    def test_infer_continue_from_id_and_message(self, server):
        assert server._infer_operation({"instance_id": "sub_1", "message": "weiter"}) == "continue"

    def test_infer_continue_accepts_task_as_prompt(self, server):
        # model used the wrong field name (task) for the follow-up prompt
        assert server._infer_operation({"instance_id": "sub_1", "task": "weiter"}) == "continue"

    def test_no_infer_when_ambiguous(self, server):
        assert server._infer_operation({"instance_id": "sub_1"}) is None            # poll/info/cancel/delete unknowable
        assert server._infer_operation({"agent_type": "a", "instance_id": "b"}) is None
        assert server._infer_operation({}) is None

    @pytest.mark.asyncio
    async def test_missing_operation_infers_create(self, server):
        server._handle_create = AsyncMock(return_value={"status": "completed"})
        result = await server.manage_sub_agent(params={
            "agent_type": "coding_agent", "task": "Write code",  # no 'operation'
        })
        server._handle_create.assert_called_once()
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_missing_operation_infers_continue(self, server):
        server._handle_continue = AsyncMock(return_value={"status": "completed"})
        result = await server.manage_sub_agent(params={
            "instance_id": "sub_1", "message": "weiter",  # no 'operation'
        })
        server._handle_continue.assert_called_once()
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_missing_operation_ambiguous_still_errors(self, server):
        result = await server.manage_sub_agent(params={"instance_id": "sub_1"})
        assert result["status"] == "error"
        assert "operation" in result["error"].lower()


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
        
        # Mock dependencies
        server._extract_registry = Mock(return_value=Mock())
        server._extract_session_service = Mock(return_value=Mock())
        server._get_manager = Mock(return_value=AsyncMock())
        
        # Mock manager to create sub-session
        mock_manager = server._get_manager.return_value
        mock_manager._extract_user_id = Mock(return_value="test_user")
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
        from datetime import datetime, UTC
        
        instance_id = "sub_completed_001"
        
        # Save originals for cleanup
        orig_extract_registry = server._extract_registry
        orig_extract_session_service = server._extract_session_service
        orig_get_manager = server._get_manager
        
        try:
            # Mock dependencies for DB lookup - return as dict (simpler than SubAgentMetadata)
            mock_manager = AsyncMock()
            mock_manager.list_sub_sessions = AsyncMock(return_value=[
                {
                    "instance_id": instance_id,
                    "agent_type": "test_agent",
                    "created_at": datetime.now(UTC).isoformat(),
                    "last_used": datetime.now(UTC).isoformat(),
                    "status": "active",  # Session status (active/archived), not execution status
                    "task_summary": "Test task",
                    "message_count": 5
                }
            ])
            
            server._extract_registry = Mock(return_value=Mock())
            server._extract_session_service = Mock(return_value=Mock())
            server._get_manager = Mock(return_value=mock_manager)
            
            # Not in async jobs (simulating restart where execution completed but session persists)
            result = await server._handle_poll({
                "instance_id": instance_id,
                "_session_id": "parent_session"
            })
            
            # Poll should return "completed" because instance exists in DB but not in _async_jobs
            assert result["status"] == "completed"
            assert result["instance_id"] == instance_id
            assert "Use 'info' operation" in result["message"]
        finally:
            # Restore originals
            server._extract_registry = orig_extract_registry
            server._extract_session_service = orig_extract_session_service
            server._get_manager = orig_get_manager

    @pytest.mark.asyncio
    async def test_poll_non_existent_instance(self, server):
        """Test polling an instance that doesn't exist."""
        # Save originals for cleanup
        orig_extract_registry = server._extract_registry
        orig_extract_session_service = server._extract_session_service
        orig_get_manager = server._get_manager
        
        try:
            mock_manager = AsyncMock()
            mock_manager.list_sub_sessions = AsyncMock(return_value=[])
            
            server._extract_registry = Mock(return_value=Mock())
            server._extract_session_service = Mock(return_value=Mock())
            server._get_manager = Mock(return_value=mock_manager)
            
            result = await server._handle_poll({
                "instance_id": "non_existent_001",
                "_session_id": "parent_session"
            })
            
            assert result["status"] == "error"
            assert "not found" in result["error"].lower()
        finally:
            # Restore originals
            server._extract_registry = orig_extract_registry
            server._extract_session_service = orig_extract_session_service
            server._get_manager = orig_get_manager

    @pytest.mark.asyncio
    async def test_wait_completed_instance_returns_immediately(self, server):
        """Test that wait returns immediately if instance already completed."""
        
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
    async def test_wait_all_hands_each_wait_the_caller_it_was_given(self, server):
        """It used to rebuild the params from four keys: the waits below ran without the caller's
        user (so a lookup fell back to scanning the session directories), without its request and
        without its cancellation token."""
        seen = []

        async def mock_wait(params):
            seen.append(params)
            return {"instance_id": params["instance_id"], "status": "completed"}

        server._handle_wait = mock_wait
        await server._handle_wait_all({
            "operation": "wait_all", "instance_ids": ["sub_a", "sub_b"],
            "_session_id": "parent1", "_user_id": "u1", "_request_id": "req1",
            "_cancellation_token": "token", "_status": AsyncMock(),
        })

        assert [p["instance_id"] for p in seen] == ["sub_a", "sub_b"]
        assert all(p.get("_user_id") == "u1" and p.get("_request_id") == "req1"
                   and p.get("_cancellation_token") == "token" for p in seen), seen
        assert all("_status" not in p for p in seen), "wait_all owns the status line of this call"

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
    async def test_async_job_cancelled_by_exception_persists_status(self, server):
        """Test that CancelledError in async job persists 'cancelled' status to DB."""
        import asyncio
        
        instance_id = "sub_cancel_exception_001"
        parent_session_id = "parent_session_456"
        
        # Mock manager to verify update_sub_session_metadata is called
        mock_manager = AsyncMock()
        mock_manager.update_sub_session_metadata = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="test_user")
        
        # Mock agent that will be cancelled
        mock_agent = AsyncMock()
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.default_llm_profile = "normal"
        mock_agent._session_tracker = Mock()
        mock_agent._session_tracker.set_session_metadata = Mock()
        
        # Mock run_events to hang so we can cancel it
        async def hanging_run_events(*args, **kwargs):
            await asyncio.sleep(100)  # Hang forever
            yield  # Never reached
            
        mock_agent.run_events = hanging_run_events
        
        mock_registry = Mock()
        mock_registry.get = Mock(return_value=mock_agent)
        
        mock_session_service = Mock()
        
        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)
        
        # Trigger CancelledError by calling _execute_async_job and cancelling it
        params = {
            "_session_id": parent_session_id,
            "_request_id": "req_001",
        }
        
        # Create task and let it start
        task = asyncio.create_task(
            server._execute_async_job(
                instance_id=instance_id,
                params=params,
                agent_name="test_agent",
                task="Test task",
                use_advanced_model=False
            )
        )
        
        # Give it time to start
        await asyncio.sleep(0.05)
        
        # Cancel it
        task.cancel()
        
        # Wait for cancellation to complete
        with pytest.raises(asyncio.CancelledError):
            await task
        
        # Verify DB update was called
        await asyncio.sleep(0.05)  # Give time for cleanup
        mock_manager.update_sub_session_metadata.assert_called()
        
        # Find the call with status="cancelled"
        calls = mock_manager.update_sub_session_metadata.call_args_list
        cancelled_call = None
        for call in calls:
            if call.kwargs.get("status") == "cancelled":
                cancelled_call = call
                break
        
        assert cancelled_call is not None, "update_sub_session_metadata should be called with status='cancelled'"
        assert cancelled_call.kwargs["parent_session_id"] == parent_session_id
        assert cancelled_call.kwargs["sub_session_id"] == instance_id
        assert "completed_at" in cancelled_call.kwargs

    @pytest.mark.asyncio
    async def test_cancel_non_existent_job(self, server):
        """Test cancelling a job that doesn't exist."""
        result = await server._handle_cancel({
            "instance_id": "non_existent"
        })
        
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_cancel_persists_status_to_db(self, server):
        """Test that cancelling a job persists 'cancelled' status to database."""
        import asyncio
        from datetime import datetime, UTC
        
        instance_id = "sub_cancel_persist_001"
        parent_session_id = "parent_session_123"
        
        # Add running job with task and parent_session_id
        task = asyncio.create_task(asyncio.sleep(100))
        async with server._async_jobs_lock:
            server._async_jobs[instance_id] = {
                "instance_id": instance_id,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "task_handle": task,
                "parent_session_id": parent_session_id
            }
        
        # Mock manager to verify update_sub_session_metadata is called
        mock_manager = AsyncMock()
        mock_manager.update_sub_session_metadata = AsyncMock()
        
        server._extract_registry = Mock(return_value=Mock())
        server._extract_session_service = Mock(return_value=Mock())
        server._get_manager = Mock(return_value=mock_manager)
        
        result = await server._handle_cancel({
            "instance_id": instance_id,
            "_session_id": parent_session_id
        })
        
        assert result["status"] == "cancelled"
        assert result["instance_id"] == instance_id
        
        # Verify DB update was called
        mock_manager.update_sub_session_metadata.assert_called_once()
        call_args = mock_manager.update_sub_session_metadata.call_args
        assert call_args.kwargs["parent_session_id"] == parent_session_id
        assert call_args.kwargs["sub_session_id"] == instance_id
        assert call_args.kwargs["status"] == "cancelled"
        assert "completed_at" in call_args.kwargs
        
        # Verify task was cancelled
        await asyncio.sleep(0.01)
        assert task.cancelled()


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


def test_server_loads_max_sub_agents_per_type_config(mock_config, mock_server_config):
    """Test that SubAgentManagerServer loads max_sub_agents_per_type from config."""
    mock_server_config.max_sub_agents_per_type = 5
    
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        server_config=mock_server_config
    )
    
    assert server.max_sub_agents_per_type == 5


def test_server_defaults_max_sub_agents_per_type(mock_config):
    """Test that SubAgentManagerServer defaults max_sub_agents_per_type to 3."""
    # Config without max_sub_agents_per_type attribute
    minimal_config = Mock(spec=ToolServerConfig)
    
    server = SubAgentManagerServer(
        name="sub_agent_manager",
        system_config=mock_config,
        server_config=minimal_config
    )
    
    assert server.max_sub_agents_per_type == 3


class TestOrphanedRunningAgents:
    """Test cleanup of orphaned 'running' sub-agents after server restart."""

    def test_is_agent_running_returns_false_for_nonexistent(self, server):
        """Test that is_agent_running() returns False for non-existent agent."""
        assert server.is_agent_running("nonexistent_id") is False

    @pytest.mark.asyncio
    async def test_is_agent_running_returns_true_for_active(self, server):
        """Test that is_agent_running() returns True for active async job."""
        import asyncio
        
        instance_id = "test_running_001"
        
        # Add to async jobs
        task = asyncio.create_task(asyncio.sleep(100))
        server._async_jobs[instance_id] = {
            "instance_id": instance_id,
            "status": "running",
            "task_handle": task
        }
        
        try:
            assert server.is_agent_running(instance_id) is True
        finally:
            # Cleanup
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del server._async_jobs[instance_id]

    @pytest.mark.asyncio
    async def test_list_marks_orphaned_running_as_interrupted(self, server):
        """Test that listing marks orphaned 'running' sub-agents as 'interrupted'."""
        parent_session_id = "parent_orphan_001"
        instance_id = "sub_orphaned_001"
        
        # Mock manager with orphaned running agent
        mock_manager = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="test_user")
        mock_manager.list_sub_sessions = AsyncMock(return_value=[
            {
                "instance_id": instance_id,
                "agent_type": "test_agent",
                "status": "running",  # Status in DB
                "created_at": "2025-12-17T10:00:00Z",
                "last_used": "2025-12-17T10:05:00Z",
                "task_summary": "Test task",
                "message_count": 3
            }
        ])
        mock_manager.update_sub_session_metadata = AsyncMock()
        
        # Mock session service
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(return_value={
            "messages": [1, 2, 3]
        })
        mock_session_service = Mock()
        mock_session_service.session_manager = mock_session_manager
        
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)
        
        # Agent is NOT in _async_jobs (orphaned)
        assert instance_id not in server._async_jobs
        
        # List sub-agents
        result = await server._handle_list({
            "_session_id": parent_session_id,
            "include_completed": False
        })
        
        # Should have marked as interrupted
        assert result["count"] == 1
        assert result["instances"][0]["status"] == "interrupted"
        assert result["instances"][0]["instance_id"] == instance_id
        
        # Verify DB update was called
        mock_manager.update_sub_session_metadata.assert_called_once()
        call_kwargs = mock_manager.update_sub_session_metadata.call_args.kwargs
        assert call_kwargs["parent_session_id"] == parent_session_id
        assert call_kwargs["sub_session_id"] == instance_id
        assert call_kwargs["status"] == "interrupted"
        assert "completed_at" in call_kwargs
        assert "Server restarted" in call_kwargs["error"]

    @pytest.mark.asyncio
    async def test_list_keeps_actually_running_agents(self, server):
        """Test that list does NOT mark truly running agents as interrupted."""
        import asyncio
        
        parent_session_id = "parent_active_001"
        instance_id = "sub_active_001"
        
        # Add to async jobs (agent is actually running)
        server._async_jobs[instance_id] = {
            "instance_id": instance_id,
            "status": "running",
            "task_handle": asyncio.create_task(asyncio.sleep(100))
        }
        
        try:
            # Mock manager
            mock_manager = AsyncMock()
            mock_manager._extract_user_id = Mock(return_value="test_user")
            mock_manager.list_sub_sessions = AsyncMock(return_value=[
                {
                    "instance_id": instance_id,
                    "agent_type": "test_agent",
                    "status": "running",
                    "created_at": "2025-12-17T11:00:00Z",
                    "last_used": "2025-12-17T11:05:00Z",
                    "task_summary": "Active task",
                    "message_count": 5
                }
            ])
            mock_manager.update_sub_session_metadata = AsyncMock()
            
            # Mock session service
            mock_session_manager = AsyncMock()
            mock_session_manager.load_session = AsyncMock(return_value={
                "messages": [1, 2, 3, 4, 5]
            })
            mock_session_service = Mock()
            mock_session_service.session_manager = mock_session_manager
            
            server._extract_session_service = Mock(return_value=mock_session_service)
            server._get_manager = Mock(return_value=mock_manager)
            
            # List sub-agents
            result = await server._handle_list({
                "_session_id": parent_session_id,
                "include_completed": False
            })
            
            # Should keep status as "running"
            assert result["count"] == 1
            assert result["instances"][0]["status"] == "running"
            assert result["instances"][0]["instance_id"] == instance_id
            
            # DB update should NOT be called
            mock_manager.update_sub_session_metadata.assert_not_called()
            
        finally:
            # Cleanup
            if instance_id in server._async_jobs:
                server._async_jobs[instance_id]["task_handle"].cancel()
                del server._async_jobs[instance_id]

    @pytest.mark.asyncio
    async def test_list_marks_orphaned_pending_as_interrupted(self, server):
        """Test that orphaned 'pending' agents are also marked as interrupted."""
        parent_session_id = "parent_orphan_002"
        instance_id = "sub_orphaned_002"
        
        # Mock manager with orphaned pending agent
        mock_manager = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="test_user")
        mock_manager.list_sub_sessions = AsyncMock(return_value=[
            {
                "instance_id": instance_id,
                "agent_type": "test_agent",
                "status": "pending",  # Also check pending status
                "created_at": "2025-12-17T12:00:00Z",
                "last_used": "2025-12-17T12:01:00Z",
                "task_summary": "Pending task",
                "message_count": 1
            }
        ])
        mock_manager.update_sub_session_metadata = AsyncMock()
        
        # Mock session service
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(return_value={"messages": [1]})
        mock_session_service = Mock()
        mock_session_service.session_manager = mock_session_manager
        
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)
        
        # Agent is NOT in _async_jobs
        assert instance_id not in server._async_jobs
        
        # List sub-agents
        result = await server._handle_list({
            "_session_id": parent_session_id,
            "include_completed": False
        })
        
        # Should mark pending as interrupted too
        assert result["count"] == 1
        assert result["instances"][0]["status"] == "interrupted"
        
        # Verify DB update
        mock_manager.update_sub_session_metadata.assert_called_once()
        call_kwargs = mock_manager.update_sub_session_metadata.call_args.kwargs
        assert call_kwargs["status"] == "interrupted"

    @pytest.mark.asyncio
    async def test_list_ignores_completed_agents(self, server):
        """Test that completed/cancelled/failed agents are not touched."""
        parent_session_id = "parent_completed_001"
        
        # Mock manager with various completed agents
        mock_manager = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="test_user")
        mock_manager.list_sub_sessions = AsyncMock(return_value=[
            {
                "instance_id": "sub_completed",
                "agent_type": "test_agent",
                "status": "completed",
                "created_at": "2025-12-17T10:00:00Z",
                "last_used": "2025-12-17T10:10:00Z",
                "task_summary": "Done",
                "message_count": 10
            },
            {
                "instance_id": "sub_cancelled",
                "agent_type": "test_agent",
                "status": "cancelled",
                "created_at": "2025-12-17T11:00:00Z",
                "last_used": "2025-12-17T11:05:00Z",
                "task_summary": "Cancelled",
                "message_count": 5
            },
            {
                "instance_id": "sub_failed",
                "agent_type": "test_agent",
                "status": "failed",
                "created_at": "2025-12-17T12:00:00Z",
                "last_used": "2025-12-17T12:02:00Z",
                "task_summary": "Failed",
                "message_count": 2
            }
        ])
        mock_manager.update_sub_session_metadata = AsyncMock()
        
        # Mock session service
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(side_effect=[
            {"messages": list(range(10))},
            {"messages": list(range(5))},
            {"messages": list(range(2))}
        ])
        mock_session_service = Mock()
        mock_session_service.session_manager = mock_session_manager
        
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)
        
        # List with include_completed=True
        result = await server._handle_list({
            "_session_id": parent_session_id,
            "include_completed": True
        })
        
        # All agents should keep their original status
        assert result["count"] == 3
        statuses = [inst["status"] for inst in result["instances"]]
        assert "completed" in statuses
        assert "cancelled" in statuses
        assert "failed" in statuses
        
        # No DB updates (no orphaned running/pending agents)
        mock_manager.update_sub_session_metadata.assert_not_called()


class TestMinResultLengthGuard:
    """Test auto-retry when sub-agent result is below min_result_length threshold."""

    @pytest.fixture
    def min_len_server(self, mock_config):
        """Server with min_result_length_by_agent configured."""
        config = Mock(spec=ToolServerConfig)
        config.max_sub_agents_per_session = 10
        config.max_nesting_depth = 5
        config.max_sub_agents_per_type = 3
        config.allowed_agents = ["*"]
        config.blocked_agents = []
        config.min_result_length_by_agent = {"test_writer": 100}
        config.min_result_retries = 2
        return SubAgentManagerServer(
            name="test_sam",
            system_config=mock_config,
            server_config=config,
        )

    def _make_blocking_mocks(self, server, run_events_fn):
        """Set up all mocks for a blocking _handle_create call."""
        mock_agent = Mock()
        mock_agent.run_events = run_events_fn
        mock_agent.agent_config = Mock()
        mock_agent.agent_config.default_llm_profile = "test"
        mock_agent._session_tracker = Mock()
        mock_agent._session_tracker.set_session_metadata = Mock()
        mock_agent._session_tracker.set_session_template_vars = Mock()

        mock_registry = Mock()
        mock_registry.get = Mock(return_value=mock_agent)

        mock_manager = AsyncMock()
        mock_manager._extract_user_id = Mock(return_value="test_user")
        mock_manager.create_sub_session = AsyncMock(return_value="sub_minlen")
        mock_manager.update_sub_agent_activity = AsyncMock()
        mock_manager.update_sub_session_metadata = AsyncMock()

        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(return_value={"context_vars": {}})
        mock_session_service = Mock()
        mock_session_service.session_manager = mock_session_manager
        mock_session_service.save_session = AsyncMock()

        server._extract_registry = Mock(return_value=mock_registry)
        server._extract_session_service = Mock(return_value=mock_session_service)
        server._get_manager = Mock(return_value=mock_manager)

        return mock_agent

    def test_config_loaded(self, min_len_server):
        """Config values are read correctly."""
        assert min_len_server._min_result_length_by_agent == {"test_writer": 100}
        assert min_len_server._min_result_retries == 2

    def test_config_defaults_when_missing(self, server):
        """Defaults to empty dict and 2 retries when not configured."""
        assert server._min_result_length_by_agent == {}
        assert server._min_result_retries == 2

    @pytest.mark.asyncio
    async def test_short_result_triggers_retry_success(self, min_len_server):
        """Short result auto-retries; uses longer result from retry."""
        call_count = 0

        async def mock_run_events(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield {"type": "final", "summary": "Hallo Welt"}
                yield {"type": "end"}
            else:
                yield {"type": "final", "summary": "A" * 200}
                yield {"type": "end"}

        self._make_blocking_mocks(min_len_server, mock_run_events)

        result = await min_len_server._handle_create({
            "agent_type": "test_writer",
            "task": "Write synopsis",
            "blocking": True,
            "_session_id": "parent_session",
            "_request_id": "req_001",
        })

        assert result["status"] == "completed"
        assert len(result["result"]) >= 100
        assert call_count == 2  # Initial + 1 retry

    @pytest.mark.asyncio
    async def test_normal_length_no_retry(self, min_len_server):
        """Result above threshold does not trigger retry."""
        call_count = 0

        async def mock_run_events(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            yield {"type": "final", "summary": "B" * 500}
            yield {"type": "end"}

        self._make_blocking_mocks(min_len_server, mock_run_events)

        result = await min_len_server._handle_create({
            "agent_type": "test_writer",
            "task": "Write synopsis",
            "blocking": True,
            "_session_id": "parent_session",
            "_request_id": "req_002",
        })

        assert result["status"] == "completed"
        assert len(result["result"]) == 500
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_unconfigured_agent_no_retry(self, min_len_server):
        """Agent types not in config never trigger retry."""
        call_count = 0

        async def mock_run_events(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            yield {"type": "final", "summary": "short"}
            yield {"type": "end"}

        self._make_blocking_mocks(min_len_server, mock_run_events)

        result = await min_len_server._handle_create({
            "agent_type": "other_agent",
            "task": "Do something",
            "blocking": True,
            "_session_id": "parent_session",
            "_request_id": "req_003",
        })

        assert result["status"] == "completed"
        assert result["result"] == "short"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted_uses_last_result(self, min_len_server):
        """When all retries produce short results, uses the last one."""
        async def mock_run_events(*args, **kwargs):
            yield {"type": "final", "summary": "still short"}
            yield {"type": "end"}

        self._make_blocking_mocks(min_len_server, mock_run_events)

        result = await min_len_server._handle_create({
            "agent_type": "test_writer",
            "task": "Write synopsis",
            "blocking": True,
            "_session_id": "parent_session",
            "_request_id": "req_004",
        })

        assert result["status"] == "completed"
        assert result["result"] == "still short"

    @pytest.mark.asyncio
    async def test_error_result_not_retried(self, min_len_server):
        """Error results are never retried even if short."""
        call_count = 0

        async def mock_run_events(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            yield {"type": "error", "message": "LLM failed"}

        self._make_blocking_mocks(min_len_server, mock_run_events)

        result = await min_len_server._handle_create({
            "agent_type": "test_writer",
            "task": "Write synopsis",
            "blocking": True,
            "_session_id": "parent_session",
            "_request_id": "req_005",
        })

        assert result["status"] == "completed"  # lifecycle: the run is over
        assert result["outcome"] == "error"     # verdict: how it ended
        assert result["result"].startswith("Error:")
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_cancelled_result_not_retried(self, min_len_server):
        """Cancelled results are never retried."""
        call_count = 0

        async def mock_run_events(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            yield {"type": "cancelled", "reason": "User cancelled"}

        self._make_blocking_mocks(min_len_server, mock_run_events)

        result = await min_len_server._handle_create({
            "agent_type": "test_writer",
            "task": "Write synopsis",
            "blocking": True,
            "_session_id": "parent_session",
            "_request_id": "req_006",
        })

        assert result["status"] == "completed"  # lifecycle: the run is over
        assert result["outcome"] == "cancelled"  # verdict: how it ended
        assert result["result"].startswith("Cancelled:")
        assert call_count == 1


class TestCreateParamValidation:
    """Missing required params on 'create' must yield an actionable error for the
    agent, not a raw KeyError (weak LLMs otherwise blindly retry the bad call)."""

    @pytest.mark.asyncio
    async def test_missing_agent_type_returns_actionable_error(self, server):
        result = await server._handle_create({"task": "Write a title"})
        assert result["status"] == "error"
        assert result["error_type"] == "missing_parameter"
        assert "agent_type" in result["error"]
        # No opaque KeyError repr leaking to the agent.
        assert result["error"] != "'agent_type'"

    @pytest.mark.asyncio
    async def test_missing_task_returns_actionable_error(self, server):
        result = await server._handle_create({"agent_type": "some_agent"})
        assert result["status"] == "error"
        assert result["error_type"] == "missing_parameter"
        assert "task" in result["error"]

    @pytest.mark.asyncio
    async def test_both_missing_lists_both(self, server):
        result = await server._handle_create({})
        assert result["status"] == "error"
        assert "agent_type" in result["error"]
        assert "task" in result["error"]

    @pytest.mark.asyncio
    async def test_blank_agent_type_treated_as_missing(self, server):
        result = await server._handle_create({"agent_type": "   ", "task": "x"})
        assert result["status"] == "error"
        assert result["error_type"] == "missing_parameter"
        assert "agent_type" in result["error"]

    @pytest.mark.asyncio
    async def test_restricted_manager_lists_allowed_agents(self, restricted_server):
        """When the manager restricts agents, the hint names the valid choices."""
        result = await restricted_server._handle_create({"task": "x"})
        assert result["status"] == "error"
        assert "coding_agent" in result["error"]
        assert "testing_agent" in result["error"]

    @pytest.mark.asyncio
    async def test_wildcard_manager_gives_generic_hint(self, server):
        """Wildcard managers cannot enumerate agents — no bare '*' in the hint."""
        result = await server._handle_create({"task": "x"})
        assert "one of: *" not in result["error"]
        assert "registered agent" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatch_create_missing_param_no_keyerror(self, server):
        """Through the public manage_sub_agent entrypoint (the real path)."""
        result = await server.manage_sub_agent({"operation": "create", "task": "x"})
        assert result["status"] == "error"
        assert result["error_type"] == "missing_parameter"
        assert "agent_type" in result["error"]


class TestAllowAdvancedModelGate:
    """Kosten-Riegel allow_advanced_model: LLM-Caller setzen
    use_advanced_model gern aus Eigeninitiative (Prod-Befund 2026-07-20:
    der v6-Coordinator spawnte JEDES Panel mit use_advanced_model=true,
    ohne dass sein Prompt es verlangt - kompletter Moderator-Run auf der
    teuren advanced-Kette). Die Instanz-Config muss das hart unterdruecken
    koennen; Default True = Bestandsverhalten."""

    def _gate(self, allow, params):
        from types import SimpleNamespace
        stub = SimpleNamespace(allow_advanced_model=allow, name="test_sam")
        return SubAgentManagerServer._effective_use_advanced(stub, params)

    def test_default_passthrough(self):
        assert self._gate(True, {"use_advanced_model": True}) is True
        assert self._gate(True, {"use_advanced_model": False}) is False
        assert self._gate(True, {}) is False

    def test_disabled_suppresses_llm_request(self):
        assert self._gate(False, {"use_advanced_model": True}) is False
        assert self._gate(False, {"use_advanced_model": False}) is False
        assert self._gate(False, {}) is False

    def test_server_default_is_true(self, server):
        # Bestehende Instanzen ohne Config-Eintrag verhalten sich unveraendert.
        assert server.allow_advanced_model is True


class TestAdvancedCreateOnlyAgents:
    """advanced_create_only_agents: for listed agent types the caller's
    use_advanced_model counts on `create` only. Built for the v6 idea writers,
    whose advanced chain is the premium model - the moderator's prompt asks
    for advanced continues (synthesis, stuck), and each would be a premium
    call over a 100k+ context. Unlisted types keep today's behaviour."""

    def _gate(self, listed, agent_type, requested):
        from types import SimpleNamespace
        stub = SimpleNamespace(advanced_create_only_agents=set(listed), name="test_sam")
        return SubAgentManagerServer._continue_use_advanced(stub, agent_type, requested)

    def test_gate_suppresses_only_listed_types(self):
        assert self._gate({"gated_agent"}, "gated_agent", True) is False
        assert self._gate({"gated_agent"}, "other_agent", True) is True
        assert self._gate({"gated_agent"}, "gated_agent", False) is False
        assert self._gate(set(), "gated_agent", True) is True

    def test_server_reads_config_and_defaults_to_empty(self, server, mock_config):
        # Instances without the key behave as before.
        assert server.advanced_create_only_agents == set()
        cfg = Mock(spec=ToolServerConfig)
        cfg.max_sub_agents_per_session = 10
        cfg.max_nesting_depth = 5
        cfg.max_sub_agents_per_type = 3
        cfg.allowed_agents = ["*"]
        cfg.blocked_agents = []
        cfg.advanced_create_only_agents = ["gated_agent", "other_gated"]
        gated = SubAgentManagerServer(name="sam", system_config=mock_config, server_config=cfg)
        assert gated.advanced_create_only_agents == {"gated_agent", "other_gated"}

    async def _run_continue(self, server, agent_type, requested):
        """Drive a real continue through the public entrypoint; only the
        boundaries (registry, session store, manager) are mocked. Returns the
        use_advanced_model that reached the agent's run_events.

        Same wiring as TestLLMProfileSelection in
        test_plugin_sub_agent_manager_llm_profiles.py - notably session_service
        is an AsyncMock for save_session: a plain Mock makes the handler raise
        inside its own except block, and then the run looks green while only
        the first half of the path ever executed."""
        captured = {}

        async def run_events(*args, **kwargs):
            captured.update(kwargs)
            yield {"type": "final", "summary": "continued"}

        agent = Mock()
        agent.agent_config.default_llm_profile = "normal"
        agent._session_tracker = Mock()
        agent.run_events = run_events
        registry = Mock()
        registry.get = Mock(return_value=agent)

        session_service = Mock()
        session_service.save_session = AsyncMock()
        session_service.session_manager = Mock()
        session_service.session_manager.load_session = AsyncMock(return_value={
            "agent_name": agent_type,
            "parent_session": {"session_id": "parent1"},
        })

        manager = Mock()
        manager._extract_user_id = Mock(return_value="u1")
        manager._session_service = session_service
        manager.update_sub_session_metadata = AsyncMock()
        manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, manager)
        manager._write_sub_agent = manager.update_sub_session_metadata
        manager.refresh_sub_context_vars = AsyncMock(return_value={})
        manager.update_sub_agent_activity = AsyncMock()

        server._extract_registry = Mock(return_value=registry)
        server._extract_session_service = Mock(return_value=session_service)
        server._get_manager = Mock(return_value=manager)

        result = await server.manage_sub_agent({
            "operation": "continue",
            "instance_id": "sub_1", "message": "go on",
            "use_advanced_model": requested,
            "_session_id": "parent1", "_agent": Mock(), "_request_id": "req1",
        })
        # Without this the test would pass on a handler that died early: the
        # except branch returns before run_events and captured stays empty.
        assert result["status"] == "completed", "handler did not finish: %r" % (result,)
        assert "use_advanced_model" in captured, "run_events never reached - fixture broken"
        return captured["use_advanced_model"]

    @pytest.mark.asyncio
    async def test_continue_path_suppresses_for_listed_type(self, server):
        server.advanced_create_only_agents = {"gated_agent"}
        assert await self._run_continue(server, "gated_agent", True) is False

    @pytest.mark.asyncio
    async def test_continue_path_keeps_flag_for_unlisted_type(self, server):
        server.advanced_create_only_agents = {"gated_agent"}
        assert await self._run_continue(server, "other_agent", True) is True

    @pytest.mark.asyncio
    async def test_hot_reload_applies_the_list(self, server, mock_config):
        """The whole point of the key is to be tunable without a restart:
        editing the list and running `agent-cli reload` has to change the
        gate, and the reload has to report it as a change."""
        assert await self._run_continue(server, "gated_agent", True) is True

        cfg = Mock(spec=ToolServerConfig)
        cfg.max_sub_agents_per_session = 10
        cfg.max_nesting_depth = 5
        cfg.max_sub_agents_per_type = 3
        cfg.allowed_agents = ["*"]
        cfg.blocked_agents = []
        cfg.advanced_create_only_agents = ["gated_agent"]
        changes = server.reload_config(cfg)

        assert "advanced_create_only_agents" in changes, \
            "reload did not report the change: %r" % (sorted(changes),)
        assert server.advanced_create_only_agents == {"gated_agent"}
        assert await self._run_continue(server, "gated_agent", True) is False


class SlowAgent:
    """Agent.run_events for the cancel tests: a run lasts until the test releases it or its request is cancelled,
    and a cancelled run ends as the real loop ends it, with its "cancelled" event. cancel_request answers False
    for a request that is not running (any more) -- the real one also answers True while a child request of it
    runs, which these tests never have."""

    def __init__(self, answer="done"):
        self.agent_config = Mock(default_llm_profile="normal")
        self._session_tracker = Mock()
        self.answer = answer
        self.deaf = False  # past its last check: a cancel no longer ends the run
        self.finishing = None  # an Event: the next run waits on it between finalizing its request and "end"
        self.live = {}  # request_id -> set by a cancel
        self.requests = []
        self.stopped = []  # the requests a cancel reached
        self.releases = []  # one per run: set it to end that run with its answer

    async def cancel_request(self, request_id):
        if request_id not in self.live:
            return False
        self.live[request_id].set()
        self.stopped.append(request_id)
        return True

    async def run_events(self, task, request_id, session_id, **kwargs):
        import asyncio
        cancelled = self.live[request_id] = asyncio.Event()
        release = asyncio.Event()
        self.releases.append(release)
        self.requests.append(request_id)
        try:
            yield {"type": "start", "request_id": request_id}  # registered, before its first model call
            if self.deaf:
                yield {"type": "final", "summary": self.answer}
                await release.wait()
            else:
                waits = [asyncio.ensure_future(cancelled.wait()), asyncio.ensure_future(release.wait())]
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                for one in waits:
                    one.cancel()
                if cancelled.is_set():
                    yield {"type": "cancelled", "reason": "Request was cancelled"}
                    return
                yield {"type": "final", "summary": self.answer}
            del self.live[request_id]  # the real loop finalizes its request before it says "end"
            gate, self.finishing = self.finishing, None
            if gate:  # the real finalize still saves and runs hooks here
                await gate.wait()
            yield {"type": "end"}
        finally:
            self.live.pop(request_id, None)

    async def started(self, count):
        """Until the count-th run has begun."""
        import asyncio

        async def runs():
            while len(self.requests) < count:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(runs(), 5)


class TestAJobAnswersOnlyItsParent:
    """A background job is keyed by a guessable id: a caller without a session is nobody's parent."""

    @pytest.fixture
    async def job(self, server):
        import asyncio
        task = asyncio.ensure_future(asyncio.sleep(30))
        server.default_wait_timeout = 1  # a wait that did reach the job fails fast instead of waiting it out
        server._async_jobs["sub_other"] = {"instance_id": "sub_other", "status": "running", "result": None,
                                           "parent_session_id": "someone_else", "task_handle": task}
        yield task
        task.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["poll", "wait", "cancel"])
    @pytest.mark.parametrize("session", [None, "intruder"])
    async def test_it_is_not_found_by_anyone_else(self, server, job, operation, session):
        params = {"operation": operation, "instance_id": "sub_other"}
        if session:
            params["_session_id"] = session
        assert await server.manage_sub_agent(params) == {"status": "error", "error": "Instance not found"}
        assert job.cancelling() == 0 and server._async_jobs["sub_other"]["status"] == "running"


class TestCancellingAJobStopsWhatItStarted:
    """A background job's tool calls and sub-agents are tasks and requests of their own: cancelling the job's task
    alone would leave them running with nobody waiting for them."""

    @pytest.mark.asyncio
    async def test_the_requests_below_the_job_are_cancelled_too(self, server, monkeypatch):
        import agent_system.core.cancellation as cancellation
        tokens = Mock()
        monkeypatch.setattr(cancellation, "get_cancellation_manager", lambda: tokens)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        started = await server.manage_sub_agent({
            "operation": "create", "agent_type": "slow_agent", "task": "work", "blocking": False,
            "_session_id": "parent1", "_agent": Mock(), "_request_id": "req1"})
        assert started["status"] == "running", started
        await agent.started(1)

        answer = await server.manage_sub_agent({"operation": "cancel", "instance_id": "sub_slow", "_session_id": "parent1"})
        assert answer["status"] == "cancelled", answer
        # CancellationManager.cancel_request cancels the request and every one below it (id prefix)
        tokens.cancel_request.assert_called_once_with(agent.requests[0])


class TestWaitingOnAJobThatEnds:
    """A wait answers with how the job ended -- also when the job left memory meanwhile."""

    COMMON = {"instance_id": "sub_slow", "_session_id": "parent1", "_agent": Mock(), "_request_id": "req1"}

    @classmethod
    async def call(cls, server, operation, **params):
        """One operation, given 5 s: a manager that hangs fails the test instead of the suite."""
        import asyncio
        return await asyncio.wait_for(server.manage_sub_agent({**cls.COMMON, "operation": operation, **params}), 5)

    @staticmethod
    def stored(server, status):
        """What the parent session has stored: the job ran and ended with this status."""
        record = {"instance_id": "sub_slow", "agent_type": "slow_agent", "status": status,
                  "created_at": "2026-09-17T06:00:00Z", "last_used": "2026-09-17T06:01:00Z"}
        server._get_manager().list_sub_sessions = AsyncMock(
            side_effect=lambda parent, include_completed=False, creator_plugin=None: [record] if include_completed else [])

    @pytest.mark.asyncio
    async def test_a_job_cancelled_during_the_wait_is_reported_cancelled(self, server):
        import asyncio
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.call(server, "create", agent_type="slow_agent", task="work", blocking=False)
        await agent.started(1)
        waiting = asyncio.ensure_future(self.call(server, "wait"))
        await asyncio.sleep(0.05)
        self.stored(server, "cancelled")

        assert (await self.call(server, "cancel"))["status"] == "cancelled"
        # the wait sees the ending, from memory or from what is stored -- that the entry goes only
        # after the ending is stored is measured in test_a_job_the_caller_cancelled_is_not_kept_for_a_poll
        assert (await waiting)["status"] == "cancelled"
        assert "sub_slow" not in server._async_jobs

    @pytest.mark.asyncio
    async def test_a_cancelled_job_keeps_its_ending_but_not_its_task(self, server):
        """Ended by CancelledError, the task holds every frame of the run, so the job lets go of it
        -- and keeps the ending itself until a reader has had it. Cancelled from outside, that is:
        the caller did not call it off, and may be asleep over it."""
        import asyncio
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.call(server, "create", agent_type="slow_agent", task="work", blocking=False)
        await agent.started(1)
        handle = server._async_jobs["sub_slow"]["task_handle"]

        handle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle, 5)

        assert server._async_jobs["sub_slow"]["task_handle"] is None
        assert (await self.call(server, "poll"))["status"] == "cancelled"
        assert "sub_slow" not in server._async_jobs, "a poll takes a finished job out of memory"

    @pytest.mark.asyncio
    async def test_a_job_the_caller_cancelled_is_not_kept_for_a_poll(self, server):
        """The caller called it off, awake: it is not coming back to read the ending, so an entry
        held for that read stayed for the life of the process. It goes once the ending is stored,
        and a poll that comes anyway reads it there."""
        import asyncio
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.call(server, "create", agent_type="slow_agent", task="work", blocking=False)
        await agent.started(1)
        handle = server._async_jobs["sub_slow"]["task_handle"]
        held_while_stored = []
        server._get_manager().update_sub_session_metadata = AsyncMock(
            side_effect=lambda **fields: held_while_stored.append("sub_slow" in server._async_jobs) or True)

        assert (await self.call(server, "cancel"))["status"] == "cancelled"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle, 5)

        assert "sub_slow" not in server._async_jobs
        # gone before its ending was stored, a wait meanwhile read a status that still said active
        assert held_while_stored and all(held_while_stored), held_while_stored
        self.stored(server, "cancelled")
        assert (await self.call(server, "poll"))["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_a_called_off_job_whose_ending_was_not_stored_stays(self, server):
        """Nothing written, nothing on disk says it ended: the entry is the only answer a poll
        or wait still has, so it is not dropped."""
        import asyncio
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.call(server, "create", agent_type="slow_agent", task="work", blocking=False)
        await agent.started(1)
        handle = server._async_jobs["sub_slow"]["task_handle"]
        server._get_manager().update_sub_session_metadata = AsyncMock(return_value=False)

        assert (await self.call(server, "cancel"))["status"] == "cancelled"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle, 5)

        assert server._async_jobs["sub_slow"]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_the_callers_mark_is_not_handed_to_the_model(self, server):
        """A poll that finds the job before its task has unwound hands over the job as it stands --
        without the bookkeeping that tells `_finish_job` the caller called it off."""
        server._async_jobs["sub_slow"] = {"instance_id": "sub_slow", "status": "cancelled",
                                          "parent_session_id": "parent1", "task_handle": None,
                                          "_ended_by_caller": True}

        answer = await self.call(server, "poll")

        assert answer["status"] == "cancelled" and "_ended_by_caller" not in answer, answer

    @pytest.mark.asyncio
    async def test_a_job_that_leaves_memory_during_the_wait_neither_hangs_nor_vanishes(self, server):
        """Another poll takes the finished job away: the wait asks the stored state -- a call that takes the job
        lock, which the wait held around it (every job of the manager hung)."""
        import asyncio
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        self.stored(server, "cancelled")
        server._async_jobs["sub_slow"] = {"instance_id": "sub_slow", "status": "running",
                                          "parent_session_id": "parent1", "task_handle": None}
        waiting = asyncio.ensure_future(self.call(server, "wait"))
        await asyncio.sleep(0.05)
        server._async_jobs["sub_slow"]["status"] = "cancelled"
        await self.call(server, "poll")

        assert (await waiting)["status"] == "cancelled"
        assert (await self.call(server, "poll"))["status"] == "cancelled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["cancelled", "failed"])
    async def test_a_stopped_job_known_only_from_the_stored_state_is_not_called_vanished(self, server, status):
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        self.stored(server, status)
        assert (await self.call(server, "wait"))["status"] == status

    @pytest.mark.asyncio
    async def test_wait_all_counts_a_cancelled_job_as_not_completed(self, server):
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        for instance, status in (("sub_done", "completed"), ("sub_stopped", "cancelled")):
            server._async_jobs[instance] = {"instance_id": instance, "status": status, "parent_session_id": "parent1"}
        result = await self.call(server, "wait_all", instance_ids=["sub_done", "sub_stopped"])
        assert (result["completed"], result["failed"]) == (1, 1), result


class TestCancelReachesABlockingRun:
    """`list` shows a blocking create/continue as running: `cancel` has to stop it too, not answer "not found"."""

    @staticmethod
    def wire(server, agent):
        """Registry, session store and manager around the agent; the sub-agent belongs to session parent1."""
        session_service = Mock()
        session_service.save_session = AsyncMock()
        session_service.session_manager = Mock()

        async def load_session(user_id, session_id, *args, **kwargs):
            if session_id == "parent1":  # the caller, a session of its own
                return {"agent_name": "coordinator", "context_vars": {}}
            return {"agent_name": "slow_agent", "parent_session": {"session_id": "parent1"}, "context_vars": {}}
        session_service.session_manager.load_session = AsyncMock(side_effect=load_session)
        manager = Mock()
        manager._extract_user_id = Mock(return_value="u1")
        manager._session_service = session_service
        manager.create_sub_session = AsyncMock(return_value="sub_slow")
        manager.update_sub_session_metadata = AsyncMock()
        manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, manager)
        manager._write_sub_agent = manager.update_sub_session_metadata
        manager.refresh_sub_context_vars = AsyncMock(return_value={})
        manager.update_sub_agent_activity = AsyncMock()
        server._extract_registry = Mock(return_value=Mock(get=Mock(return_value=agent)))
        server._extract_session_service = Mock(return_value=session_service)
        server._get_manager = Mock(return_value=manager)
        return session_service

    @staticmethod
    def start(server, operation, **extra):
        import asyncio
        params = {"operation": operation, "_session_id": "parent1", "_agent": Mock(), "_request_id": "req1", **extra}
        params.update({"agent_type": "slow_agent", "task": "work"} if operation == "create"
                      else {"instance_id": "sub_slow", "message": "work on"})
        return asyncio.ensure_future(server.manage_sub_agent(params))

    @staticmethod
    async def finish(run):
        import asyncio
        return await asyncio.wait_for(run, 5)  # a run that never ends fails here, it does not hang the suite

    @staticmethod
    async def cancel(server, session="parent1"):
        return await server.manage_sub_agent({"operation": "cancel", "instance_id": "sub_slow", "_session_id": session})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["create", "continue"])
    async def test_a_cancel_stops_the_run_and_its_caller_learns_it(self, server, operation):
        agent = SlowAgent()
        session_service = self.wire(server, agent)
        run = self.start(server, operation)
        await agent.started(1)

        assert (await self.cancel(server))["status"] == "cancelling"
        result = await self.finish(run)
        assert (result["status"], result["outcome"]) == ("completed", "cancelled"), result
        assert result["result"].startswith("Cancelled:")
        session_service.save_session.assert_awaited_once()  # the run ended as any run ends
        # over: nothing left to stop
        after = await self.cancel(server)
        assert after["status"] == "error" and "not found" in after["error"], after

    @pytest.mark.asyncio
    async def test_another_session_cannot_stop_it(self, server):
        agent = SlowAgent()
        self.wire(server, agent)
        run = self.start(server, "create")
        await agent.started(1)

        assert await self.cancel(server, session="intruder") == {"status": "error", "error": "Instance not found"}
        agent.releases[0].set()
        result = await self.finish(run)
        assert (result["outcome"], result["result"]) == ("completed", "done"), result

    @pytest.mark.asyncio
    async def test_a_caller_without_a_session_cannot_stop_it(self, server):
        """POST /chat/command may come without a session: such a caller owns no sub-agent."""
        agent = SlowAgent()
        self.wire(server, agent)
        run = self.start(server, "create")
        await agent.started(1)

        assert await self.cancel(server, session=None) == {"status": "error", "error": "Instance not found"}
        agent.releases[0].set()
        assert (await self.finish(run))["outcome"] == "completed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["create", "continue"])
    async def test_a_cancel_while_the_run_is_prepared_stops_it_at_its_start(self, server, operation):
        """Marked running, the run still reads and writes its session first: `list` already shows it. It begins
        anyway -- loading its session as any run does -- and stops at its first event, before a model call."""
        import asyncio
        agent = SlowAgent()
        session_service = self.wire(server, agent)
        preparing, prepared = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            preparing.set()
            await prepared.wait()
            return {"agent_name": "slow_agent", "parent_session": {"session_id": "parent1"}, "context_vars": {}}
        if operation == "create":
            session_service.session_manager.load_session = slow
        else:
            server._get_manager().refresh_sub_context_vars = slow
        run = self.start(server, operation)
        await asyncio.wait_for(preparing.wait(), 5)

        assert server.is_agent_running("sub_slow")
        assert (await self.cancel(server))["status"] == "cancelling"
        prepared.set()
        result = await self.finish(run)
        assert result["outcome"] == "cancelled", result
        assert agent.stopped == agent.requests, "the run itself passes the cancel on to its request"

    @pytest.mark.asyncio
    async def test_a_finished_async_job_of_the_instance_does_not_hide_the_run(self, server):
        """A background job stays in memory until polled. A continue of the same instance drops it as it begins
        -- a cancel that comes before that still has to reach the continue, not the finished job."""
        import asyncio
        server._async_jobs["sub_slow"] = {"instance_id": "sub_slow", "status": "completed",
                                          "parent_session_id": "parent1", "_awaiting_poll": True}
        agent = SlowAgent()
        self.wire(server, agent)
        async with server._async_jobs_lock:  # the continue is marked running and waits to drop the finished job
            run = self.start(server, "continue")
            await asyncio.wait_for(self.until(lambda: "sub_slow" in server._blocking_runs), 5)
            assert (await asyncio.wait_for(self.cancel(server), 5))["status"] == "cancelling"
        assert (await self.finish(run))["outcome"] == "cancelled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ended", ["completed", "failed", "cancelled"])
    async def test_a_continue_replaces_the_ending_of_an_earlier_background_run(self, server, ended):
        server._async_jobs["sub_slow"] = {"instance_id": "sub_slow", "status": ended, "task_handle": None,
                                          "parent_session_id": "parent1", "_awaiting_poll": True}
        agent = SlowAgent()
        self.wire(server, agent)
        run = self.start(server, "continue")
        await agent.started(1)
        agent.releases[0].set()
        assert (await self.finish(run))["outcome"] == "completed"
        assert "sub_slow" not in server._async_jobs, "a later poll or wait would report the old ending"

    @pytest.mark.asyncio
    async def test_a_continue_reopens_without_what_an_earlier_run_left(self, server):
        """A run that never recorded its end -- cancelled or crashed before activities were
        cleared at every ending -- left its activity. Reopened with it, the instance reads as a
        crash to every other process until this run takes its lock, and a `list` there writes
        "interrupted" over a run that is just starting."""
        agent = SlowAgent()
        self.wire(server, agent)
        run = self.start(server, "continue")
        await agent.started(1)

        # while the run is under way: its end would write last_used over the leftover anyway
        written = [call.kwargs for call in server._get_manager().update_sub_session_metadata.await_args_list]
        agent.releases[0].set()
        await self.finish(run)

        assert any(fields.get("status") == "active" and "current_activity" in fields
                   and fields["current_activity"] is None for fields in written), written

    @pytest.mark.asyncio
    async def test_a_continue_refused_as_already_running_touches_nothing(self, server):
        """Reopening clears the activity. Before the check that refuses it, a continue that collides
        with a run wiped that run's activity -- the panel read it idle while it worked."""
        agent = SlowAgent()
        self.wire(server, agent)
        first = self.start(server, "continue")
        await agent.started(1)
        writes = server._get_manager().update_sub_session_metadata
        before = writes.await_count

        refused = await self.finish(self.start(server, "continue"))

        assert "already running" in str(refused.get("error", "")), refused
        assert writes.await_count == before, writes.await_args_list[before:]
        agent.releases[0].set()
        await self.finish(first)

    @pytest.mark.asyncio
    async def test_a_continue_leaves_a_background_run_that_has_not_ended_alone(self, server):
        """Only a started background run is marked running; one whose task has not begun yet is not."""
        server._async_jobs["sub_slow"] = {"instance_id": "sub_slow", "status": "running", "task_handle": None,
                                          "parent_session_id": "parent1"}
        agent = SlowAgent()
        self.wire(server, agent)
        run = self.start(server, "continue")
        await agent.started(1)
        agent.releases[0].set()
        await self.finish(run)
        assert server._async_jobs["sub_slow"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_a_run_that_is_already_ending_is_not_called_cancelled(self, server):
        """Its request is over, the answer stands: the session is being saved."""
        import asyncio
        agent = SlowAgent()
        session_service = self.wire(server, agent)
        saving, saved = asyncio.Event(), asyncio.Event()

        async def save_session(**kwargs):
            saving.set()
            await saved.wait()
        session_service.save_session = save_session
        run = self.start(server, "create")
        await agent.started(1)
        agent.releases[0].set()
        await asyncio.wait_for(saving.wait(), 5)

        answer = await self.cancel(server)
        assert answer["status"] == "error" and "already ending" in answer["error"], answer
        saved.set()
        assert (await self.finish(run))["result"] == "done"

    @pytest.fixture
    def retrying_server(self, mock_config):
        config = Mock(spec=ToolServerConfig)
        config.max_sub_agents_per_session = 10
        config.max_nesting_depth = 5
        config.max_sub_agents_per_type = 3
        config.allowed_agents = ["*"]
        config.blocked_agents = []
        config.min_result_length_by_agent = {"slow_agent": 100}  # "done" is too short: a retry follows
        config.min_result_retries = 2
        return SubAgentManagerServer(name="sub_agent_manager", system_config=mock_config, server_config=config)

    @pytest.mark.asyncio
    async def test_a_cancel_reaches_the_retry_that_is_running(self, retrying_server):
        agent = SlowAgent()
        self.wire(retrying_server, agent)
        run = self.start(retrying_server, "create")
        await agent.started(1)
        agent.releases[0].set()  # a short answer: the first retry starts
        await agent.started(2)

        assert (await self.cancel(retrying_server))["status"] == "cancelling"
        result = await self.finish(run)
        assert result["outcome"] == "cancelled", result
        assert len(agent.requests) == 2, "a cancelled retry must not be followed by another"

    @pytest.mark.asyncio
    async def test_a_cancel_after_the_last_check_still_stops_the_retries(self, retrying_server):
        agent = SlowAgent()
        agent.deaf = True  # the answer is out, the run no longer reacts: only the retries can still be stopped
        self.wire(retrying_server, agent)
        run = self.start(retrying_server, "create")
        await agent.started(1)

        assert (await self.cancel(retrying_server))["status"] == "cancelling"
        agent.releases[0].set()
        result = await self.finish(run)
        assert result["result"] == "done" and len(agent.requests) == 1, (result, agent.requests)

    @pytest.mark.asyncio
    async def test_a_parent_cancel_during_a_retry_ends_the_retries(self, retrying_server):
        """The parent's cancel reaches the retry by its request id prefix, not through `cancel`."""
        agent = SlowAgent()
        self.wire(retrying_server, agent)
        run = self.start(retrying_server, "create")
        await agent.started(1)
        agent.releases[0].set()
        await agent.started(2)

        assert await agent.cancel_request(agent.requests[1])
        result = await self.finish(run)
        assert result["outcome"] == "cancelled", result
        assert len(agent.requests) == 2, "a cancelled retry must not be followed by another"

    @pytest.mark.asyncio
    async def test_a_cancel_that_comes_too_late_changes_nothing(self, retrying_server):
        """Its request is already finalized: the answer says so, and the retries still follow."""
        import asyncio
        agent = SlowAgent()
        finishing = agent.finishing = asyncio.Event()
        self.wire(retrying_server, agent)
        run = self.start(retrying_server, "create")
        await agent.started(1)
        agent.releases[0].set()
        await asyncio.wait_for(self.until(lambda: not agent.live), 5)

        answer = await self.cancel(retrying_server)
        assert answer["status"] == "error" and "already ending" in answer["error"], answer
        finishing.set()
        for count in (2, 3):  # both retries run, and both are short again
            await agent.started(count)
            agent.releases[count - 1].set()
        result = await self.finish(run)
        assert (result["outcome"], len(agent.requests)) == ("completed", 3), (result, agent.requests)

    @pytest.mark.asyncio
    async def test_a_cancel_of_the_caller_ends_the_retries(self, retrying_server):
        """The caller's cancel reaches this tool call's token -- not a retry, which has a request id of its own."""
        from agent_system.core.cancellation import CancellationToken
        agent = SlowAgent()
        agent.deaf = True  # the short answer is out before the cancel
        self.wire(retrying_server, agent)
        token = CancellationToken("req1_001_000")
        run = self.start(retrying_server, "create", _cancellation_token=token)
        await agent.started(1)

        token.cancel()
        agent.releases[0].set()
        result = await self.finish(run)
        assert result["result"] == "done" and len(agent.requests) == 1, (result, agent.requests)

    @pytest.mark.asyncio
    async def test_a_late_second_cancel_keeps_what_the_first_asked_for(self, retrying_server):
        """The first cancel came when the answer was already on its way: the retries stay off."""
        import asyncio
        agent = SlowAgent()
        agent.deaf = True
        finishing = agent.finishing = asyncio.Event()
        self.wire(retrying_server, agent)
        run = self.start(retrying_server, "create")
        await agent.started(1)
        assert (await self.cancel(retrying_server))["status"] == "cancelling"
        agent.releases[0].set()
        await asyncio.wait_for(self.until(lambda: not agent.live), 5)

        assert (await self.cancel(retrying_server))["status"] == "error"
        finishing.set()
        result = await self.finish(run)
        assert result["result"] == "done" and len(agent.requests) == 1, (result, agent.requests)

    @staticmethod
    async def until(condition):
        import asyncio
        while not condition():
            await asyncio.sleep(0.01)


class TestTheCallerIsWokenWhenItsJobIsDone:
    """A background job can wake the session that started it, so its caller may end its turn over it
    instead of polling. Waking itself is core (core/session_presence.py); what is tested here is who
    the manager tells about which session, and that the job's own ending never depends on it."""

    @staticmethod
    def presence(monkeypatch, state="woke_session", fails=None, watch=None, guards=None,
                 order=None):
        """The core's wake as the server reaches it.

        The seam is `wake_session`, not `notify`: the repeating, its bounds, and that notify()
        never runs on the caller's loop all belong to the core and are measured there
        (tests/session/test_session_wake_helper.py). What is left here is what the MANAGER
        decides -- which session, which user, when, and with which guard.

        Returns the list of (session, user) told; with `watch`, each entry carries what it saw at
        the moment the manager told anybody, `guards` collects the `still_needed` handed over,
        and `order` records "woken" among whatever else the test is ordering it against.
        """
        told = []

        async def wake(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
            if guards is not None:
                guards.append(still_needed)
            if order is not None:
                order.append("woken")
            told.append((session_id, user_id) if watch is None else (session_id, user_id, watch()))
            if fails is not None:
                # The real one swallows its own failures; this measures the manager's guard
                # around it, which is what keeps a finished job out of its caller's error path.
                raise fails
            return state

        monkeypatch.setattr(sam_server, "wake_session", wake)
        return told

    @staticmethod
    def arming(monkeypatch, blocked=""):
        """What the core answers when the manager asks, BEFORE the job runs, whether this session
        can be woken at all. Returns the list of (session, user) asked about."""
        asked = []

        def wake_blocked(system_config, session_id, user_id):
            asked.append((session_id, user_id))
            return blocked

        monkeypatch.setattr(sam_server, "wake_blocked", wake_blocked)
        return asked

    @staticmethod
    def start(server, **extra):
        """A background create of sub_slow, returned once the job is on its way."""
        return TestCancelReachesABlockingRun.finish(
            TestCancelReachesABlockingRun.start(server, "create", blocking=False, **extra))

    @staticmethod
    async def run_to_end(server, agent):
        """Let the running job answer, and wait for the job -- not just the run -- to be over."""
        import asyncio
        await agent.started(1)
        handle = server._async_jobs["sub_slow"]["task_handle"]
        agent.releases[0].set()
        await asyncio.wait_for(handle, 5)

    @pytest.mark.asyncio
    async def test_the_wake_names_the_run_that_asked_for_the_job(self, server, monkeypatch):
        """By the id of its create call. Left to the core, it reads the ringing task's current
        request -- the sub-agent's -- and a stopped sub-agent kept its caller from being woken."""
        named = []

        async def wake(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
            named.append(started_by)
            return "woke_session"

        monkeypatch.setattr(sam_server, "wake_session", wake)
        self.arming(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert named == ["req1"], named

    @pytest.mark.asyncio
    async def test_a_finished_job_wakes_the_session_that_started_it(self, server, monkeypatch):
        """And it is told only once the ending is recorded: a woken run reads the job, so a wake
        that goes out first sends it to a job that still says it is running."""
        told = self.presence(monkeypatch,
                             watch=lambda: dict(server._async_jobs.get("sub_slow", {})))
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert [(session, user) for session, user, _ in told] == [("parent1", "u1")], \
            "the parent session and its user, not the sub-agent's"
        at_the_wake = told[0][2]
        assert at_the_wake["status"] == "completed" and at_the_wake["result"] == "done", at_the_wake

    @pytest.mark.asyncio
    async def test_a_job_nobody_asked_to_be_woken_for_wakes_nobody(self, server, monkeypatch):
        """The default: the caller stays awake and polls, as every job did before."""
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server)
        await self.run_to_end(server, agent)

        assert told == []

    @pytest.mark.asyncio
    async def test_with_presence_off_nothing_is_woken_and_nothing_breaks(self, server, monkeypatch, caplog):
        """What the README promises for a host that switched session_presence off: the caller
        polls, exactly as every job did before. Found by a mutation that came back green --
        `presence is None` was the one branch of the wake nothing measured.

        The ONLY test in this class that drives the real `wake_session` -- every other one
        replaces it. Nothing is patched to get there: the fixture's config is a Mock, and
        `presence_for` answers None for anything that is not a real SessionPresenceConfig, so
        presence is off here by construction. That is the point: the manager hands the core a
        session it cannot wake, and what comes back must not touch the job's ending or leave the
        caller a warning about a setting the host chose itself.
        """
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        with caplog.at_level(logging.WARNING, logger=sam_server.logger.name):
            await self.start(server, wake_when_done=True)
            await self.run_to_end(server, agent)

        job = server._async_jobs["sub_slow"]
        assert job["status"] == "completed" and job["result"] == "done", \
            "the ending is the job's own, not the wake's"
        # Nothing is ATTEMPTED either: without the guard the wake runs into a presence that is
        # not there, and the caller reads a warning about a host setting it chose itself.
        assert [r.message for r in caplog.records if "wake" in r.message.lower()] == []

    @pytest.mark.asyncio
    async def test_a_job_that_fails_wakes_it_too(self, server, monkeypatch):
        """Whoever sleeps on a job must not sleep through its failure -- there is no second ending."""
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        session_service = TestCancelReachesABlockingRun.wire(server, agent)
        session_service.save_session = AsyncMock(side_effect=RuntimeError("the disk said no"))
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert told == [("parent1", "u1")]
        assert server._async_jobs["sub_slow"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_failed_job_holds_its_slot_until_its_ending_is_stored(self, server):
        """Let go first, a continue could start the instance again before the job's "failed" was
        written -- and that "failed" then landed on the new run."""
        agent = SlowAgent()
        session_service = TestCancelReachesABlockingRun.wire(server, agent)
        session_service.save_session = AsyncMock(side_effect=RuntimeError("the disk said no"))
        held = []

        async def store(**fields):
            held.append((fields.get("status"), "sub_slow" in server._running_agents))

        server._get_manager().update_sub_session_metadata = AsyncMock(side_effect=store)
        await self.start(server)
        await self.run_to_end(server, agent)

        assert ("failed", True) in held, held
        assert "sub_slow" not in server._running_agents

    @pytest.mark.asyncio
    async def test_a_job_whose_ending_was_cut_short_still_lets_go_of_its_slot(self, server):
        """A cancel arriving while the ending is being stored (here the failed one) cuts it
        short. The slot is let go of after that write, so it stayed taken: "already running" for
        the life of the process."""
        import asyncio
        agent = SlowAgent()
        session_service = TestCancelReachesABlockingRun.wire(server, agent)
        session_service.save_session = AsyncMock(side_effect=RuntimeError("the disk said no"))
        server._get_manager().update_sub_session_metadata = AsyncMock(side_effect=asyncio.CancelledError())
        await self.start(server)
        with pytest.raises(asyncio.CancelledError):
            await self.run_to_end(server, agent)

        assert "sub_slow" not in server._running_agents

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", ["completed", "failed"])
    async def test_a_new_run_keeps_the_slot_the_old_job_let_go_of(self, server, monkeypatch, ending):
        """The slot goes before the bell, and the bell can ring for minutes: the continue the woken
        caller makes holds the slot by then. The old job's own cleanup after the bell took it from
        under that run -- "already running" gone, and a second continue on the same transcript."""
        self.presence(monkeypatch)
        agent = SlowAgent()
        session_service = TestCancelReachesABlockingRun.wire(server, agent)
        if ending == "failed":
            session_service.save_session = AsyncMock(side_effect=RuntimeError("the disk said no"))

        async def a_continue_starts_meanwhile(instance_id, *args, **kwargs):
            async with server._running_lock:
                server._running_agents.add(instance_id)

        server._wake_parent = a_continue_starts_meanwhile
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert server._async_jobs["sub_slow"]["status"] == ending, "fixture: another ending"
        assert "sub_slow" in server._running_agents

    @pytest.mark.asyncio
    async def test_a_job_whose_entry_was_taken_before_it_ended_wakes_nobody(self, server, monkeypatch):
        """The entry is gone by the time the task unwinds: a poll, a wait, a `continue` or a
        delete took it, and every one of those is the caller awake and handling the ending
        itself. Ringing then is not free even once -- the marker a ring leaves behind turns into
        a whole woken run when the caller's turn ends. (Archived while it ran is the exception,
        and it has its own test: there nobody has read anything.)"""
        import asyncio
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await agent.started(1)
        handle = server._async_jobs.pop("sub_slow")["task_handle"]  # a reader takes the job with it
        agent.releases[0].set()
        await asyncio.wait_for(handle, 5)

        assert told == []

    @pytest.mark.asyncio
    async def test_a_job_the_caller_cancelled_itself_does_not_wake_it(self, server, monkeypatch):
        """Cancelling is the caller saying it is not waiting any more, and it said so awake, in a
        turn of its own. A ring for that job wakes an idle session for a whole turn about
        something it called off itself."""
        import asyncio
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await agent.started(1)
        handle = server._async_jobs["sub_slow"]["task_handle"]  # the cancelled job lets go of it
        await TestCancelReachesABlockingRun.cancel(server)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle, 5)

        assert told == []

    @pytest.mark.asyncio
    async def test_a_job_the_caller_deleted_while_it_ran_does_not_wake_it(self, server, monkeypatch):
        """`delete` is the caller dropping the sub-agent, awake -- for the bell the same as a
        cancel of its own. Archiving to make room reaches the same method behind the caller's
        back and still rings (below); only the tool's own delete says it was the caller."""
        import asyncio
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await agent.started(1)
        handle = server._async_jobs["sub_slow"]["task_handle"]
        deleted = await server.manage_sub_agent(
            {"operation": "delete", "instance_id": "sub_slow", "_session_id": "parent1", "_agent": Mock()})
        agent.releases[0].set()
        await asyncio.wait_for(handle, 5)

        assert deleted["status"] != "error", deleted
        assert told == []

    @pytest.mark.asyncio
    async def test_an_ending_recorded_before_a_cancel_from_elsewhere_still_wakes_it(self, server, monkeypatch):
        """The ending goes into memory first and is only then persisted and rung. A cancel from
        elsewhere in between -- the process going down during that write -- runs the ending a
        second time, over an entry that already says it is over. That is not the caller calling
        it off: the caller is asleep, and it is still owed its bell."""
        told = self.presence(monkeypatch)
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        server._async_jobs["sub_slow"] = {  # as the first, cut-short ending left it
            "instance_id": "sub_slow", "status": "completed", "result": "done", "_awaiting_poll": True,
            "parent_session_id": "parent1", "task_handle": None}

        await server._finish_job("sub_slow", {"_session_id": "parent1", "wake_when_done": True},
                                 "cancelled", drop_task=True, manager=server._get_manager(),
                                 stored={"status": "cancelled"})

        assert told == [("parent1", "u1")]

    @pytest.mark.asyncio
    async def test_a_job_cancelled_from_elsewhere_wakes_it(self, server, monkeypatch):
        """The guard is "the caller ended it", and nothing wider: a task cancelled without the
        tool being asked -- the request tree it hangs in going down, the process shutting down --
        leaves a caller asleep over a job that will never answer. That one still rings."""
        import asyncio
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await agent.started(1)
        server._async_jobs["sub_slow"]["task_handle"].cancel()  # nothing recorded the ending first
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(server._async_jobs["sub_slow"]["task_handle"], 5)

        assert told == [("parent1", "u1")]
        assert server._async_jobs["sub_slow"]["status"] == "cancelled"

    @pytest.mark.parametrize("failure", [
        OSError("the sessions directory is gone"),
        RuntimeError("something nobody expected"),
    ], ids=["disk", "anything"])
    @pytest.mark.asyncio
    async def test_a_wake_that_fails_does_not_cost_the_job_its_ending(
            self, server, monkeypatch, failure):
        """The run is over and recorded; a wake that cannot be delivered costs a poll, not the
        result. Whatever it fails with: narrowed to the disk error the obvious fake raises, the
        next one through lands in the JOB's error handler and records a finished run as failed."""
        told = self.presence(monkeypatch, fails=failure)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert told == [("parent1", "u1")]
        assert server._async_jobs["sub_slow"]["status"] == "completed"
        assert server._async_jobs["sub_slow"]["result"] == "done"

    @pytest.mark.asyncio
    async def test_a_job_that_never_got_started_wakes_it_too(self, server, monkeypatch):
        """The job broke before it had a manager, so there is nobody to read the session's user
        from -- and a caller that went to sleep over it would wait forever. The id it injected
        answers instead."""
        import asyncio
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        reached = []

        def registry_for(params):  # create still gets one, the job itself does not
            reached.append(1)
            if len(reached) == 1:
                return Mock(get=Mock(return_value=agent))
            raise RuntimeError("the registry is gone")

        server._extract_registry = registry_for
        await self.start(server, wake_when_done=True, _user_id="u1")
        await asyncio.wait_for(server._async_jobs["sub_slow"]["task_handle"], 5)

        assert told == [("parent1", "u1")]
        assert server._async_jobs["sub_slow"]["status"] == "failed"
        assert agent.requests == [], "the run never happened"

    @pytest.mark.asyncio
    async def test_the_job_is_marked_in_memory_before_the_stored_write(self, server, monkeypatch):
        """Both orders have a window, and this is the one chosen: the mark sits before any await,
        so it also happens while the task is being cancelled -- an await there can be cut, and a
        job left saying "running" answers "running" for good. The stored write follows.

        It matters beyond polling: the abort paths reach the ending with the instance already out
        of _running_agents, so a `continue` can start meanwhile. Marking behind the await would
        stamp the dead run's status onto the job that continue put there."""
        order = []
        told = self.presence(monkeypatch, order=order)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        manager = server._get_manager()
        at_write = []
        manager.update_sub_session_metadata = AsyncMock(
            side_effect=lambda **kw: (order.append("stored"),
                                      at_write.append(dict(server._async_jobs.get("sub_slow", {}))))[1])
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert order[-2:] == ["stored", "woken"], (
            "the woken run polls, and a poll that misses the job in memory falls back to stored "
            f"metadata -- which would still say active: {order}")
        assert at_write, "the ending was never written to the sub-session's metadata"
        assert at_write[-1].get("status") == "completed", at_write[-1]
        assert at_write[-1].get("result") == "done", at_write[-1]
        # and it is marked as not handed over yet: without that, the bookkeeping cannot tell an
        # ending nobody has read from one that was already given away, and drops it too early
        assert at_write[-1].get("_awaiting_poll") is True, at_write[-1]
        assert told == [("parent1", "u1")], "and the wake comes after both"

    @pytest.mark.asyncio
    async def test_the_ringing_does_not_hold_the_instance(self, server, monkeypatch):
        """The run is over before the bell is rung, and the ringing lasts up to five minutes.
        Held that long, the instance answers "already running" to the `continue` the tool
        description pushes the woken caller towards, and `list` reports a run long finished."""
        running = []

        async def wake(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
            running.append(set(server._running_agents))
            return "woke_session"

        monkeypatch.setattr(sam_server, "wake_session", wake)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        assert running == [set()], "the slot was still held while the bell was ringing"

    @pytest.mark.asyncio
    async def test_the_core_is_handed_this_servers_config_and_the_name_of_the_work(
            self, server, monkeypatch):
        """Handed None instead, `wake_blocked` finds no `session_presence` on it: every wake
        dies as "presence is off" and every caller is told it will not be woken, silently. And
        `what` is the only name the work has in the operator's log."""
        seen = []

        async def wake(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
            seen.append((system_config, what))
            return "woke_session"

        monkeypatch.setattr(sam_server, "wake_session", wake)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        (config, what), = seen
        assert config is server.system_config
        assert "sub_slow" in what, what

    @pytest.mark.asyncio
    async def test_without_a_user_for_the_session_it_says_so_and_stops(
            self, server, monkeypatch, caplog):
        """The id is injected per tool call. Missing, there is no session directory to ring at --
        and a caller asleep over the job would wait for good, so the operator hears about it."""
        told = self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        manager = server._get_manager()
        monkeypatch.setattr(manager, "_extract_user_id", lambda *a, **kw: "")
        await self.start(server, wake_when_done=True)
        with caplog.at_level(logging.WARNING, logger=sam_server.logger.name):
            await self.run_to_end(server, agent)

        assert told == []
        assert any("no user for the session" in r.message for r in caplog.records), \
            [r.message for r in caplog.records]

    @pytest.mark.asyncio
    async def test_a_create_does_not_fail_over_the_wording_of_its_own_answer(
            self, server, monkeypatch):
        """The job is running by then. Whatever reading the config runs into, the neutral
        sentence is the one that was there before any of this."""
        self.presence(monkeypatch)

        def explodes(system_config, session_id, user_id):
            raise RuntimeError("the sessions directory is gone")

        monkeypatch.setattr(sam_server, "wake_blocked", explodes)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)

        started = await self.start(server, wake_when_done=True)

        assert started["status"] == "running"
        assert "Use poll or wait" in started["message"], started["message"]

    @pytest.mark.asyncio
    async def test_a_caller_told_it_may_sleep_is_told_when_it_may_not(self, server, monkeypatch):
        """The tool description promises the wake without conditions, because it describes the
        argument and not this session. Three things rule one out before the job runs at all, and
        a caller that ends its turn on the promise waits for good. So the answer says which."""
        self.presence(monkeypatch)
        asked = self.arming(monkeypatch, blocked="session presence is off")
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)

        started = await self.start(server, wake_when_done=True)

        assert asked == [("parent1", "u1")], "it did not ask about the session it would wake"
        assert "NOT be woken" in started["message"], started["message"]
        assert "session presence is off" in started["message"], "the reason is what to act on"
        assert "poll or wait" in started["message"]

    @pytest.mark.asyncio
    async def test_a_sub_agent_that_starts_a_job_is_told_it_is_not_woken(self, server, monkeypatch):
        """A sub-agent's own session is never woken: the run that spawned it takes its answer.
        Told it may sleep, it ended its turn over the job -- its caller got "I am waiting" for an
        answer, and the job's result reached nobody."""
        self.presence(monkeypatch)
        self.arming(monkeypatch)  # the core has nothing against it: it does not look at the link
        agent = SlowAgent()
        session_service = TestCancelReachesABlockingRun.wire(server, agent)
        session_service.session_manager.load_session.side_effect = None
        session_service.session_manager.load_session.return_value = {
            "agent_name": "coordinator", "parent_session": {"session_id": "the_top"}, "context_vars": {}}

        started = await self.start(server, wake_when_done=True)

        assert "NOT be woken" in started["message"], started["message"]
        assert "sub-agent's own session" in started["message"], started["message"]
        assert "end your turn:" not in started["message"], started["message"]

    @pytest.mark.asyncio
    async def test_a_caller_that_can_be_woken_is_told_it_may_sleep(self, server, monkeypatch):
        """The counter-proof: without it the sentence above could be the only one there is."""
        self.presence(monkeypatch)
        self.arming(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)

        started = await self.start(server, wake_when_done=True)

        assert "NOT be woken" not in started["message"], started["message"]
        assert "end your turn" in started["message"], started["message"]

    @pytest.mark.asyncio
    async def test_a_job_nobody_wants_to_sleep_on_is_not_asked_about(self, server, monkeypatch):
        """`wake_when_done` off is the default, and it costs nothing: no lock file is read, and
        the answer is the one every background job gave before any of this existed."""
        self.presence(monkeypatch)
        asked = self.arming(monkeypatch, blocked="session presence is off")
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)

        started = await self.start(server)

        assert asked == []
        assert "woken" not in started["message"], started["message"]

    @pytest.mark.asyncio
    async def test_the_ringing_is_told_when_it_may_stop(self, server, monkeypatch):
        """One ring is not enough: a job that ends while the caller's own turn is still running
        only leaves a marker, and the next LLM step of that turn takes it in the belief that a
        hook passes the input on. Nothing passes on "your sub-agent is done", so the core rings
        again while the session stays held -- and `still_needed` is what stops it, because a ring
        that lands after the woken run has read the result starts a SECOND run.

        The guard is the bookkeeping the manager already keeps: the ending sits in `_async_jobs`
        marked `_awaiting_poll` until somebody reads it, and a poll takes the job with it."""
        guards = []
        self.presence(monkeypatch, guards=guards)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await self.run_to_end(server, agent)

        still_needed, = guards
        assert still_needed is not None and still_needed() is True, \
            "the ending is sitting there unread -- the ringing has to go on"

        await server.manage_sub_agent(
            {"operation": "poll", "instance_id": "sub_slow", "_session_id": "parent1"})

        assert still_needed() is False, "it rings on after the caller read the result itself"

        # And a fresh job under the same id -- what a `continue` on the instance leaves behind --
        # is not an unread ending either: asking only whether SOMETHING is there would ring on
        # over it, and that second ring starts a second woken run.
        server._async_jobs["sub_slow"] = {"instance_id": "sub_slow", "status": "running"}
        assert still_needed() is False, "any job under that id counted as an ending nobody read"

    @pytest.mark.asyncio
    async def test_an_archived_job_hands_over_no_guard_at_all(self, server, monkeypatch):
        """Archived while it ran, the job is dropped from `_async_jobs` the moment it ends -- so
        the guard would answer "already read" on the first ring and stop it, for an ending nobody
        has seen. Nothing here can tell, and saying so is what makes the core ring its budget."""
        guards = []
        self.presence(monkeypatch, guards=guards)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server, wake_when_done=True)
        await agent.started(1)
        await server._archive_job("sub_slow")
        await self.run_to_end(server, agent)

        assert guards == [None], "a guard that cannot see the job must not be handed over"

    @pytest.mark.asyncio
    async def test_a_wait_hands_back_no_bookkeeping_of_ours(self, server, monkeypatch):
        """`_awaiting_poll` says whether WE may drop the job; poll strips it, wait did not, and
        the model read it in every wait_all result."""
        self.presence(monkeypatch)
        agent = SlowAgent()
        TestCancelReachesABlockingRun.wire(server, agent)
        await self.start(server)
        await self.run_to_end(server, agent)
        waited = await server.manage_sub_agent(
            {"operation": "wait", "instance_id": "sub_slow", "_session_id": "parent1"})

        assert waited["status"] == "completed" and waited["result"] == "done"
        assert "_awaiting_poll" not in waited and "task_handle" not in waited, waited


class TestTheRunningSlotKnowsItsHolder:
    """The running slot records the task that took it. A continue refused because a run still going
    holds it is the caller's mistake, logged at INFO; a slot no run holds leaked. And only its holder
    lets go of a slot another run still going holds."""

    @staticmethod
    def logged(caplog):
        """The refusals at INFO, and every record that reads as a fault."""
        records = [r for r in caplog.records if r.name == "plugins.sub_agent_manager.server"]
        return ([r.getMessage() for r in records if r.levelno == logging.INFO and " refused: " in r.getMessage()],
                [r.getMessage() for r in records if r.levelno >= logging.WARNING or r.exc_info])

    @pytest.mark.asyncio
    async def test_a_continue_on_a_background_job_still_going(self, server, caplog):
        import asyncio
        runs = TestCancelReachesABlockingRun
        agent = SlowAgent()
        runs.wire(server, agent)
        await runs.finish(runs.start(server, "create", blocking=False))
        await agent.started(1)
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            refused = await runs.finish(runs.start(server, "continue"))
        handle = server._async_jobs["sub_slow"]["task_handle"]
        agent.releases[0].set()
        await asyncio.wait_for(handle, 5)

        assert "already running" in str(refused.get("error", "")), refused
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 1, caplog.text
        assert "sub_slow" not in server._running_agents and server._slot_holders == {}

    @pytest.mark.asyncio
    async def test_a_continue_on_a_blocking_run_still_going(self, server, caplog):
        runs = TestCancelReachesABlockingRun
        agent = SlowAgent()
        runs.wire(server, agent)
        first = runs.start(server, "continue")
        await agent.started(1)
        with caplog.at_level(logging.INFO, logger="plugins.sub_agent_manager.server"):
            refused = await runs.finish(runs.start(server, "continue"))
        agent.releases[0].set()
        await runs.finish(first)

        assert "already running" in str(refused.get("error", "")), refused
        refusals, faults = self.logged(caplog)
        assert faults == [] and len(refusals) == 1, caplog.text
        assert "sub_slow" not in server._running_agents and server._slot_holders == {}

    @pytest.mark.asyncio
    async def test_a_job_that_ends_twice_leaves_the_continue_after_it_its_slot(self, server, monkeypatch):
        """The job lets go of its slot before it rings its caller; the caller continues the instance
        meanwhile, and a cancel reaches the job while it still rings -- its second ending freed the
        continue's slot, and a second continue could run beside it on the same transcript."""
        import asyncio
        ringing = asyncio.Event()

        async def wake(system_config, session_id, user_id, what="", still_needed=None, started_by=None):
            ringing.set()
            await asyncio.Event().wait()  # rings until the job is cancelled

        monkeypatch.setattr(sam_server, "wake_session", wake)
        TestTheCallerIsWokenWhenItsJobIsDone.arming(monkeypatch)
        runs = TestCancelReachesABlockingRun
        agent = SlowAgent()
        runs.wire(server, agent)
        await runs.finish(runs.start(server, "create", blocking=False, wake_when_done=True))
        handle = server._async_jobs["sub_slow"]["task_handle"]
        await agent.started(1)
        agent.releases[0].set()
        await asyncio.wait_for(ringing.wait(), 5)
        assert "sub_slow" not in server._running_agents, "let go before the bell"

        continued = runs.start(server, "continue")
        await agent.started(2)
        writes = server._get_manager().update_sub_session_metadata
        before = writes.await_count
        handle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle, 5)

        assert "sub_slow" in server._running_agents, "the job's second ending freed the continue's slot"
        # and it stored "cancelled" over the running continue, which dropped out of `list`
        assert [call.kwargs for call in writes.await_args_list[before:]] == [], writes.await_args_list[before:]
        agent.releases[1].set()
        assert "error" not in await runs.finish(continued)
        assert "sub_slow" not in server._running_agents and server._slot_holders == {}


class TestListDoesNotDeclareALiveSubAgentDead:
    """`list` heals state a crash left behind: a sub-agent that looks like it runs but has no task
    here is marked interrupted, and that is written over its metadata. This process is not the only
    one that runs sub-agents, though -- a woken coordinator runs in its own, the API runs beside the
    writer's worker -- so the question is whether ANY process has it in hand."""

    RECORD = {"instance_id": "sub_live", "agent_type": "slow_agent", "status": "active",
              "created_at": "2026-09-18T06:00:00Z", "last_used": "2026-09-18T06:01:00Z",
              "task_summary": "work", "current_activity": "🔧 Running tool: read",
              "activity_updated_at": "2026-09-18T06:02:00Z", "message_count": 2}

    @classmethod
    def wire(cls, server, monkeypatch, lock, presence_on=True):
        """One sub-agent that looks like it runs, and what the core's lock probe says about its
        session: "running", or None for a lock nobody holds."""
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        manager = server._get_manager()
        manager.list_sub_sessions = AsyncMock(return_value=[dict(cls.RECORD)])
        presence = Mock(status=Mock(return_value=lock)) if presence_on else None
        monkeypatch.setattr(sam_server, "presence_for", lambda config: presence)
        return manager, presence

    @staticmethod
    async def listed(server):
        params = {"operation": "list", "_session_id": "parent1", "_agent": Mock(), "_user_id": "u1"}
        return params, await server.manage_sub_agent(params)

    @pytest.mark.asyncio
    async def test_one_another_process_holds_is_left_alone(self, server, monkeypatch):
        manager, presence = self.wire(server, monkeypatch, "running")

        params, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["running"]
        assert manager.update_sub_session_metadata.await_count == 0, \
            "a live sub-agent had its metadata overwritten by a listing"
        # the lock asked about is the SUB-session's, under the user of the session that owns it
        assert presence.status.call_args.args == ("sub_live", "u1"), presence.status.call_args
        # and that user is the one the caller injected, not one found by scanning directories
        assert manager._extract_user_id.call_args.args == ("parent1", params), \
            manager._extract_user_id.call_args

    @pytest.mark.asyncio
    @pytest.mark.parametrize("presence_on", [True, False],
                             ids=["nobody holds it", "presence is off"])
    async def test_one_nobody_runs_is_marked_interrupted(self, server, monkeypatch, presence_on):
        """Off, there is no cross-process answer -- then it falls back to what it always did."""
        manager, _ = self.wire(server, monkeypatch, None, presence_on)

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["interrupted"]
        assert manager.update_sub_session_metadata.await_args.kwargs["status"] == "interrupted"

    @pytest.mark.asyncio
    async def test_words_of_an_ending_mid_run_do_not_make_it_idle(self, server, monkeypatch):
        """Every tool's status scope ends with "completed" by default, and that is the activity
        stored while the next step runs. Read before the lock, it called a worker another process
        is running idle -- and the coordinator was told a working worker was done."""
        manager, presence = self.wire(server, monkeypatch, "running")
        manager.list_sub_sessions.return_value = [{**self.RECORD, "current_activity": "✅ completed"}]

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["running"]

    @pytest.mark.asyncio
    async def test_a_last_word_nobody_holds_is_idle_not_a_crash(self, server, monkeypatch):
        """Runs from before the activity was cleared at every ending left their last status line
        behind ("✅ completed (4 steps)") -- well over a thousand records, each written before the
        run's ending recorded `last_used`. They ended, and a listing must not write "interrupted"
        over them."""
        manager, _ = self.wire(server, monkeypatch, None)
        manager.list_sub_sessions.return_value = [
            {**self.RECORD, "current_activity": "✅ completed (4 steps)",
             "activity_updated_at": "2026-09-18T06:00:59+00:00"}]

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["idle"]
        assert manager.update_sub_session_metadata.await_count == 0

    @pytest.mark.asyncio
    async def test_a_crash_whose_last_line_reads_like_an_ending_is_a_crash(self, server, monkeypatch):
        """Mid-run a tool's status line ends with "completed". A process that dies right after one
        left a run that never recorded its ending -- told "idle", the coordinator reads a result
        that was never written."""
        manager, _ = self.wire(server, monkeypatch, None)
        manager.list_sub_sessions.return_value = [{**self.RECORD, "current_activity": "✅ completed"}]

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["interrupted"]

    @pytest.mark.asyncio
    async def test_one_already_interrupted_is_not_written_again(self, server, monkeypatch):
        """Healing is a one-off: a listing writes only a change, never the same verdict again
        with a new time over it."""
        manager, _ = self.wire(server, monkeypatch, None)
        manager.list_sub_sessions.return_value = [{**self.RECORD, "status": "interrupted"}]

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["interrupted"]
        assert manager.update_sub_session_metadata.await_count == 0

    @pytest.mark.asyncio
    async def test_one_whose_run_is_over_is_idle(self, server, monkeypatch):
        """Stored, a clean ending is "active" like a run under way -- the word said nothing a
        caller could act on. Nothing runs and nothing was left running: idle."""
        manager, _ = self.wire(server, monkeypatch, None)
        manager.list_sub_sessions.return_value = [{**self.RECORD, "current_activity": None}]

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["idle"]
        assert manager.update_sub_session_metadata.await_count == 0

    @pytest.mark.asyncio
    async def test_a_run_elsewhere_that_has_said_nothing_yet_is_running(self, server, monkeypatch):
        """A run holds its lock from right after "start", through its setup, and until after its
        ending event -- with no activity at either end. Asked only where an activity was set, the
        list called it idle there while a poll, which asks the lock, called it running."""
        manager, _ = self.wire(server, monkeypatch, "running")
        manager.list_sub_sessions.return_value = [{**self.RECORD, "current_activity": None}]

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["running"]

    @pytest.mark.asyncio
    async def test_one_this_process_runs_is_running_without_asking_the_lock(self, server, monkeypatch):
        """A run of this process may not have said anything yet -- its own bookkeeping answers."""
        manager, presence = self.wire(server, monkeypatch, None)
        manager.list_sub_sessions.return_value = [{**self.RECORD, "current_activity": None}]
        server._running_agents.add("sub_live")

        _, result = await self.listed(server)

        assert [i["status"] for i in result["instances"]] == ["running"]
        presence.status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_run_that_failed_is_listed_and_an_archived_one_on_request(self, server, monkeypatch):
        """`include_completed` promises archived ones; a failed or cancelled run was held back
        with them, and a caller that had not ended it itself learned of it nowhere."""
        manager, _ = self.wire(server, monkeypatch, None)
        manager.list_sub_sessions.return_value = [
            {**self.RECORD, "instance_id": f"sub_{status}", "status": status, "current_activity": None}
            for status in ("failed", "cancelled", "archived")]

        _, default = await self.listed(server)
        params = {"operation": "list", "_session_id": "parent1", "_agent": Mock(), "_user_id": "u1",
                  "include_completed": True}
        everything = await server.manage_sub_agent(params)

        assert [i["status"] for i in default["instances"]] == ["failed", "cancelled"]
        assert [i["status"] for i in everything["instances"]] == ["failed", "cancelled", "archived"]


class TestAFinishedJobDoesNotStayInMemory:
    """Only a poll ever took one out, and a continue of the same instance. The usual shape is
    create + wait_all + delete, and after it every result text stayed for the life of the process."""

    @staticmethod
    def job(server, instance_id, status, **extra):
        server._async_jobs[instance_id] = {
            "instance_id": instance_id, "status": status, "result": "x" * 1000,
            "parent_session_id": "parent1", "task_handle": None, **extra}

    @staticmethod
    async def archive(server, instance_id):
        return await server.manage_sub_agent(
            {"operation": "delete", "instance_id": instance_id, "_session_id": "parent1", "_agent": Mock()})

    @pytest.mark.asyncio
    async def test_archiving_an_instance_takes_its_finished_job(self, server):
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        self.job(server, "sub_done", "completed")

        await self.archive(server, "sub_done")

        assert "sub_done" not in server._async_jobs

    @pytest.mark.asyncio
    async def test_archiving_leaves_a_running_job_alone(self, server):
        """Its task handle is what a cancel needs; the run does not stop because the entry was tidied."""
        import asyncio
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        task = asyncio.ensure_future(asyncio.sleep(30))
        self.job(server, "sub_busy", "running", task_handle=task)
        try:
            await self.archive(server, "sub_busy")

            assert "sub_busy" in server._async_jobs
        finally:
            task.cancel()

    @pytest.mark.asyncio
    async def test_archiving_a_running_instance_drops_its_job_when_the_run_ends(self, server):
        """The entry stays while it runs -- a cancel needs its handle -- but nobody polls an
        archived instance, so its ending takes it out instead of leaving the result for good."""
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        self.job(server, "sub_busy", "running")

        await self.archive(server, "sub_busy")
        assert server._async_jobs["sub_busy"]["_archived"] is True, "kept, and marked"

        await server._finish_job("sub_busy", {"_session_id": "parent1"}, "completed",
                                 stored={"status": "active"}, manager=server._get_manager(),
                                 result="an answer nobody will ever ask for")

        assert "sub_busy" not in server._async_jobs

    @staticmethod
    def stored(server, listing, messages):
        """A poll with no job in memory: the manager answers from the parent's metadata, and the
        sub-session on disk holds `messages`."""
        manager = AsyncMock()
        manager.list_sub_sessions = AsyncMock(return_value=listing)
        manager._extract_user_id = Mock(return_value="u1")
        manager._session_service.session_manager.load_session = AsyncMock(
            return_value={"messages": messages})
        server._extract_registry = Mock(return_value=Mock())
        server._extract_session_service = Mock(return_value=Mock())
        server._get_manager = Mock(return_value=manager)
        return manager

    ACTIVE = [{"instance_id": "sub_done", "agent_type": "worker", "status": "active"}]
    ARCHIVED = [{"instance_id": "sub_done", "agent_type": "worker", "status": "archived"}]

    @pytest.mark.asyncio
    async def test_a_poll_with_no_job_left_answers_in_the_run_s_own_words(self, server):
        """Whoever reads a job takes it, and a restart leaves none at all -- so this path is the
        normal one, not the exception. It used to answer with a sentence about a persisted
        session, which a model reads AS the sub-agent's answer."""
        self.stored(server, self.ACTIVE, [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "a first pass, asked again afterwards", "tool_calls": None},
            {"role": "user", "content": "again, with the numbers"},
            # `tool_calls: null` is what a finished run leaves on disk: the model defaults it to
            # None and `save_session` dumps it without exclude_none. A guard that asks whether the
            # KEY is there instead of what is in it answers "cut off in a tool call" for every
            # run that ever ended cleanly.
            {"role": "assistant", "content": "the answer nobody could read before", "tool_calls": None},
            {"role": "tool", "content": "42"}])

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "completed"
        # the LAST thing the run said, and something the run said: a transcript ends in whatever
        # the last step produced, and an earlier pass is not the answer
        assert result["result"] == "the answer nobody could read before"

    @pytest.mark.asyncio
    async def test_an_archived_instance_is_not_a_stranger_to_poll(self, server):
        """Archiving to make room happens behind the caller's back, and `list_sub_sessions` keeps
        only active and interrupted ones -- so its poll used to answer "not found" about a run it
        started itself."""
        manager = self.stored(server, [], [{"role": "assistant", "content": "archived, but finished"}])
        manager.list_sub_sessions = AsyncMock(side_effect=[[], self.ARCHIVED])
        server._runs_in_another_process = AsyncMock(return_value=False)

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "completed"
        assert result["result"] == "archived, but finished"
        # what is asked, not just that something was: archived ones are only in the full listing,
        # and only this instance's sub-agents are ours to answer about
        assert manager.list_sub_sessions.await_args_list[1].kwargs == {
            "include_completed": True, "creator_plugin": server.name}

    @pytest.mark.asyncio
    async def test_an_archived_instance_that_still_runs_is_not_finished(self, server):
        """Archiving to make room takes the OLDEST, running or not, and "archived" says nothing
        about whether the run is over. Two ways to still be running with no job here: a blocking
        run never has one, and a job lives in the process that started it -- a woken coordinator
        asks from another. Answering "completed" there ends a wait on an answer not yet written.
        """
        manager = self.stored(server, [], [{"role": "assistant", "content": "half a thought"}])
        manager.list_sub_sessions = AsyncMock(side_effect=[[], self.ARCHIVED])
        server._runs_in_another_process = AsyncMock(return_value=True)

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "running"
        assert "result" not in result, "nothing to hand over yet"
        assert result["message"] == "Archived while it runs; its ending is still to come"
        assert server._runs_in_another_process.await_args.args == ("sub_done", "u1")

    @pytest.mark.asyncio
    async def test_a_run_cut_off_in_a_tool_call_has_no_answer_to_give(self, server):
        """The dangerous near-miss: its last word is a tool call, so there IS an earlier answer
        in the transcript -- and it is not the result. Measured in this repo's own sessions, a
        run that ended there hands its mid-run self-check to the coordinator as the sub-agent's
        result. Better the sentence that says nothing than a paragraph that says the wrong thing.
        """
        self.stored(server, self.ACTIVE, [
            {"role": "assistant", "content": "a self-check from the middle of the run"},
            {"role": "tool", "content": "42"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}])

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "completed"
        assert result["result"] == "Sub-agent execution completed (session persisted)"

    @pytest.mark.asyncio
    async def test_the_narration_before_a_tool_call_is_not_an_answer_either(self, server):
        """The shape the guard above missed: the run said something AND asked for a tool in the
        same step. What stands there is what a model narrates before it works -- "let me look at
        the configuration first" -- and it is handed over as the result, where the caller reads
        it as the sub-agent's finding. Having no answer is the same either way."""
        self.stored(server, self.ACTIVE, [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "Let me look at the configuration first.",
             "tool_calls": [{"id": "1", "function": {"name": "read", "arguments": "{}"}}]}])

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "completed"
        assert result["result"] == "Sub-agent execution completed (session persisted)"

    @pytest.mark.asyncio
    async def test_a_run_another_process_holds_is_not_finished_either(self, server):
        """The same ending as the archived one above, without the archiving: the instance is
        active, this process has no job for it, and its transcript is still being written. A
        coordinator woken for one sub-agent polls its others from a process of its own -- and was
        handed the middle of a run as its result."""
        manager = self.stored(server, self.ACTIVE, [{"role": "assistant", "content": "half a thought"}])
        server._runs_in_another_process = AsyncMock(return_value=True)

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "running"
        assert "result" not in result, "nothing to hand over yet"
        assert result["message"] == "Still running; its ending is still to come"
        # the lock asked about is the SUB-session's, under the user of the session that OWNS it
        assert server._runs_in_another_process.await_args.args == ("sub_done", "u1")
        assert manager._extract_user_id.call_args.args[0] == "parent1"

    @pytest.mark.asyncio
    async def test_the_lock_beside_the_sub_session_is_what_answers(self, server, monkeypatch):
        """The shape with no job anywhere: a BLOCKING create never files one, so a poll from
        another turn of the same session finds the instance active and nothing of ours running.
        Here the real helper runs -- the lock beside the sub-session, asked under the user of the
        session that owns it -- and its answer must not be worded as another process's."""
        self.stored(server, self.ACTIVE, [{"role": "assistant", "content": "half a thought"}])
        presence = Mock(status=Mock(return_value="running"))
        monkeypatch.setattr(sam_server, "presence_for", lambda config: presence)

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "running"
        assert result["message"] == "Still running; its ending is still to come"
        assert presence.status.call_args.args == ("sub_done", "u1")

    @pytest.mark.asyncio
    async def test_a_blocking_run_of_this_process_is_not_finished_either(self, server, monkeypatch):
        """With `session_presence` off there is no cross-process answer at all, and a blocking run
        has no job of its own anywhere -- so a poll would go straight back to calling a live run
        completed. What runs HERE is the other half of the question `list` asks."""
        self.stored(server, self.ACTIVE, [{"role": "assistant", "content": "half a thought"}])
        monkeypatch.setattr(sam_server, "presence_for", lambda config: None)
        server._running_agents.add("sub_done")

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "running"
        assert result["message"] == "Still running; its ending is still to come"

    @pytest.mark.asyncio
    async def test_a_wait_with_no_job_here_looks_less_and_less_often(self, server, monkeypatch):
        """Every look on this branch reads the parent's session file, re-parsed whenever a running
        sub-agent wrote its activity -- which it does all the time. At the half second the turn
        WITH a job runs at, a forty-minute sub-agent would be four thousand of those, once per
        waiter of a `wait_all`. So this turn doubles its pause up to `WAIT_DB_POLL_MAX`."""
        import asyncio
        self.stored(server, self.ACTIVE, [{"role": "assistant", "content": "the whole thought"}])
        server._runs_in_another_process = AsyncMock(return_value=True)
        server.default_wait_timeout = 300
        slept = []
        real_sleep = asyncio.sleep

        async def recorded(seconds, *args, **kwargs):
            slept.append(seconds)
            if len(slept) == 6:  # enough to see the doubling and the ceiling; then let it end
                server._runs_in_another_process = AsyncMock(return_value=False)
            return await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", recorded)
        result = await asyncio.wait_for(
            server._handle_wait({"instance_id": "sub_done", "_session_id": "parent1"}), 10)

        assert result["status"] == "completed"
        assert slept == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0], "the pause is meant to grow and to stop growing"

    @pytest.mark.asyncio
    async def test_a_wait_over_a_run_in_another_process_does_not_call_it_gone(self, server):
        """`wait` reads its own poll, and "running" is not one of the endings it knows: it fell
        through to "disappeared during wait" -- said about a sub-agent that is working. A wait
        ends when the job ends or when its timeout does, and on nothing else."""
        import asyncio
        self.stored(server, self.ACTIVE, [{"role": "assistant", "content": "half a thought"}])
        server._runs_in_another_process = AsyncMock(return_value=True)
        server.default_wait_timeout = 0.2

        result = await asyncio.wait_for(  # a wait that stops ending fails here instead of hanging
            server._handle_wait({"instance_id": "sub_done", "_session_id": "parent1"}), 5)

        assert result["status"] == "error"
        assert "Timeout" in result["error"] and "disappeared" not in result["error"]

    @pytest.mark.asyncio
    async def test_a_wait_over_a_run_in_another_process_hands_over_its_ending(self, server):
        """The other half, and the one that says it really waited: the run ends while the wait is
        in it, and the answer it gets is the run's own -- not the timeout, and not the middle of
        the transcript it saw on the way."""
        import asyncio
        self.stored(server, self.ACTIVE, [{"role": "assistant", "content": "the whole thought"}])
        # asked at the wait's own poll, then once per turn of its loop: running, running, done
        server._runs_in_another_process = AsyncMock(side_effect=[True, True, False])
        server.default_wait_timeout = 30

        result = await asyncio.wait_for(
            server._handle_wait({"instance_id": "sub_done", "_session_id": "parent1"}), 10)

        assert result["status"] == "completed" and result["result"] == "the whole thought"
        assert server._runs_in_another_process.await_count == 3, "it asked again every turn"

    @pytest.mark.asyncio
    async def test_a_transcript_that_cannot_be_read_is_not_an_error(self, server):
        """The session file may be gone, unreadable or half-written. None of that is worth
        turning a poll into a failure: the caller keeps the answer it had before this existed."""
        self.stored(server, self.ACTIVE, [])
        server._get_manager.return_value._session_service.session_manager.load_session = AsyncMock(
            side_effect=OSError("the sessions directory is gone"))

        result = await server._handle_poll({"instance_id": "sub_done", "_session_id": "parent1"})

        assert result["status"] == "completed"
        assert result["result"] == "Sub-agent execution completed (session persisted)"

    @pytest.mark.asyncio
    async def test_an_ordinary_ending_stays_active(self, server):
        """The counterpart: only an instance that WAS archived is stored as archived. A normal
        run keeps the status its success path writes, or every finished sub-agent would vanish
        from the listing."""
        stored_kwargs = {}
        manager = AsyncMock()
        manager.update_sub_session_metadata = AsyncMock(
            side_effect=lambda **kw: stored_kwargs.update(kw))
        self.job(server, "sub_plain", "running")

        await server._finish_job("sub_plain", {"_session_id": "parent1"}, "completed",
                                 stored={"status": "active", "last_used": "now"}, manager=manager,
                                 result="done")

        assert stored_kwargs["status"] == "active", stored_kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("job_status", ["failed", "cancelled"])
    async def test_an_archived_run_that_ended_badly_keeps_its_verdict(self, server, job_status):
        """The other half of the rule above, and the one that hides a dead fan-out if it is
        wrong: "failed" and "cancelled" are the run's verdict and they say themselves that it is
        over. Stored as "archived" they read as an orderly end -- poll would answer "completed"
        for a sub-agent that never finished, and wait_all would count it among the good ones."""
        stored_kwargs = {}
        manager = AsyncMock()
        manager.update_sub_session_metadata = AsyncMock(
            side_effect=lambda **kw: stored_kwargs.update(kw))
        self.job(server, "sub_bad", "running", _archived=True)

        await server._finish_job("sub_bad", {"_session_id": "parent1"}, job_status,
                                 stored={"status": job_status, "error": "it broke"},
                                 manager=manager)

        assert stored_kwargs["status"] == job_status, stored_kwargs

    @pytest.mark.asyncio
    async def test_an_archiving_that_wrote_nothing_is_not_reported_as_done(self, server):
        """The metadata write gives up quietly -- parent unreadable, no sub_agents metadata, id
        not among them -- and raises nothing. Told "archived" anyway, the caller believes an
        instance is gone that is still active, still counted and still pollable, and the result
        it could have read has been dropped underneath it."""
        TestCancelReachesABlockingRun.wire(server, SlowAgent())
        self.job(server, "sub_done", "completed")
        manager = server._get_manager()
        manager.update_sub_session_metadata = AsyncMock(return_value=False)

        answer = await self.archive(server, "sub_done")

        assert answer["status"] == "error", answer
        assert "sub_done" in server._async_jobs, "its result is still the caller's to read"

    def test_the_manager_it_builds_reports_what_it_archived_itself(self, server):
        """The second way to archive, and the one that skips this server: at the limit the
        manager archives the oldest sub-agent to make room, down where the id never surfaces
        here. Its job would keep the whole result text for the life of the process.

        The real factory, not the one a fixture stubs -- what is asserted is the wiring.
        """
        manager = SubAgentManagerServer._get_manager(server, Mock())

        assert manager._on_archived == server._archive_job

    @pytest.mark.asyncio
    async def test_wait_all_waits_once_per_instance(self, server):
        """Two waits on one job race for it, and the loser finds it taken: it answers from the
        stored state, without the result."""
        seen = []

        async def mock_wait(params):
            seen.append(params["instance_id"])
            return {"instance_id": params["instance_id"], "status": "completed"}

        server._handle_wait = mock_wait
        result = await server._handle_wait_all(
            {"instance_ids": ["sub_a", "sub_a", "sub_b"], "_session_id": "parent1"})

        assert seen == ["sub_a", "sub_b"]
        assert result["completed"] == 2


class TestWhatEveryRunSetsUpAndReports:
    """The three copies of "run a sub-agent" were merged into `_prepare_agent` and `_consume_run`
    (18.09.2026). The suite that was the net under that merge turned out to leave several of
    their branches unmeasured, found by mutating them one at a time; these are those branches:
    what a run is handed before it starts, and what it reports while it runs.

    Each one is silent when it breaks -- a sub-session filed under the wrong user or written into
    the coordinator's own session, a panel whose activity never moves, a prompt rendered from the
    snapshot a refresh was supposed to replace. Which is why the assertions name WHICH session and
    WHICH user, not just that somebody was told something.
    """

    @staticmethod
    def agent():
        """A registry agent as _prepare_agent finds it: shared, and carrying whatever the run
        before it left behind."""
        made = Mock()
        made.agent_config = Mock(default_llm_profile="normal")
        made._session_tracker = Mock()
        made._session_service = "whatever the last run left here"
        return made

    @staticmethod
    def manager(user_id="u1", stored_vars=None):
        manager = AsyncMock()
        manager._extract_user_id = Mock(return_value=user_id)
        manager._session_service.session_manager.load_session = AsyncMock(
            return_value={"context_vars": stored_vars or {}})
        return manager

    @pytest.mark.asyncio
    async def test_the_run_persists_through_the_session_service_it_was_given(self, server):
        """The agent comes from the registry and is shared; its `_session_service` is whatever
        the previous run set. Left alone, a sub-agent writes its transcript through somebody
        else's store -- and `info` then reads an empty one."""
        agent, manager = self.agent(), self.manager()
        session_service = manager._session_service

        await server._prepare_agent(agent, manager, session_service, {},
                                    parent_session_id="parent1", instance_id="sub_1",
                                    agent_name="worker")

        assert agent._session_service is session_service

    @pytest.mark.asyncio
    async def test_the_session_is_filed_under_the_user_the_manager_resolved(self, server):
        """Tool execution reads the user id from this metadata, and every session path is built
        from it. A wrong one files the sub-session in another user's directory, where its own
        `info` will not find it again."""
        agent, manager = self.agent(), self.manager(user_id="the_real_user")
        params = {"_user_id": "the_real_user"}

        returned = await server._prepare_agent(agent, manager, manager._session_service, params,
                                               parent_session_id="parent1", instance_id="sub_1",
                                               agent_name="worker")

        instance, metadata = agent._session_tracker.set_session_metadata.call_args.args
        assert instance == "sub_1", "the sub-agent's session, not the coordinator's"
        assert metadata["user_id"] == "the_real_user"
        assert metadata["agent_name"] == "worker"
        assert returned == "the_real_user", "and the caller is told the same user"
        # asked the right way round: the other order makes the params the session id, and the
        # user resolves to "anonymous" -- a sub-session in a directory its own `info` never finds
        manager._extract_user_id.assert_called_once_with("parent1", params)

    @pytest.mark.asyncio
    async def test_the_vars_the_caller_resolved_reach_the_session(self, server):
        """A `continue` refreshes the vars against the parent's live state before the run. If
        they are never set, the prompt renders from the snapshot the sub-agent inherited at
        creation -- the coordinator says `aufgabe=World`, the task text says World, and
        `{{ aufgabe }}` still says Idee, in one prompt."""
        agent, manager = self.agent(), self.manager()

        await server._prepare_agent(agent, manager, manager._session_service, {},
                                    parent_session_id="parent1", instance_id="sub_1",
                                    agent_name="worker", context_vars={"aufgabe": "World"})

        agent._session_tracker.set_session_template_vars.assert_called_once_with(
            "sub_1", {"aufgabe": "World"})
        # and the session's own vars are not read over them: that snapshot is what the refresh
        # exists to replace
        manager._session_service.session_manager.load_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_them_the_session_s_own_vars_are_read(self, server):
        """The other half: a create or a background job brings none, and then what the sub-agent
        inherited when it was created is what its prompt renders from. Read from its session, not
        from the parent's -- the parent's live values are the caller's job to resolve."""
        agent, manager = self.agent(), self.manager(user_id="u1", stored_vars={"aufgabe": "Idee"})

        await server._prepare_agent(agent, manager, manager._session_service, {},
                                    parent_session_id="parent1", instance_id="sub_1",
                                    agent_name="worker")

        manager._session_service.session_manager.load_session.assert_awaited_once_with(
            "u1", "sub_1")
        agent._session_tracker.set_session_template_vars.assert_called_once_with(
            "sub_1", {"aufgabe": "Idee"})

    @pytest.mark.asyncio
    async def test_a_run_says_what_it_is_doing_while_it_does_it(self, server):
        """The only live signal there is: `list` and the panel show this, and the `list` healing
        reads it to tell a crashed run from a working one. Without it a working sub-agent looks
        idle for the whole run, and its last activity stays whatever it was."""
        manager = self.manager()
        agent, asked = Mock(), {}

        async def run_events(**kwargs):
            asked.update(kwargs)
            yield {"type": "thinking_delta"}
            yield {"type": "tool_call", "action": "file_ops_read"}
            yield {"type": "mcp_call", "action": "legacy_name"}  # until every deployed side is new
            yield {"type": "status", "phase": "progress", "message": "reading"}
            yield {"type": "final", "summary": "done"}
            yield {"type": "end"}

        agent.run_events = run_events
        await server._consume_run(agent, manager, parent_session_id="parent1",
                                  instance_id="sub_1", task="the task", request_id="r")

        # The run belongs to the sub-agent's session, not the coordinator's: the other way round
        # the sub-agent writes its transcript into the session of the agent that spawned it.
        assert asked["session_id"] == "sub_1"
        assert asked["task"] == "the task" and asked["request_id"] == "r"

        # WHICH sub-agent, under WHICH parent -- swapped, the activity is written where nobody
        # looks, and `list` reads an empty one and cannot tell a working run from a crashed one.
        said = [call.args[2] for call in manager.update_sub_agent_activity.call_args_list]
        for tool in ("file_ops_read", "legacy_name"):
            assert any(tool in (s or "") for s in said), tool
            manager.update_sub_agent_activity.assert_any_call(
                "parent1", "sub_1", f"🔧 Running tool: {tool}")
        assert any("reading" in (s or "") for s in said), "a status event is activity too"
        assert len([s for s in said if s]) >= 4, said
        assert said[-1] is None, "and it is cleared when the run is over"

    @pytest.mark.asyncio
    async def test_a_run_says_it_runs_while_it_holds_its_session_and_not_a_moment_longer(self, server):
        """An activity nobody holds reads as a crash to every other process, and a `list` there
        writes "interrupted" over the run. The run takes its session's lock only after its "start"
        event (Agent._presence_hold) and lets go of it before its trailing status lines and "end"
        (Agent._run_events' finally) -- so a word before the one or after the other heals a run
        that is fine."""
        manager = self.manager()
        agent, held, seen = Mock(), [False], []

        async def run_events(**kwargs):
            yield {"type": "start"}
            held[0] = True  # the generator resumes after the start event: the hold comes here
            yield {"type": "status", "phase": "start", "message": "worker started"}
            yield {"type": "final", "summary": "done"}
            held[0] = False  # the finally around the run lets go before what follows
            yield {"type": "status", "phase": "end", "message": "completed (1 steps)"}
            yield {"type": "end"}

        async def track(parent, instance, activity):
            seen.append((activity, held[0]))

        agent.run_events = run_events
        manager.update_sub_agent_activity = AsyncMock(side_effect=track)
        await server._consume_run(agent, manager, parent_session_id="parent1",
                                  instance_id="sub_1", task="t", request_id="r")

        assert seen[0] == ("⚙️ worker started", True), seen
        cleared = next(i for i, (activity, _) in enumerate(seen) if activity is None)
        assert seen[cleared][1] is True, f"cleared only after the run let go of its session: {seen}"
        assert all(activity is None for activity, _ in seen[cleared:]), f"said after its ending: {seen}"

    @pytest.mark.asyncio
    async def test_a_run_that_raises_does_not_leave_its_activity_behind(self, server):
        """Only a process that dies leaves an activity behind -- that is what reads as
        interrupted. A run that raised is over, and says so."""
        manager = self.manager()
        agent = Mock()

        async def run_events(**kwargs):
            yield {"type": "tool_call", "action": "read"}
            raise RuntimeError("provider down")

        agent.run_events = run_events
        with pytest.raises(RuntimeError):
            await server._consume_run(agent, manager, parent_session_id="parent1",
                                      instance_id="sub_1", task="t", request_id="r")

        said = [call.args[2] for call in manager.update_sub_agent_activity.call_args_list]
        assert said[-1] is None, said

    @pytest.mark.asyncio
    async def test_a_run_is_over_when_it_says_end(self, server):
        """"final" does not stop the loop -- the generator has to run out for the messages to be
        persisted -- so "end" is what stops it. Reading on past it consumes events of a run that
        is finished, and a later "final" would overwrite the answer already collected."""
        manager = self.manager()
        agent, after_end, asked = Mock(), [], {}

        async def run_events(**kwargs):
            asked.update(kwargs)
            yield {"type": "final", "summary": "the answer"}
            yield {"type": "end"}
            after_end.append("asked for more")
            yield {"type": "final", "summary": "a second run's answer"}

        agent.run_events = run_events
        result = await server._consume_run(agent, manager, parent_session_id="parent1",
                                           instance_id="sub_1", task="t", request_id="r")

        assert result == "the answer"
        assert after_end == [], "nothing after the end is read"
        assert asked["session_id"] == "sub_1", "and it ran in the sub-agent's own session"

