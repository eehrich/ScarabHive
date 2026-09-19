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

# The events that end a run's answer, as the chat reads them too: after them the run only finishes. An `error` is
# none, as in the chat, which keeps such a run stoppable until its end.
ANSWER_EVENTS = ("final", "cancelled")


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
    # The run has sent its answer (ANSWER_EVENTS) and only finishes now: saves, session-end hooks
    answered: bool = False
    # How many items the run has handed to the queue, counted from the first -- its events, and
    # the end marker as the last. NOT the queue's length: the buffer drops its oldest when it
    # fills, and this keeps counting. It is what lets a client that has already seen the run's
    # first N events say so, so the reconnect can skip exactly those and no more.
    events_emitted: int = 0

    def catch_up_skip(self, seen: int) -> int:
        """How many of the buffered items a client that already holds the run's first ``seen``
        events may be sent past.

        The buffer keeps the LAST ``qsize`` of the ``events_emitted`` items, the oldest having
        been pushed out as it filled, so its first entry is item number
        ``events_emitted - qsize + 1`` and the client wants everything after number ``seen``.

        Both ends are held: never more than the buffer holds, and never less than nothing. A
        client that has seen nothing (``seen`` 0, or a reconnect that names no number) skips
        nothing, which is the replay this reconnect did before it could be told; one that has
        seen everything the run has sent skips the buffer whole. And one the buffer has
        OUTRUN -- its oldest pushed out past what the client had seen -- skips nothing: none
        of what is left is on its screen. Its gap stays, and only its next session load
        closes it; skipping here would widen it.

        Read before the reconnect answer goes out, not after: answering hands control back to
        the event loop, and what the run puts in the queue meanwhile has not been seen.
        """
        buffered = self.event_queue.qsize()
        first_buffered = self.events_emitted - buffered + 1
        return max(0, min(buffered, seen - first_buffered + 1))


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
        """Wire the tool registry + default agent into cancel_job.

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
                    if own_job is not None and isinstance(event, dict) and event.get("type") in ANSWER_EVENTS:
                        own_job.answered = True
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
                    # Counted whether or not it had to push an older one out: the number
                    # says which event this was, not how many are still waiting.
                    if own_job is not None:
                        own_job.events_emitted += 1
                
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
                    # Counted like an event although it is not one: what reads the count
                    # against the queue's length (the reconnect's catch-up) needs the two
                    # to mean the same items, and the marker occupies a place in the queue.
                    # No client ever reports having seen it -- it only arrives once the run
                    # is over, and then there is nothing left to catch up on.
                    if own_job is not None:
                        own_job.events_emitted += 1
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

        2. **Agent-server cancel** — walk the registered tool servers
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

    async def cancel_session(self, session_id: str) -> list[str]:
        """Cancel every run of ``session_id`` in this process that has not answered yet -- its background jobs, and
        the requests an agent runs for it inline (a /run with files is no job) -- and return their request ids.

        For a deleted session: those runs would answer for nothing, since the session manager writes a deleted
        session no more. A run past its answer is not cancelled: it only finishes, and a cancel would take its
        background sub-agents along (their cancellation tokens share the run's id as a prefix) -- as the chat
        spares it when the viewer leaves. Graceful cancels only, so the delete answers at once; each run ends at
        its next step.
        """
        async with self._lock:
            request_ids = {
                job.request_id for job in self._jobs.values()
                if job.status == JobStatus.RUNNING and not job.answered
                and session_id in (job.session_id, job.actual_session_id)
            }
        for server in self._agent_servers():
            try:
                active = server._request_manager.get_active_requests()
            except Exception:  # noqa: BLE001 -- an agent without a request manager runs nothing here
                continue
            request_ids.update(
                request_id for request_id in active
                if server._session_tracker.get_session_for_request(request_id) == session_id
            )
        for request_id in request_ids:
            await self.cancel_job(request_id)
        return sorted(request_ids)

    def _agent_servers(self):
        """The Agent servers of this process: every registered one, then the default agent."""
        try:
            from agent_system.servers.agent.server import Agent as _Agent
        except Exception:  # noqa: BLE001
            _Agent = None  # type: ignore[assignment]
        if _Agent is not None and self._agent_registry is not None:
            try:
                names = self._agent_registry.list()
            except Exception:  # noqa: BLE001
                names = []
            for name in names:
                try:
                    server = self._agent_registry.get(name)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(server, _Agent):
                    yield server
        if self._default_agent is not None:
            yield self._default_agent

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

    async def active_sessions(self) -> dict[str, dict[str, Any]]:
        """Which sessions this process is running right now: session_id -> info.

        Two sources, because neither sees everything:

        * The background jobs. That is what the web UI starts, and the source
          that always knows whose run it is. A job's session is read under both
          names -- ``session_id`` is empty for a session the run itself creates,
          and only ``actual_session_id`` names it once the start event has. The
          same pair ``cancel_session`` matches on; matching one of them alone
          makes a fresh session look idle for its whole first turn.
        * The agent servers' session locks. A ``/run`` that carries files is no
          job, and a sub-agent's session never was one -- those show up here and
          nowhere else. The owner comes from the same tracker's session metadata,
          which every run writes (the API layer, agent-cli, the sub-agent manager
          all set it); it stays None where the run reached the tracker without a
          user, and then the caller has to decide from somewhere else.

        Jobs win on conflict, for the user_id. The result is process-internal
        truth and says nothing about who may SEE a session -- the caller filters.
        """
        async with self._lock:
            running = [job for job in self._jobs.values() if job.status == JobStatus.RUNNING]
        active: dict[str, dict[str, Any]] = {}
        for server in self._agent_servers():
            tracker = getattr(server, "_session_tracker", None)
            try:
                owners = tracker.active_sessions()
            except Exception as err:  # noqa: BLE001 -- one agent must not cost the others their runs
                # Nothing known holds a tracker back today, so this is a guard, not a
                # path. Logged rather than passed over: unanswered, it shows up as an
                # agent whose runs quietly stop being marked anywhere.
                logger.debug("[ACTIVE_SESSIONS] %s has no readable session tracker: %s",
                             getattr(server, "name", server), err, exc_info=True)
                continue
            for session_id, request_id in owners.items():
                if session_id:
                    meta = tracker.get_session_metadata(session_id) or {}
                    active[session_id] = {
                        "request_id": request_id,
                        "agent_name": getattr(server, "name", None),
                        "user_id": meta.get("user_id"),
                        "attachable": False,
                        "answered": False,
                    }
        for job in running:
            for session_id in (job.session_id, job.actual_session_id):
                if session_id:
                    active[session_id] = {
                        "request_id": job.request_id,
                        "agent_name": job.agent_name,
                        "user_id": job.user_id,
                        # Only a job can be reconnected to: /events finds it by id and
                        # streams its buffer. A run without one answers 409 there.
                        "attachable": True,
                        # Past its answer, only finishing. ``cancel_session`` spares those
                        # on purpose -- cancelling one takes its background sub-agents with
                        # it -- so whoever cancels from this answer has to spare them too.
                        "answered": job.answered,
                    }
        return active

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
