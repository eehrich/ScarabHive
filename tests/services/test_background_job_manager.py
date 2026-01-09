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
    async def test_cancel_job(self, job_manager):
        """Test cancelling a running job."""
        cancel_event = asyncio.Event()
        
        async def slow_runner():
            yield {"type": "start"}
            try:
                await asyncio.sleep(10)  # Long running
                yield {"type": "end"}
            except asyncio.CancelledError:
                cancel_event.set()
                raise
        
        job = await job_manager.create_job(
            request_id="req1",
            user_id="user1",
            agent_name="test_agent",
            session_id=None,
            agent_runner=slow_runner,
        )
        
        # Wait for job to start
        await asyncio.sleep(0.1)
        assert job.status == JobStatus.RUNNING
        
        # Cancel the job
        result = await job_manager.cancel_job("req1")
        assert result is True
        
        # Wait for cancellation
        await asyncio.sleep(0.2)
        assert job.status == JobStatus.CANCELLED
    
    @pytest.mark.asyncio
    async def test_cancel_nonexistent_job(self, job_manager):
        """Test cancelling a job that doesn't exist."""
        result = await job_manager.cancel_job("nonexistent")
        assert result is False
    
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
