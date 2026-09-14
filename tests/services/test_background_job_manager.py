"""Tests for BackgroundJobManager.

Tests cover:
- Job creation and lifecycle
- Event buffering
- Job cancellation
- SSE client tracking
- Cleanup of old jobs
- Concurrent access
"""
import asyncio
import contextlib
import pytest
from unittest.mock import MagicMock

from agent_system.services.background_job_manager import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
    get_background_job_manager,
    reset_background_job_manager,
)


@pytest.fixture
def job_manager():
    """Create a fresh BackgroundJobManager for each test."""
    reset_background_job_manager()
    manager = BackgroundJobManager()
    yield manager
    # Clean up any running tasks
    for job in manager._jobs.values():
        if not job.task.done():
            job.task.cancel()


@pytest.fixture
def reset_singleton():
    """Reset singleton before and after test."""
    reset_background_job_manager()
    yield
    reset_background_job_manager()


class TestJobStatus:
    """Test JobStatus enum."""
    
    def test_status_values(self):
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.CANCELLED.value == "cancelled"


class TestBackgroundJob:
    """Test BackgroundJob dataclass."""
    
    def test_default_values(self):
        queue = asyncio.Queue()
        task = MagicMock(spec=asyncio.Task)
        
        job = BackgroundJob(
            request_id="req1",
            user_id="user1",
            agent_name="test_agent",
            session_id="sess1",
            task=task,
            event_queue=queue,
        )
        
        assert job.request_id == "req1"
        assert job.user_id == "user1"
        assert job.agent_name == "test_agent"
        assert job.session_id == "sess1"
        assert job.status == JobStatus.RUNNING
        assert job.result is None
        assert job.error_message is None
        assert job.completed_at is None
        assert job.sse_client_count == 0
        assert job.created_at > 0
        # New fields for reconnect support
        assert job.actual_session_id is None
        assert job.last_status_message is None
        assert job.task_description is None


