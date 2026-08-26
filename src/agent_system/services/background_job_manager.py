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

from agent_system.core.cancellation import get_cancellation_manager

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class DuplicateRequestIdError(RuntimeError):
    """A job is already RUNNING under this request_id.

    Raised by ``create_job`` instead of displacing the live run. Callers
    should surface it rather than starting a second agent: retrying the
    same request_id lands on the reconnect path once the caller sees it.
    """

    def __init__(self, request_id: str) -> None:
        super().__init__(
            f"request_id {request_id} is already running — refusing to "
            "start a second job under the same id"
        )
        self.request_id = request_id


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
        # Optional registry of MCP-registered agent servers. cancel_job
        # walks it so a request that lives on a sub-agent (e.g.
        # linear_book, v5b_story_designer) is cancelled on the SERVER
        # that actually owns it, not just the default agent. Without
        # this the agent's per-server _request_manager flag never
        # flips and the agent keeps running until completion despite
        # the cancellation token being set — the 2026-06-27 cancel
        # regression. Wired by app.py after the registry is built.
        self._agent_registry: Any = None
        # Optional default agent. Belt-and-suspenders for callers that
        # bind requests to the global default agent (chat_agent on
        # most deploys) without going through the registry.
        self._default_agent: Any = None

    def set_agent_registry(self, registry: Any, default_agent: Any) -> None:
        """Wire the MCP registry + default agent into cancel_job.

        Idempotent. Called once at startup from app.py after the
        registry is fully populated. The setter pattern keeps the
        services layer free of an upstream import of the FastAPI
        app module.
        """
        self._agent_registry = registry
        self._default_agent = default_agent
    
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

        # The job object THIS wrapper owns, assigned below under the lock.
        # The wrapper must never write through ``self._jobs[request_id]``:
        # should the dict ever hold a different job for this id, writing
        # through it would flip a foreign, still-running job to COMPLETED —
        # a lying status on a live run. (``is not None`` is for the type
        # checker: the assignment happens before the task can first run.)
        own_job: Optional[BackgroundJob] = None

        async def job_wrapper() -> None:
            """Wrapper that runs the agent and captures events/errors."""
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
                    if own_job is not None:
                        own_job.status = JobStatus.COMPLETED
                        own_job.completed_at = time.time()
                        logger.info(f"[BACKGROUND_JOB] Job {request_id} completed successfully")

            except asyncio.CancelledError:
                async with self._lock:
                    if own_job is not None:
                        own_job.status = JobStatus.CANCELLED
                        own_job.completed_at = time.time()
                        logger.info(f"[BACKGROUND_JOB] Job {request_id} was cancelled")
                raise
            except Exception as e:
                async with self._lock:
                    if own_job is not None:
                        own_job.status = JobStatus.FAILED
                        own_job.error_message = str(e)
                        own_job.completed_at = time.time()
                        logger.error(f"[BACKGROUND_JOB] Job {request_id} failed: {e}", exc_info=True)
            finally:
                # Signal end to any waiting consumers
                try:
                    event_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        
        # Check-and-register in ONE lock block. The callers' own duplicate
        # guard (app.py's _validate_client_request_id) is a check-then-act
        # with a wide window — it runs in the request handler while
        # create_job only runs once the SSE body is being streamed — so two
        # concurrent requests carrying the same caller-supplied request_id
        # both passed it and both started a full agent run. Registering
        # blindly then left the older run alive but unreachable by id: no
        # reconnect, and cancel_job would cancel the younger run instead.
        # asyncio.create_task does not await, so the whole sequence stays
        # atomic against other coroutines.
        async with self._lock:
            existing = self._jobs.get(request_id)
            if existing is not None and existing.status == JobStatus.RUNNING:
                raise DuplicateRequestIdError(request_id)

            task = asyncio.create_task(
                job_wrapper(), name=f"background_job_{request_id}",
            )
            job = BackgroundJob(
                request_id=request_id,
                user_id=user_id,
                agent_name=agent_name,
                session_id=session_id,
                task=task,
                event_queue=event_queue,
                llm_profile=llm_profile,
            )
            own_job = job
            self._jobs[request_id] = job

        logger.info(f"[BACKGROUND_JOB] Started job {request_id} for agent {agent_name}")
        return job
    
    async def get_job(self, request_id: str) -> Optional[BackgroundJob]:
        """Get a job by request_id."""
        async with self._lock:
            return self._jobs.get(request_id)
    
    async def cancel_job(self, request_id: str, force_timeout: float = 0.0) -> bool:
        """Cancel a running job gracefully across all layers.

        Cancellation has three independent layers, each addressing a
        different leak path observed in production:

        1. ``CancellationManager`` token (prefix-matched so sub-agent
           requests like ``parent_sub_xxx`` get cancelled too). Agents
           check ``is_cancelled()`` at every LLM-result handler / tool
           dispatch / sub-agent join and shut down cleanly.

        2. **Agent-server cancel** — walk the registered MCP servers
           and call ``cancel_request(request_id)`` on the one that
           actually owns the request. Without this the
           per-server ``_request_manager`` flag never flips, so the
           agent's own loop never sees the cancellation even after
           the token is set (2026-06-27 regression: ``/api/requests/
           {rid}/cancel`` had been calling cancel only on the
           DEFAULT global agent, missing every linear_book /
           v5b_story_designer / cover_artist request). The default
           agent is then tried as belt-and-suspenders for caller paths
           that bypassed the registry.

        3. Optional ``force_timeout`` — if the agent is mid-blocking-
           I/O (``asyncio.to_thread`` around httpx), the token + flag
           don't end the asyncio task until the I/O returns. The
           timeout then triggers ``Task.cancel()`` so the next await
           point unwinds.

        Args:
            request_id: The request ID to cancel
            force_timeout: If > 0, force-cancel task after this many seconds if still running

        Returns:
            True if cancellation was applied somewhere — token set,
            registered agent acknowledged, default agent acknowledged,
            or the background job task was found. False only when
            none of those layers matched (the request really wasn't
            tracked).
        """
        # 1. Token cancellation (existing behaviour, prefix-matched).
        cancellation_manager = get_cancellation_manager()
        token_cancelled = cancellation_manager.cancel_request(request_id)
        if token_cancelled:
            logger.info(
                "[BACKGROUND_JOB] Set cancellation token for %s "
                "(including sub-requests)", request_id,
            )

        # 2. Agent-server cancel via registry walk.
        agent_cancelled = await self._cancel_on_owning_agent(request_id)

        # Job-task lookup — used to gate force_timeout AND so we can
        # report 'something matched' even if only the BackgroundJob
        # task exists (no token / no agent ack).
        async with self._lock:
            job = self._jobs.get(request_id)
            job_exists = job is not None and job.status == JobStatus.RUNNING

        # 3. Force-cancel after grace.
        if job_exists and force_timeout > 0:
            logger.info(
                "[BACKGROUND_JOB] Waiting %ss for graceful shutdown of %s",
                force_timeout, request_id,
            )
            await asyncio.sleep(force_timeout)
            async with self._lock:
                job = self._jobs.get(request_id)
                if job and job.status == JobStatus.RUNNING:
                    logger.warning(
                        "[BACKGROUND_JOB] Force-cancelling task for %s "
                        "after timeout", request_id,
                    )
                    job.task.cancel()

        return token_cancelled or agent_cancelled or job_exists

    async def _cancel_on_owning_agent(self, request_id: str) -> bool:
        """Find the Agent server that owns `request_id` and call its
        cancel_request. Falls back to the default agent if no
        registered server matches.

        Returns True if any agent acknowledged the cancel. Pure
        method on the manager — no FastAPI imports — so the
        services layer stays clean of upstream dependencies.
        """
        # The Agent class is imported lazily so importing this module
        # before the agent server is built doesn't trigger a circular
        # import (services → servers → ... → services).
        try:
            from agent_system.servers.agent.server import Agent as _Agent
        except Exception:  # noqa: BLE001
            logger.debug(
                "[BACKGROUND_JOB] Agent class import failed during "
                "cancel — registry walk skipped",
            )
            return False

        # Walk the registry first — that's where sub-agent servers
        # (linear_book, v5b_*, cover_artist, ...) register themselves.
        if self._agent_registry is not None:
            try:
                names = self._agent_registry.list()
            except Exception:  # noqa: BLE001
                names = []
            for name in names:
                try:
                    srv = self._agent_registry.get(name)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(srv, _Agent):
                    continue
                try:
                    active = srv._request_manager.get_active_requests()
                except Exception:  # noqa: BLE001
                    # Older Agent without _request_manager — skip.
                    continue
                if request_id not in active:
                    continue
                try:
                    ok = await srv.cancel_request(request_id)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[BACKGROUND_JOB] cancel_request on agent=%s "
                        "raised — continuing", name,
                    )
                    continue
                if ok:
                    logger.info(
                        "[BACKGROUND_JOB] Cancel propagated via "
                        "registry to agent=%s for request_id=%s",
                        name, request_id,
                    )
                    return True

        # Belt-and-suspenders: try the default agent. Idempotent for
        # callers that wire requests directly to it.
        if self._default_agent is not None:
            try:
                return bool(
                    await self._default_agent.cancel_request(request_id)
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[BACKGROUND_JOB] default-agent cancel raised — "
                    "treating as not-found",
                )
        return False

    async def is_request_active_anywhere(self, request_id: str) -> bool:
        """True when ``request_id`` is live on ANY tracked surface.

        Mirrors ``_cancel_on_owning_agent``'s registry walk for the
        STATUS side. The ``/api/requests/{rid}/status`` endpoint's
        session-tracker fallback used to consult only the DEFAULT
        agent's tracker — the exact per-agent blind spot the
        2026-06-27 cancel regression had on the cancel side: a
        ``/run?agent_name=linear_book`` request registers in
        linear_book's ``_request_manager``, so the default agent's
        tracker reports it inactive and the endpoint answered
        ``unknown/no_active_run`` for a run that was actively
        grinding. The writer-side reconcile pass consumed that as
        "BackgroundJob gone" and re-queued the job for resume —
        double-running a multi-hour book_generation.

        Checks, in order:
          1. BackgroundJobManager's own jobs (RUNNING only),
          2. every registry-registered Agent server's request manager,
          3. the default agent's request manager.
        """
        async with self._lock:
            job = self._jobs.get(request_id)
            if job is not None and job.status == JobStatus.RUNNING:
                return True

        try:
            from agent_system.servers.agent.server import Agent as _Agent
        except Exception:  # noqa: BLE001
            logger.debug(
                "[BACKGROUND_JOB] Agent class import failed during "
                "status walk — registry walk skipped",
            )
            _Agent = None  # type: ignore[assignment]

        if _Agent is not None and self._agent_registry is not None:
            try:
                names = self._agent_registry.list()
            except Exception:  # noqa: BLE001
                names = []
            for name in names:
                try:
                    srv = self._agent_registry.get(name)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(srv, _Agent):
                    continue
                try:
                    active = srv._request_manager.get_active_requests()
                except Exception:  # noqa: BLE001
                    continue
                if request_id in active:
                    return True

        if self._default_agent is not None:
            try:
                active = self._default_agent._request_manager.get_active_requests()
                if request_id in active:
                    return True
            except Exception:  # noqa: BLE001
                pass
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
