"""Background Job Manager for decoupling agent execution from SSE streams.

This module provides infrastructure for running agent jobs in the background,
allowing:
- Agent continues running even if browser disconnects
- Browser can reconnect and resume receiving events
- Robust operation for multi-hour/day batch jobs
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Optional

from agent_system.core.cancellation import get_cancellation_manager

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# The events that end a run's answer, as the chat reads them too: after them the run only finishes. An `error` is
# none, as in the chat, which keeps such a run stoppable until its end.
ANSWER_EVENTS = ("final", "cancelled")


def _answer_delta(event: Any) -> Optional[dict[str, Any]]:
    """The answer delta ``event`` is, or carries as a sub-run's ``sub_run`` envelope; else None."""
    if not isinstance(event, dict):
        return None
    inner = event.get("event") if event.get("type") == "sub_run" else event
    return inner if isinstance(inner, dict) and inner.get("type") == "thinking_delta" else None


def _keep_only_the_end(events: deque[Any]) -> None:
    """Drop every buffered event before the run's last answer -- or, without one, its last error."""
    def last(kinds: tuple[str, ...]) -> int:
        return max((i for i, e in enumerate(events) if isinstance(e, dict) and e.get("type") in kinds),
                   default=-1)

    end = last(ANSWER_EVENTS)
    if end < 0:
        end = last(("error",))
    for _ in range(end if end >= 0 else len(events)):
        events.popleft()


def _without_answer_so_far(event: dict[str, Any]) -> dict[str, Any]:
    """A copy of an answer delta, or of its sub_run envelope, without ``accumulated``."""
    if event.get("type") == "sub_run":
        return {**event, "event": _without_answer_so_far(event["event"])}
    return {key: value for key, value in event.items() if key != "accumulated"}


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
    # The run's events, the newest of them (create_job bounds it), for every reader alike:
    # each reads from its own place (``follow``) and nobody takes anything out. A queue
    # handed each event to ONE reader -- a second tab, or the chat beside writer_jobs' own
    # stream, took every other event, the answer among them.
    events: deque[Any] = field(default_factory=deque)
    status: JobStatus = JobStatus.RUNNING
    result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    # Track connected SSE clients (for cleanup decisions)
    sse_client_count: int = 0
    # Since when no stream reads the job (time.monotonic()); None while one does.
    # A reload drops the count to 0 for a moment -- a check that someone still
    # watches (tool_approval) allows for that instead of taking it for a closed tab.
    unread_since: Optional[float] = field(default_factory=time.monotonic)
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
    # A mirror of a run someone else collects (POST /run): pages follow it like any job, but
    # the answer went to that caller, not through here -- so once it is over, the status
    # endpoint does not offer it as proof that the run finished (writer_jobs' reconcile
    # takes a finished job's status as "done" and would bury a book run it should resume).
    mirror: bool = False
    # How many events the run has sent, counted from the first. NOT the buffer's length: the
    # buffer drops its oldest when it fills, and this keeps counting. Event number n (from 0)
    # is the one a reader at ``n`` gets next -- which is how a client that has already seen
    # the run's first N events says so, and is sent exactly the rest.
    events_emitted: int = 0
    # Woken whenever an event arrives or the run ends.
    changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    def events_from(self, cursor: int) -> tuple[list[Any], int]:
        """The buffered events from number ``cursor`` on, and the number to read on from.

        What the buffer has dropped is gone: a reader that far behind starts at the oldest it
        still holds, and its gap stays -- only its next session load closes it. A cursor past
        the run's last event comes back as that event's number.
        """
        first_buffered = self.events_emitted - len(self.events)
        skip = max(0, cursor - first_buffered)
        return list(itertools.islice(self.events, skip, None)), self.events_emitted

    async def follow(self, cursor: int = 0, keepalive: float = 10.0) -> AsyncIterator[Any]:
        """Every event from number ``cursor`` on as the run sends it, until the run has ended.

        None each time ``keepalive`` seconds pass without one: the stream's cue to say it is
        still there, as a proxy closes a line that stays quiet.
        """
        while True:
            events, cursor = self.events_from(cursor)
            for event in events:
                yield event
            if events:
                continue
            if self.status != JobStatus.RUNNING:
                return
            async with self.changed:
                try:
                    await asyncio.wait_for(
                        self.changed.wait_for(
                            lambda: self.events_emitted > cursor or self.status != JobStatus.RUNNING),
                        keepalive)
                    continue
                except asyncio.TimeoutError:
                    pass
            yield None