class TestBackgroundJobManager:
    """Test BackgroundJobManager functionality."""
    
    @pytest.mark.asyncio
    async def test_create_job_basic(self, job_manager):
        """Test basic job creation."""
        events_received = []
        
        async def simple_runner():
            yield {"type": "start"}
            yield {"type": "data", "value": 1}
            yield {"type": "end"}
        
        job = await job_manager.create_job(
            request_id="req1",
            user_id="user1",
            agent_name="test_agent",
            session_id="sess1",
            agent_runner=simple_runner,
        )
        
        assert job.request_id == "req1"
        assert job.user_id == "user1"
        assert job.agent_name == "test_agent"
        assert job.session_id == "sess1"
        assert job.status == JobStatus.RUNNING
        
        # Wait for job to complete and collect events
        while True:
            event = await asyncio.wait_for(job.event_queue.get(), timeout=2.0)
            if event is None:
                break
            events_received.append(event)
        
        assert len(events_received) == 3
        assert events_received[0] == {"type": "start"}
        assert events_received[1] == {"type": "data", "value": 1}
        assert events_received[2] == {"type": "end"}
        
        # Give task time to update status
        await asyncio.sleep(0.1)
        assert job.status == JobStatus.COMPLETED
    
    @pytest.mark.asyncio
    async def test_create_job_with_error(self, job_manager):
        """Test job that raises an exception."""
        async def failing_runner():
            yield {"type": "start"}
            raise ValueError("Test error")
        
        job = await job_manager.create_job(
            request_id="req1",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=failing_runner,
        )
        
        # Collect events until None
        events = []
        while True:
            event = await asyncio.wait_for(job.event_queue.get(), timeout=2.0)
            if event is None:
                break
            events.append(event)
        
        assert len(events) == 1
        assert events[0] == {"type": "start"}
        
        # Give task time to update status
        await asyncio.sleep(0.1)
        assert job.status == JobStatus.FAILED
        assert "Test error" in job.error_message
    
    @pytest.mark.asyncio
    async def test_create_job_refuses_duplicate_running_request_id(
        self, job_manager,
    ):
        """Check-and-register must be ONE atomic step.

        The callers' duplicate guard (app.py's _validate_client_request_id)
        is a check-then-act with a wide window — it runs in the request
        handler while create_job only runs once the SSE body streams — so
        two concurrent requests both passed it and both started a full
        agent run. Registering blindly then left the older run alive but
        unreachable by id: no reconnect, and cancel_job would have hit the
        younger run instead.
        """
        from agent_system.services.background_job_manager import (
            DuplicateRequestIdError,
        )
        may_finish = asyncio.Event()
        started = []

        async def runner():
            started.append(1)
            yield {"type": "ev"}
            await may_finish.wait()

        first = await job_manager.create_job(
            request_id="dup0", user_id="u", agent_name="a",
            session_id=None, agent_runner=runner,
        )
        await asyncio.sleep(0)  # let the first wrapper start

        with pytest.raises(DuplicateRequestIdError):
            await job_manager.create_job(
                request_id="dup0", user_id="u", agent_name="a",
                session_id=None, agent_runner=runner,
            )

        assert await job_manager.get_job("dup0") is first, (
            "the refused duplicate displaced the live job anyway"
        )
        may_finish.set()
        await asyncio.wait_for(first.task, timeout=2.0)
        assert started == [1], f"a second agent run started: {started!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome, expected",
        [
            ("complete", JobStatus.COMPLETED),
            ("raise", JobStatus.FAILED),
            ("cancel", JobStatus.CANCELLED),
        ],
    )
    async def test_terminal_status_is_written_to_its_own_job(
        self, job_manager, outcome, expected,
    ):
        """A finishing job must only write its OWN status — on every one
        of the three terminal paths.

        Writing through self._jobs[request_id] instead meant that if the
        dict had since been rebound to another job, the finishing run
        marked THAT still-running job terminal: a live agent reported as
        done. Parametrised because the three branches are separate code:
        covering only 'completed' let the failure and cancel paths keep
        the old lookup silently.
        """
        may_finish = asyncio.Event()

        async def runner():
            yield {"type": "ev"}
            await may_finish.wait()
            if outcome == "raise":
                raise ValueError("boom")

        job = await job_manager.create_job(
            request_id="own1", user_id="u", agent_name="a",
            session_id=None, agent_runner=runner,
        )
        await asyncio.sleep(0)

        # Rebind the id to a foreign job, exactly as a racing duplicate
        # would have. Bypasses create_job (which now refuses duplicates) —
        # the point here is the WRITE path, not the registration.
        foreign = BackgroundJob(
            request_id="own1", user_id="u", agent_name="other",
            session_id=None, task=MagicMock(spec=asyncio.Task),
            event_queue=asyncio.Queue(),
        )
        job_manager._jobs["own1"] = foreign

        may_finish.set()
        if outcome == "cancel":
            job.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(job.task), timeout=2.0)
        await asyncio.sleep(0.05)

        assert job.status == expected
        assert foreign.status == JobStatus.RUNNING, (
            f"the finishing job wrote {foreign.status} onto a foreign, "
            "still-running job — status lies about a live agent"
        )

    @pytest.mark.asyncio
    async def test_get_job(self, job_manager):
        """Test getting a job by ID."""
        async def simple_runner():
            yield {"type": "data"}
        
        job = await job_manager.create_job(
            request_id="req123",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=simple_runner,
        )
        
        # Get existing job
        found_job = await job_manager.get_job("req123")
        assert found_job is job
        
        # Get non-existing job
        not_found = await job_manager.get_job("nonexistent")
        assert not_found is None
    
    @pytest.mark.asyncio
    async def test_cancel_job_graceful(self, job_manager):
        """Test graceful cancellation - sets token, agent detects and shuts down."""
        from agent_system.core.cancellation import get_cancellation_manager
        
        cancellation_detected = asyncio.Event()
        
        async def cancellation_aware_runner():
            """Runner that checks cancellation token like a real agent."""
            cancellation_manager = get_cancellation_manager()
            token = cancellation_manager.create_token("req_graceful")
            
            yield {"type": "start"}
            
            # Simulate agent loop checking for cancellation
            for i in range(100):
                if token.is_cancelled:
                    cancellation_detected.set()
                    yield {"type": "cancelled", "reason": "User requested cancellation"}
                    return  # Clean exit
                await asyncio.sleep(0.05)
            
            yield {"type": "end"}
        
        job = await job_manager.create_job(
            request_id="req_graceful",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=cancellation_aware_runner,
        )
        
        # Wait for job to start
        await asyncio.sleep(0.1)
        assert job.status == JobStatus.RUNNING
        
        # Cancel gracefully (no force timeout)
        result = await job_manager.cancel_job("req_graceful")
        assert result is True
        
        # Wait for agent to detect cancellation and shutdown cleanly
        await asyncio.wait_for(cancellation_detected.wait(), timeout=2.0)
        
        # Give the job time to complete cleanly
        await asyncio.sleep(0.2)
        assert job.status == JobStatus.COMPLETED  # Completed cleanly, not CANCELLED
    
    @pytest.mark.asyncio
    async def test_cancel_job_force(self, job_manager):
        """Test force cancellation after timeout."""
        cancel_event = asyncio.Event()
        
        async def unresponsive_runner():
            """Runner that ignores cancellation token."""
            yield {"type": "start"}
            try:
                await asyncio.sleep(10)  # Long running, doesn't check cancellation
                yield {"type": "end"}
            except asyncio.CancelledError:
                cancel_event.set()
                raise
        
        job = await job_manager.create_job(
            request_id="req_force",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=unresponsive_runner,
        )
        
        # Wait for job to start
        await asyncio.sleep(0.1)
        assert job.status == JobStatus.RUNNING
        
        # Cancel with force timeout (very short for test)
        result = await job_manager.cancel_job("req_force", force_timeout=0.2)
        assert result is True
        
        # Wait for force cancellation
        await asyncio.sleep(0.3)
        assert job.status == JobStatus.CANCELLED
    
    @pytest.mark.asyncio
    async def test_cancel_nonexistent_job(self, job_manager):
        """Test cancelling a job that doesn't exist."""
        result = await job_manager.cancel_job("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_job_propagates_to_sub_requests(self, job_manager):
        """Test that cancelling a job also cancels sub-request tokens via CancellationManager.
        
        This is critical for sub-agents: when parent agent is cancelled,
        sub-agents with request_ids like "parent_sub_xxx" should also be cancelled.
        """
        from agent_system.core.cancellation import get_cancellation_manager
        
        # Create tokens for parent and sub-requests (simulating sub-agent scenario)
        cancellation_manager = get_cancellation_manager()
        parent_token = cancellation_manager.create_token("parent_123")
        sub_token_1 = cancellation_manager.create_token("parent_123_sub_abc")
        sub_token_2 = cancellation_manager.create_token("parent_123_sub_def")
        unrelated_token = cancellation_manager.create_token("other_request")
        
        # Create a job with the parent request_id
        async def slow_runner():
            yield {"type": "start"}
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
        
        await job_manager.create_job(
            request_id="parent_123",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=slow_runner,
        )
        
        # Wait for job to start
        await asyncio.sleep(0.1)
        
        # Cancel the parent job
        result = await job_manager.cancel_job("parent_123")
        assert result is True
        
        # All tokens with the parent prefix should be cancelled
        assert parent_token.is_cancelled, "Parent token should be cancelled"
        assert sub_token_1.is_cancelled, "Sub-request token 1 should be cancelled"
        assert sub_token_2.is_cancelled, "Sub-request token 2 should be cancelled"
        
        # Unrelated token should NOT be cancelled
        assert not unrelated_token.is_cancelled, "Unrelated token should NOT be cancelled"
        
        # Cleanup
        cancellation_manager.unregister_request("parent_123")
        cancellation_manager.unregister_request("parent_123_sub_abc")
        cancellation_manager.unregister_request("parent_123_sub_def")
        cancellation_manager.unregister_request("other_request")
    
    @pytest.mark.asyncio
    async def test_sse_client_tracking(self, job_manager):
        """Test SSE client count tracking."""
        async def simple_runner():
            yield {"type": "data"}
        
        job = await job_manager.create_job(
            request_id="req1",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=simple_runner,
        )
        
        assert job.sse_client_count == 0
        
        # Increment
        await job_manager.increment_sse_client("req1")
        assert job.sse_client_count == 1
        
        await job_manager.increment_sse_client("req1")
        assert job.sse_client_count == 2
        
        # Decrement
        await job_manager.decrement_sse_client("req1")
        assert job.sse_client_count == 1
        
        await job_manager.decrement_sse_client("req1")
        assert job.sse_client_count == 0
        
        # Decrement below 0 should stay at 0
        await job_manager.decrement_sse_client("req1")
        assert job.sse_client_count == 0
    
    @pytest.mark.asyncio
    async def test_get_active_jobs(self, job_manager):
        """Test getting list of active jobs."""
        async def slow_runner():
            yield {"type": "start"}
            await asyncio.sleep(10)
        
        # Create multiple jobs
        await job_manager.create_job(
            request_id="req1",
            user_id="user1",
            agent_name="agent1",
            session_id=None,
            agent_runner=slow_runner,
        )
        
        await job_manager.create_job(
            request_id="req2",
            user_id="user2",
            agent_name="agent2",
            session_id=None,
            agent_runner=slow_runner,
        )
        
        await asyncio.sleep(0.1)  # Let jobs start
        
        # Get all active jobs
        active = await job_manager.get_active_jobs()
        assert len(active) == 2
        assert any(j["request_id"] == "req1" for j in active)
        assert any(j["request_id"] == "req2" for j in active)
        
        # Get by user
        user1_jobs = await job_manager.get_active_jobs(user_id="user1")
        assert len(user1_jobs) == 1
        assert user1_jobs[0]["request_id"] == "req1"
    
    @pytest.mark.asyncio
    async def test_get_all_jobs(self, job_manager):
        """Test getting all jobs including completed."""
        async def quick_runner():
            yield {"type": "data"}
        
        job = await job_manager.create_job(
            request_id="req1",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=quick_runner,
        )
        
        # Wait for completion
        while job.status == JobStatus.RUNNING:
            await asyncio.sleep(0.1)
        
        # Without include_completed, should be empty (no running jobs)
        running = await job_manager.get_all_jobs(include_completed=False)
        assert len(running) == 0
        
        # With include_completed, should include our job
        all_jobs = await job_manager.get_all_jobs(include_completed=True)
        assert len(all_jobs) == 1
        assert all_jobs[0]["request_id"] == "req1"
        assert all_jobs[0]["status"] == "completed"
    
    @pytest.mark.asyncio
    async def test_event_buffer_overflow(self, job_manager):
        """Test that queue overflow drops old events."""
        # Use small buffer for testing
        original_max = BackgroundJobManager.MAX_EVENT_BUFFER
        BackgroundJobManager.MAX_EVENT_BUFFER = 5
        
        try:
            event_count = 10
            
            async def many_events_runner():
                for i in range(event_count):
                    yield {"type": "data", "index": i}
            
            job = await job_manager.create_job(
                request_id="req1",
                user_id="user1",
                agent_name="test_agent",
                session_id=None,
                agent_runner=many_events_runner,
            )
            
            # Wait for completion
            while job.status == JobStatus.RUNNING:
                await asyncio.sleep(0.1)
            
            # Queue should have at most MAX_EVENT_BUFFER + 1 (for None signal)
            # Actually may have less due to race conditions
            assert job.event_queue.qsize() <= 6  # 5 events + None
            
        finally:
            BackgroundJobManager.MAX_EVENT_BUFFER = original_max
    
    @pytest.mark.asyncio
    async def test_cleanup_old_jobs(self, job_manager):
        """Test cleanup of old completed jobs."""
        # Set very short TTL for testing
        original_ttl = BackgroundJobManager.COMPLETED_JOB_TTL
        BackgroundJobManager.COMPLETED_JOB_TTL = 0.1  # 100ms
        
        try:
            async def quick_runner():
                yield {"type": "data"}
            
            job = await job_manager.create_job(
                request_id="req1",
                user_id="user1",
                agent_name="test_agent",
                session_id=None,
                agent_runner=quick_runner,
            )
            
            # Wait for completion
            while job.status == JobStatus.RUNNING:
                await asyncio.sleep(0.1)
            
            # Job should exist
            assert await job_manager.get_job("req1") is not None
            
            # Wait for TTL to expire
            await asyncio.sleep(0.2)
            
            # Run cleanup
            await job_manager._cleanup_old_jobs()
            
            # Job should be removed
            assert await job_manager.get_job("req1") is None
            
        finally:
            BackgroundJobManager.COMPLETED_JOB_TTL = original_ttl
    
    @pytest.mark.asyncio
    async def test_cleanup_respects_sse_clients(self, job_manager):
        """Test that cleanup doesn't remove jobs with connected clients."""
        original_ttl = BackgroundJobManager.COMPLETED_JOB_TTL
        BackgroundJobManager.COMPLETED_JOB_TTL = 0.1
        
        try:
            async def quick_runner():
                yield {"type": "data"}
            
            job = await job_manager.create_job(
                request_id="req1",
                user_id="user1",
                agent_name="test_agent",
                session_id=None,
                agent_runner=quick_runner,
            )
            
            # Wait for completion
            while job.status == JobStatus.RUNNING:
                await asyncio.sleep(0.1)
            
            # Add SSE client
            await job_manager.increment_sse_client("req1")
            
            # Wait for TTL
            await asyncio.sleep(0.2)
            
            # Run cleanup
            await job_manager._cleanup_old_jobs()
            
            # Job should still exist because of SSE client
            assert await job_manager.get_job("req1") is not None
            
        finally:
            BackgroundJobManager.COMPLETED_JOB_TTL = original_ttl
    
    def test_job_count_properties(self, job_manager):
        """Test job count properties."""
        assert job_manager.job_count == 0
        assert job_manager.active_job_count == 0
    
    @pytest.mark.asyncio
    async def test_concurrent_job_access(self, job_manager):
        """Test concurrent access to job manager."""
        async def slow_runner():
            for i in range(3):
                yield {"type": "data", "i": i}
                await asyncio.sleep(0.01)
        
        # Create multiple jobs concurrently
        jobs = await asyncio.gather(
            job_manager.create_job("req1", "user1", "agent1", None, slow_runner),
            job_manager.create_job("req2", "user2", "agent2", None, slow_runner),
            job_manager.create_job("req3", "user3", "agent3", None, slow_runner),
        )
        
        assert len(jobs) == 3
        assert job_manager.job_count == 3
        
        # Concurrent SSE client tracking
        await asyncio.gather(
            job_manager.increment_sse_client("req1"),
            job_manager.increment_sse_client("req2"),
            job_manager.increment_sse_client("req3"),
        )
        
        for job in jobs:
            assert job.sse_client_count == 1


class TestSingleton:
    """Test singleton behavior."""
    
    def test_get_background_job_manager_singleton(self, reset_singleton):
        """Test that get_background_job_manager returns singleton."""
        manager1 = get_background_job_manager()
        manager2 = get_background_job_manager()
        
        assert manager1 is manager2
    
    def test_reset_singleton(self, reset_singleton):
        """Test that reset creates new instance."""
        manager1 = get_background_job_manager()
        reset_background_job_manager()
        manager2 = get_background_job_manager()
        
        assert manager1 is not manager2


class TestCleanupLoop:
    """Test cleanup loop functionality."""
    
    @pytest.mark.asyncio
    async def test_cleanup_loop_can_be_cancelled(self, job_manager):
        """Test that cleanup loop can be cleanly cancelled."""
        task = asyncio.create_task(job_manager.cleanup_loop())
        
        await asyncio.sleep(0.1)
        task.cancel()
        
        # Cleanup loop handles cancellation gracefully and doesn't raise
        try:
            await task
        except asyncio.CancelledError:
            pass  # This is also acceptable behavior
    
    @pytest.mark.asyncio
    async def test_start_stop_cleanup_task(self, job_manager):
        """Test starting and stopping cleanup task."""
        assert job_manager._cleanup_task is None
        
        await job_manager.start_cleanup_task()
        assert job_manager._cleanup_task is not None
        
        await job_manager.stop_cleanup_task()
        assert job_manager._cleanup_task is None


class TestCancelSession:
    """Deleting a session cancels its runs that have not answered: its background jobs -- by the session they were
    started for or the one their start named -- and the requests an agent runs inline for it."""

    @pytest.mark.asyncio
    async def test_cancels_the_jobs_and_inline_requests_of_the_session_only(self, job_manager):
        from unittest.mock import MagicMock

        async def waiting_runner():
            await asyncio.sleep(30)
            yield {"type": "end"}

        async def answered_runner():
            yield {"type": "final", "summary": "done"}
            await asyncio.sleep(30)  # its save and session-end hooks
            yield {"type": "end"}

        for request_id, session_id in (("job-of-s1", "s1"), ("job-of-s2", "s2"), ("job-named-s1", None)):
            await job_manager.create_job(request_id=request_id, user_id="user1", agent_name="test_agent",
                                         session_id=session_id, agent_runner=waiting_runner)
        job_manager._jobs["job-named-s1"].actual_session_id = "s1"
        answered = await job_manager.create_job(request_id="answered-of-s1", user_id="user1", agent_name="test_agent",
                                                session_id="s1", agent_runner=answered_runner)
        final = await asyncio.wait_for(answered.event_queue.get(), timeout=2)
        assert final["type"] == "final" and answered.status == JobStatus.RUNNING, "fixture: the job is not finishing"
        inline = _agent_owning({"inline-of-s1", "inline-of-s2"})
        inline._session_tracker = MagicMock()
        inline._session_tracker.get_session_for_request = MagicMock(
            side_effect={"inline-of-s1": "s1", "inline-of-s2": "s2"}.get)
        registry = MagicMock()
        registry.list = MagicMock(return_value=["inline"])
        registry.get = MagicMock(return_value=inline)
        job_manager.set_agent_registry(registry=registry, default_agent=None)
        cancelled = []

        async def recording_cancel(request_id, force_timeout=0.0):
            cancelled.append(request_id)
            return True

        job_manager.cancel_job = recording_cancel

        assert await job_manager.cancel_session("s1") == ["inline-of-s1", "job-named-s1", "job-of-s1"]
        assert sorted(cancelled) == ["inline-of-s1", "job-named-s1", "job-of-s1"]


# --------------------------------------------------------------------------- #
# 2026-06-27 — registry-aware cancel_job
# --------------------------------------------------------------------------- #


def _agent_owning(owned_request_ids: set[str]):
    """Stand-in for agent_system.servers.agent.server.Agent that
    passes isinstance() AND exposes _request_manager + cancel_request
    so cancel_job's registry walk can drive it deterministically."""
    from unittest.mock import AsyncMock, MagicMock
    from agent_system.servers.agent.server import Agent

    agent = MagicMock(spec=Agent)
    agent._request_manager = MagicMock()
    agent._request_manager.get_active_requests = MagicMock(
        return_value=set(owned_request_ids),
    )
    agent.cancel_request = AsyncMock(return_value=True)
    return agent


class TestCancelJobRegistryWalk:
    """The 2026-06-27 cancel regression: writer-side cancel was
    setting the cancellation token + asking the DEFAULT agent only.
    Sub-agent requests (linear_book, v5b_story_designer, ...) kept
    running because the agent server that owned them never had its
    _request_manager flag flipped. cancel_job now walks the registry
    AND falls back to the default agent."""

    @pytest.mark.asyncio
    async def test_cancel_walks_registry_and_cancels_owning_agent(self):
        from unittest.mock import AsyncMock, MagicMock

        mgr = BackgroundJobManager()
        owning = _agent_owning({"req-target"})
        bystander = _agent_owning({"req-other"})
        non_agent = MagicMock()  # not Agent → must be filter-skipped

        registry = MagicMock()
        registry.list = MagicMock(
            return_value=["non_agent", "bystander", "owning"],
        )
        registry.get = MagicMock(side_effect=lambda n: {
            "non_agent": non_agent,
            "bystander": bystander,
            "owning": owning,
        }[n])
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        ok = await mgr.cancel_job("req-target")
        assert ok is True
        owning.cancel_request.assert_awaited_once_with("req-target")
        bystander.cancel_request.assert_not_called()
        # default-agent path is the fallback and must NOT have fired
        # (we found the request via the registry).
        default_agent.cancel_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_falls_back_to_default_agent_when_registry_misses(
        self,
    ):
        from unittest.mock import AsyncMock, MagicMock

        mgr = BackgroundJobManager()
        unrelated = _agent_owning({"some-other-id"})
        registry = MagicMock()
        registry.list = MagicMock(return_value=["unrelated"])
        registry.get = MagicMock(return_value=unrelated)
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=True)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        ok = await mgr.cancel_job("orphan-id")
        assert ok is True
        unrelated._request_manager.get_active_requests.assert_called_once()
        unrelated.cancel_request.assert_not_called()
        default_agent.cancel_request.assert_awaited_once_with("orphan-id")

    @pytest.mark.asyncio
    async def test_cancel_returns_false_when_nothing_owns(self):
        """Single source of truth must not lie with True if no layer
        actually acknowledged the cancel — that's the regression we
        are closing."""
        from unittest.mock import AsyncMock, MagicMock

        mgr = BackgroundJobManager()
        unrelated = _agent_owning({"x"})
        registry = MagicMock()
        registry.list = MagicMock(return_value=["unrelated"])
        registry.get = MagicMock(return_value=unrelated)
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        # Make sure no CancellationManager token exists for this id
        # so the True path can only come from an agent ack.
        ok = await mgr.cancel_job("ghost-id")
        assert ok is False

    @pytest.mark.asyncio
    async def test_cancel_survives_registry_probe_exception(self):
        """A broken Agent server (probe raises) must not abort the
        whole cancel — log and move on to the next server / default."""
        from unittest.mock import AsyncMock, MagicMock

        mgr = BackgroundJobManager()
        broken = _agent_owning({"x"})
        broken._request_manager.get_active_requests = MagicMock(
            side_effect=RuntimeError("simulated probe failure"),
        )
        healthy = _agent_owning({"target"})

        registry = MagicMock()
        registry.list = MagicMock(return_value=["broken", "healthy"])
        registry.get = MagicMock(side_effect=lambda n: {
            "broken": broken, "healthy": healthy,
        }[n])
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        ok = await mgr.cancel_job("target")
        assert ok is True
        healthy.cancel_request.assert_awaited_once_with("target")

    @pytest.mark.asyncio
    async def test_cancel_without_registry_wired_still_works(self):
        """Backward compat: if set_agent_registry was never called
        (legacy startup path / test code), cancel_job must still
        behave like the original token-only version. No crash, no
        registry walk, just the token + job-task layers."""
        mgr = BackgroundJobManager()
        # Deliberately NO set_agent_registry call.
        ok = await mgr.cancel_job("ghost-id")
        assert ok is False    # token didn't exist, no agent, no job

    @pytest.mark.asyncio
    async def test_set_agent_registry_is_idempotent(self):
        """Wiring is a single call from app.py startup; tolerate
        re-invocation (e.g. hot-reload of the FastAPI app) without
        losing state."""
        from unittest.mock import AsyncMock, MagicMock

        mgr = BackgroundJobManager()
        reg1 = MagicMock()
        reg1.list = MagicMock(return_value=[])
        agent1 = MagicMock()
        agent1.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=reg1, default_agent=agent1)

        reg2 = MagicMock()
        reg2.list = MagicMock(return_value=[])
        agent2 = MagicMock()
        agent2.cancel_request = AsyncMock(return_value=True)
        # Second call overrides — that's the contract.
        mgr.set_agent_registry(registry=reg2, default_agent=agent2)

        await mgr.cancel_job("test-rid")
        agent1.cancel_request.assert_not_called()
        agent2.cancel_request.assert_awaited_once_with("test-rid")


