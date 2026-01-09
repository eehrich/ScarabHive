"""Background Job Manager for decoupling agent execution from SSE streams.

This module provides infrastructure for running agent jobs in the background,
allowing:
- Agent continues running even if browser disconnects
- Browser can reconnect and resume receiving events
- Robust operation for multi-hour/day batch jobs
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class JobStatus(Enum):
    """Status of a background job."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BackgroundJob:
    """Represents an agent job running in the background."""
    request_id: str
    user_id: str
    agent_name: str
    session_id: Optional[str]
    task: asyncio.Task[Any]
    event_queue: asyncio.Queue[Any]
    status: JobStatus = JobStatus.RUNNING
    result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    # Track connected SSE clients (for cleanup decisions)
    sse_client_count: int = 0
    # Track actual session_id (may be set after start event)
    actual_session_id: Optional[str] = None
    # Last known status message for reconnecting clients
    last_status_message: Optional[str] = None
    # Task description for display
    task_description: Optional[str] = None
    # LLM profile used for this job
    llm_profile: Optional[str] = None


class BackgroundJobManager:
    """Manages background agent jobs independently of SSE connections.
    
    This manager allows agent execution to continue even when SSE connections
    are interrupted. Key features:
    
    - Jobs run in background asyncio tasks
    - Events are buffered in queues for client consumption
    - Multiple SSE clients can attach to the same job
    - Old completed jobs are automatically cleaned up
    
    Usage:
        manager = BackgroundJobManager()
        
        # Create a job
        job = await manager.create_job(
            request_id="abc123",
            user_id="user1",
            agent_name="my_agent",
            session_id="session1",
            agent_runner=my_async_generator
        )
        
        # Read events from job.event_queue
        while True:
            event = await job.event_queue.get()
            if event is None:
                break
            process(event)
    """
    
    # Maximum events to buffer per job (prevents memory leak)
    MAX_EVENT_BUFFER = 1000
    # How long to keep completed jobs before cleanup (seconds)
    COMPLETED_JOB_TTL = 300  # 5 minutes
    
    def __init__(self) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task[None]] = None
    
    async def start_cleanup_task(self) -> None:
        """Start periodic cleanup of completed jobs."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop_impl())
    
    async def stop_cleanup_task(self) -> None:
        """Stop cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
    
    async def cleanup_loop(self) -> None:
        """Periodically clean up old completed jobs. Public API for starting cleanup."""
        await self._cleanup_loop_impl()
    
    async def _cleanup_loop_impl(self) -> None:
        """Internal cleanup loop implementation."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._cleanup_old_jobs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in job cleanup: {e}")
    
    async def _cleanup_old_jobs(self) -> None:
        """Remove completed jobs older than TTL."""
        now = time.time()
        async with self._lock:
            to_remove = []
            for request_id, job in self._jobs.items():
                if job.status != JobStatus.RUNNING:
                    if job.completed_at and (now - job.completed_at) > self.COMPLETED_JOB_TTL:
                        # Only remove if no SSE clients connected
                        if job.sse_client_count == 0:
                            to_remove.append(request_id)
            
            for request_id in to_remove:
                del self._jobs[request_id]
                logger.debug(f"[JOB_CLEANUP] Removed completed job {request_id}")
    
    async def create_job(
        self,
        request_id: str,
        user_id: str,
        agent_name: str,
        session_id: Optional[str],
        agent_runner: Callable[[], Any],  # async generator function
        llm_profile: Optional[str] = None,
    ) -> BackgroundJob:
        """Create and start a new background job.
        
        Args:
            request_id: Unique identifier for this request
            user_id: User who initiated the request
            agent_name: Name of the agent to run
            session_id: Optional session ID for continuity
            agent_runner: Async generator function that yields events
            llm_profile: Optional LLM profile override
            
        Returns:
            BackgroundJob instance with task and event_queue
        """
        event_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.MAX_EVENT_BUFFER)
        
        async def job_wrapper() -> None:
            """Wrapper that runs the agent and captures events/errors."""
            job = self._jobs.get(request_id)
            if not job:
                return
            
            try:
                async for event in agent_runner():
                    # Put event in queue (non-blocking, drop old if full)
                    try:
                        event_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        # Queue full - drop oldest event and add new one
                        try:
                            event_queue.get_nowait()
                            event_queue.put_nowait(event)
                        except asyncio.QueueEmpty:
                            pass
                
                # Mark as completed
                async with self._lock:
                    if job := self._jobs.get(request_id):
                        job.status = JobStatus.COMPLETED
                        job.completed_at = time.time()
                        logger.info(f"[BACKGROUND_JOB] Job {request_id} completed successfully")
                
            except asyncio.CancelledError:
                async with self._lock:
                    if job := self._jobs.get(request_id):
                        job.status = JobStatus.CANCELLED
                        job.completed_at = time.time()
                        logger.info(f"[BACKGROUND_JOB] Job {request_id} was cancelled")
                raise
            except Exception as e:
                async with self._lock:
                    if job := self._jobs.get(request_id):
                        job.status = JobStatus.FAILED
                        job.error_message = str(e)
                        job.completed_at = time.time()
                        logger.error(f"[BACKGROUND_JOB] Job {request_id} failed: {e}", exc_info=True)
            finally:
                # Signal end to any waiting consumers
                try:
                    event_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        
        task = asyncio.create_task(job_wrapper(), name=f"background_job_{request_id}")
        
        job = BackgroundJob(
            request_id=request_id,
            user_id=user_id,
            agent_name=agent_name,
            session_id=session_id,
            task=task,
            event_queue=event_queue,
            llm_profile=llm_profile,
        )
        
        async with self._lock:
            self._jobs[request_id] = job
        
        logger.info(f"[BACKGROUND_JOB] Started job {request_id} for agent {agent_name}")
        return job
    
    async def get_job(self, request_id: str) -> Optional[BackgroundJob]:
        """Get a job by request_id."""
        async with self._lock:
            return self._jobs.get(request_id)
    
    async def cancel_job(self, request_id: str) -> bool:
        """Cancel a running job.
        
        Returns:
            True if job was found and cancellation initiated, False otherwise
        """
        async with self._lock:
            job = self._jobs.get(request_id)
            if job and job.status == JobStatus.RUNNING:
                job.task.cancel()
                return True
        return False
    
    async def get_active_jobs(self, user_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Get list of active jobs, optionally filtered by user.
        
        Args:
            user_id: If provided, only return jobs for this user
            
        Returns:
            List of job info dictionaries
        """
        async with self._lock:
            jobs = [
                {
                    "request_id": job.request_id,
                    "user_id": job.user_id,
                    "agent_name": job.agent_name,
                    "session_id": job.session_id,
                    "status": job.status.value,
                    "created_at": job.created_at,
                    "sse_clients": job.sse_client_count,
                }
                for job in self._jobs.values()
                if job.status == JobStatus.RUNNING
            ]
            
            if user_id:
                jobs = [j for j in jobs if j["user_id"] == user_id]
            
            return jobs
    
    async def get_all_jobs(self, include_completed: bool = False) -> list[dict[str, Any]]:
        """Get all jobs info.
        
        Args:
            include_completed: If True, include completed/failed/cancelled jobs
            
        Returns:
            List of job info dictionaries
        """
        async with self._lock:
            jobs = []
            for job in self._jobs.values():
                if not include_completed and job.status != JobStatus.RUNNING:
                    continue
                jobs.append({
                    "request_id": job.request_id,
                    "user_id": job.user_id,
                    "agent_name": job.agent_name,
                    "session_id": job.session_id,
                    "status": job.status.value,
                    "created_at": job.created_at,
                    "completed_at": job.completed_at,
                    "error": job.error_message,
                    "sse_clients": job.sse_client_count,
                    "events_buffered": job.event_queue.qsize(),
                })
            return jobs
    
    async def increment_sse_client(self, request_id: str) -> None:
        """Track SSE client connection."""
        async with self._lock:
            if job := self._jobs.get(request_id):
                job.sse_client_count += 1
                logger.debug(f"[SSE_CLIENT] {request_id} clients: {job.sse_client_count}")
    
    async def decrement_sse_client(self, request_id: str) -> None:
        """Track SSE client disconnection."""
        async with self._lock:
            if job := self._jobs.get(request_id):
                job.sse_client_count = max(0, job.sse_client_count - 1)
                logger.debug(f"[SSE_CLIENT] {request_id} clients: {job.sse_client_count}")
    
    @property
    def job_count(self) -> int:
        """Return total number of jobs (including completed)."""
        return len(self._jobs)
    
    @property
    def active_job_count(self) -> int:
        """Return number of running jobs."""
        return sum(1 for j in self._jobs.values() if j.status == JobStatus.RUNNING)


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_background_job_manager: Optional[BackgroundJobManager] = None


def get_background_job_manager() -> BackgroundJobManager:
    """Get or create the BackgroundJobManager singleton instance.
    
    This function is thread-safe for the initial creation and ensures
    only one instance exists globally.
    """
    global _background_job_manager
    if _background_job_manager is None:
        _background_job_manager = BackgroundJobManager()
    return _background_job_manager


def reset_background_job_manager() -> None:
    """Reset the singleton instance (mainly for testing)."""
    global _background_job_manager
    _background_job_manager = None