class BackgroundJobManager:
    """Manages background agent jobs independently of SSE connections.
    
    This manager allows agent execution to continue even when SSE connections
    are interrupted. Key features:
    
    - Jobs run in background asyncio tasks
    - Events are buffered per job, and every reader gets all of them
    - Multiple SSE clients can attach to the same job
    - Finished jobs let go of their buffer but how the run ended (start_cleanup_task, from the app's lifespan)
    
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
        
        # Read its events, from the first, until the run ends
        async for event in job.follow():
            if event is not None:   # None: nothing for a while
                process(event)
    """
    
    # Maximum events to buffer per job (prevents memory leak)
    MAX_EVENT_BUFFER = 1000
    # How long a finished job keeps all of its buffered events (seconds); see _cleanup_old_jobs
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
        """Finished jobs past COMPLETED_JOB_TTL keep only how their run ended.

        The job itself stays until the process ends: writer_jobs asks for it long after.
        A story_design retry reconnects under the same id and reads the run's answer, and
        reconcile takes a stale run's status from it -- with the job gone, the retry
        started the whole run again. What is let go is the rest of its event buffer.
        ponytail: records are kept for good, each with its task and its final answer; drop
        them by age once their number matters, but not before writer_jobs' longest retry
        backoff.
        """
        now = time.time()
        async with self._lock:
            for job in self._jobs.values():
                if (job.status != JobStatus.RUNNING and job.completed_at
                        and now - job.completed_at > self.COMPLETED_JOB_TTL
                        and job.sse_client_count == 0):   # not under a reader's feet
                    _keep_only_the_end(job.events)
    
    async def create_job(
        self,
        request_id: str,
        user_id: str,
        agent_name: str,
        session_id: Optional[str],
        agent_runner: Callable[[], Any],  # async generator function
        llm_profile: Optional[str] = None,
        mirror: bool = False,
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
            BackgroundJob instance with its task and its events
        """
        # The job object THIS wrapper owns, assigned below under the lock.
        # The wrapper must never write through ``self._jobs[request_id]``:
        # should the dict ever hold a different job for this id, writing
        # through it would flip a foreign, still-running job to COMPLETED —
        # a lying status on a live run. (``is not None`` is for the type
        # checker: the assignment happens before the task can first run.)
        own_job: Optional[BackgroundJob] = None
        # (sub-run id or None, step) -> the number of that step's latest answer delta
        latest_answer_delta: dict[tuple[Any, Any], int] = {}

        async def publish(event: Any) -> None:
            """Hand an event to every reader, and note what a reconnect is told about the run."""
            assert own_job is not None
            delta = _answer_delta(event)
            if delta is not None:
                # An answer delta carries the whole answer so far, so the one before it of
                # the same step says nothing a reader of both still needs but its own
                # ``delta``. Held whole, a thousand of them were every prefix of the
                # answer: a 30k-character answer, 20M characters per job.
                key = (event.get("run_id"), delta.get("step"))
                at = latest_answer_delta.get(key, -1) - (own_job.events_emitted - len(own_job.events))
                if at >= 0:
                    # a copy: a reader may be holding the event, and it is the run's
                    own_job.events[at] = _without_answer_so_far(own_job.events[at])
                latest_answer_delta[key] = own_job.events_emitted
            if isinstance(event, dict):
                kind = event.get("type")
                if kind in ANSWER_EVENTS:
                    own_job.answered = True
                # the session a run creates comes with its start event: the job is
                # found by it from then on (/api/sessions/active, a reconnect)
                elif kind == "start" and event.get("session_id"):
                    own_job.actual_session_id = event["session_id"]
                elif kind == "status" and event.get("message"):
                    own_job.last_status_message = event["message"]
            own_job.events.append(event)   # the buffer drops its oldest once full
            # Counted whether or not an older one had to go: the number says which event
            # this was, not how many are still held.
            own_job.events_emitted += 1
            async with own_job.changed:
                own_job.changed.notify_all()

        async def job_wrapper() -> None:
            """Wrapper that runs the agent and captures events/errors."""
            try:
                async for event in agent_runner():
                    await publish(event)

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
                logger.error(f"[BACKGROUND_JOB] Job {request_id} failed: {e}", exc_info=True)
                # Said on the stream too: a run that fails outside its own error handling
                # sends no error and no end, and its readers only saw the stream stop.
                await publish({"type": "error", "request_id": request_id, "message": str(e)})
                async with self._lock:
                    if own_job is not None:
                        own_job.status = JobStatus.FAILED
                        own_job.error_message = str(e)
                        own_job.completed_at = time.time()
            finally:
                # The run is over: every reader waiting for more hears it
                if own_job is not None:
                    async with own_job.changed:
                        own_job.changed.notify_all()

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
                events=deque(maxlen=self.MAX_EVENT_BUFFER),
                llm_profile=llm_profile,
                mirror=mirror,
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
        # Somebody stopped it -- the web chat's Stop, an admin, a deleted session,
        # writer_jobs. Noted first: the run may be past the point where the
        # layers below reach it (its finalize), and its session is let go marked
        # all the same, so nothing starts it again by itself (core/session_presence.py).
        from ..core.session_presence import note_stop
        note_stop(request_id)

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
                # A mirror's task is only its relay: cancelling it would not stop the
                # run, only the pages following it, while the run goes on to its end.
                if job and job.status == JobStatus.RUNNING and not job.mirror:
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
        for server in self._agent_servers():
            try:
                if request_id in server._request_manager.get_active_requests():
                    return True
            except Exception:  # noqa: BLE001 -- an agent without a request manager runs nothing here
                continue
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
                    "events_buffered": len(job.events),
                })
            return jobs
    
    def unread_for(self, request_id: str) -> Optional[float]:
        """Seconds no stream has read the job ``request_id`` (0.0 while one does);
        None when there is no such job (a run streamed inline, as POST /run with
        files is: it is read for as long as it runs)."""
        job = self._jobs.get(request_id)
        if job is None:
            return None
        if job.unread_since is None:   # cleared by every reader that comes
            return 0.0
        return time.monotonic() - job.unread_since

    async def increment_sse_client(self, request_id: str) -> None:
        """Track SSE client connection."""
        async with self._lock:
            if job := self._jobs.get(request_id):
                job.sse_client_count += 1
                job.unread_since = None
                logger.debug(f"[SSE_CLIENT] {request_id} clients: {job.sse_client_count}")
    
    async def decrement_sse_client(self, request_id: str) -> None:
        """Track SSE client disconnection."""
        async with self._lock:
            if job := self._jobs.get(request_id):
                job.sse_client_count = max(0, job.sse_client_count - 1)
                if job.sse_client_count == 0 and job.unread_since is None:
                    job.unread_since = time.monotonic()
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