class TestCancelJobIntegrationWithCancellationManager:
    """Self-review follow-up: the standalone TestCancelJobRegistryWalk
    cases mock the cancellation manager away. These exercise the REAL
    CancellationManager singleton + the registry walk together so the
    ``token_cancelled or agent_cancelled or job_exists`` return-value
    composition is provably correct under realistic state."""

    def _isolate_cancellation_manager(self, monkeypatch):
        """Replace the singleton with a fresh instance so the test
        starts with an empty token store and other tests can't pollute
        what we see."""
        from agent_system.core import cancellation as cm
        fresh = cm.CancellationManager()
        monkeypatch.setattr(cm, "_cancellation_manager", fresh)
        return fresh

    @pytest.mark.asyncio
    async def test_token_and_registry_walk_both_fire_and_return_true(
        self, monkeypatch,
    ):
        """The realistic production case: cancel arrives, the token
        store has a token for the request_id AND a registered agent
        owns it. Both layers must fire — the agent's per-request flag
        flips (registry walk) AND the prefix-token gets set (so
        sub-requests cancel too). Return True."""
        from unittest.mock import AsyncMock, MagicMock

        cm = self._isolate_cancellation_manager(monkeypatch)
        # Pre-create a token as if /run had registered one for this rid.
        cm.create_token("req-real")
        # And one prefix-matched sub-request — must also get cancelled.
        cm.create_token("req-real_sub_xyz")

        mgr = BackgroundJobManager()
        owning = _agent_owning({"req-real"})
        registry = MagicMock()
        registry.list = MagicMock(return_value=["owning"])
        registry.get = MagicMock(return_value=owning)
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        ok = await mgr.cancel_job("req-real")
        assert ok is True

        # Token layer: parent + sub both flipped to cancelled.
        assert cm.get_token("req-real").is_cancelled
        assert cm.get_token("req-real_sub_xyz").is_cancelled
        # Registry layer: owning server got the call.
        owning.cancel_request.assert_awaited_once_with("req-real")

    @pytest.mark.asyncio
    async def test_token_only_no_agent_returns_true(self, monkeypatch):
        """A request that has a registered token but no Agent server
        owns it (rare: token registered, agent died, default agent
        also doesn't know about it). Token-only success path must
        STILL return True — the operator's button feels correct."""
        from unittest.mock import AsyncMock, MagicMock

        cm = self._isolate_cancellation_manager(monkeypatch)
        cm.create_token("orphan")

        mgr = BackgroundJobManager()
        unrelated = _agent_owning({"someone-else"})
        registry = MagicMock()
        registry.list = MagicMock(return_value=["unrelated"])
        registry.get = MagicMock(return_value=unrelated)
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        ok = await mgr.cancel_job("orphan")
        assert ok is True
        assert cm.get_token("orphan").is_cancelled
        unrelated.cancel_request.assert_not_called()
        default_agent.cancel_request.assert_awaited_once_with("orphan")

    @pytest.mark.asyncio
    async def test_no_token_no_agent_no_job_returns_false(
        self, monkeypatch,
    ):
        """The only path that must return False — every layer
        genuinely had nothing to cancel. Closes the regression
        precisely: pre-fix the endpoint lied with True even here."""
        from unittest.mock import AsyncMock, MagicMock

        self._isolate_cancellation_manager(monkeypatch)
        # No create_token call — token store is empty.

        mgr = BackgroundJobManager()
        unrelated = _agent_owning({"someone-else"})
        registry = MagicMock()
        registry.list = MagicMock(return_value=["unrelated"])
        registry.get = MagicMock(return_value=unrelated)
        default_agent = MagicMock()
        default_agent.cancel_request = AsyncMock(return_value=False)
        mgr.set_agent_registry(registry=registry, default_agent=default_agent)

        ok = await mgr.cancel_job("never-existed")
        assert ok is False
