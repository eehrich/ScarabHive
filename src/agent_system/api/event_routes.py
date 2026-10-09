"""GET/POST /events: a run streamed as SSE through a background job, and reconnects to it.

The run is a BackgroundJob (services/background_job_manager.py): it goes on when
its client leaves, and a client that lost the stream reattaches by request id
(``_handle_events``' fast path) and reads on from the job's buffer. Every stream
of every job reads through ``_sse_lines``. What /events shares with POST /run --
the request id check, the agent and LLM choice, opening, naming and saving the
session -- is in run_start; holding the session in session_writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from typing import Any, Callable, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_system import app_state
from agent_system.api.app_context import AppContext, app_context, parse_json_body
from agent_system.api.run_start import (
    asks_a_person,
    carry_title,
    get_agent_with_overrides,
    llm_params_from,
    open_session_for_run,
    refused_before_the_run,
    save_after_run,
    set_run_metadata,
    sse_response,
    validate_client_request_id,
)
from agent_system.api.session_writes import claim_session, hold_fresh_session, let_go
from agent_system.core.request_context import (
    register_request_user,
    release_request_user_tree,
    release_run_attended,
    set_run_attended,
)
from agent_system.services.background_job_manager import (
    BackgroundJob,
    DuplicateRequestIdError,
    JobStatus,
    get_background_job_manager,
)
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)

router = APIRouter()


async def _format_and_yield_event(ev: dict, selected_agent, request_id: str, session_id: str) -> str:
    """An event as an SSE data line. An answer in it stays the Markdown the model wrote: the chat draws it."""
    payload = ev.to_dict() if hasattr(ev, 'to_dict') else ev
    try:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    except (TypeError, ValueError) as e:
        logger.error("Failed to serialize event %s: %s", ev, e)
        error_payload = {"type": "error", "message": f"Serialization error: {str(e)}"}
        return f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"


async def _sse_lines(ctx: AppContext, job: BackgroundJob, format_agent: Any, cursor: int,
                     before_send: Optional[Callable[[dict], None]] = None):
    """A job's events from number ``cursor`` on as SSE lines, a keepalive while it is quiet.

    Every stream of every job reads through here: the run's own and each reconnect.
    ``before_send`` sees each event first.
    """
    job_manager = get_background_job_manager()
    await job_manager.increment_sse_client(job.request_id)
    try:
        async for ev in job.follow(cursor, ctx.config.status.sse_keepalive_interval):
            if ev is None:
                yield ":keepalive\n\n"
                continue
            if before_send is not None:
                before_send(ev)
            yield await _format_and_yield_event(
                ev, format_agent, job.request_id, job.actual_session_id or job.session_id or "unknown")
    finally:
        await job_manager.decrement_sse_client(job.request_id)


async def _handle_events(
    ctx: AppContext,
    request: Request,
    task: str,
    session_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    llm_profile: Optional[str] = None,
    request_id: Optional[str] = None,
    force: bool = False,
    session_title: Optional[str] = None,
    attended: bool = False,
    llm_params: Optional[dict] = None,
):
    """Shared implementation for GET/POST /events endpoints.

    Streams agent SSE events for a task. ``attended``: the client shows the
    run's questions to the person who started it, who can answer them (the
    web chat); a new run records it, a reconnect keeps what its start said.
    """
    logger = logging.getLogger(__name__)

    # ========================================
    # SECURITY: Enforce endpoint authentication
    # ========================================
    current_user = await ctx.enforce_endpoint_security(request)

    # SECURITY: Validate LLM access (this is an LLM-consuming endpoint)
    ctx.validate_llm_access(current_user, is_llm_request=True)

    # Determine user_id for session management
    user_id = current_user.username if current_user else "anonymous"

    # Check if reconnecting to an existing job
    job_manager = get_background_job_manager()
    existing_job: Optional[BackgroundJob] = None
    if request_id:
        existing_job = await job_manager.get_job(request_id)
        if existing_job:
            # Verify user owns this job
            if existing_job.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied to this request")
            logger.info(f"Client reconnecting to job {request_id}")
            # Use the agent_name from the original job, not from query params
            agent_name = existing_job.agent_name

    # Not reconnecting: adopt a caller-supplied request_id (parity with
    # POST /run) so external dispatchers can track/cancel the run under
    # an ID they know; mint one only when the client sent none. Before
    # 2026-08 a client-supplied ID was silently discarded here, which
    # made every /events-dispatched run uncancellable by its caller.
    # Same guards as /run (validate_client_request_id): format → 400; id
    # already active elsewhere (non-BackgroundJob, so not reconnectable), or
    # in line with a live run's id → 409.
    if not existing_job:
        # No task, no run. The chat's reconnect URL carries none, and for an id whose
        # job is gone by then (a restart) it would start an empty turn in the session.
        if not (task or "").strip():
            if request_id:
                raise HTTPException(status_code=404, detail=f"No running job under request_id {request_id}")
            raise HTTPException(status_code=400, detail="Missing 'task'")
        if request_id:
            request_id = await validate_client_request_id(request_id)
        else:
            request_id = short_id()

    # Register request ownership for status stream security
    # (register_request_user, not a raw dict write -- keeps the FIFO cap)
    register_request_user(request_id, user_id)

    logger.info("SSE /events connected, task=%s, request_id=%s, session_id=%s, agent=%s, llm_profile=%s, user_id=%s, reconnect=%s",
               task, request_id, session_id, agent_name or "default", llm_profile or "default", user_id, existing_job is not None)

    # FAST PATH: For reconnecting clients, skip all setup and go straight to streaming
    if existing_job:
        # Load agent for event formatting (needed for HTML conversion via hooks)
        # Start with the global default agent from build_app()
        reconnect_agent = request.app.state.agent  # Global agent from build_app

        # Try to load the specific agent if it's different from the default
        if existing_job.agent_name and existing_job.agent_name != reconnect_agent.name:
            try:
                reconnect_agent = app_state.app_registry.get(existing_job.agent_name)  # type: ignore[attr-defined]
            except Exception as e:
                logger.warning(f"Could not load agent '{existing_job.agent_name}': {e}, using default")

        # ``catch_up=skip&seen=N``: the client has just loaded this session, and that
        # load told it the run had sent N events by then -- everything those events
        # say is already in the messages it is showing, so it reads on from event N.
        # Without it the reconnect replays what the buffer still holds.
        cursor = 0
        if request.query_params.get("catch_up") == "skip":
            try:
                cursor = max(0, int(request.query_params.get("seen", "")))
            except ValueError:
                cursor = 0

        async def reconnect_event_stream():
            """Simplified event stream for reconnecting clients."""
            # Send immediate :ok to establish connection
            yield ":ok\n\n"

            # Send reconnect event
            reconnect_payload = {
                "type": "reconnect",
                "request_id": request_id,
                "session_id": existing_job.actual_session_id or existing_job.session_id,
                "agent_name": existing_job.agent_name,
                "llm_profile": existing_job.llm_profile,
                "llm_params": existing_job.llm_params,
                "status": existing_job.status.value,
                "task": existing_job.task_description,
                "created_at": existing_job.created_at,
                "message": f"Reconnected to running job (started {int(time.time() - existing_job.created_at)}s ago)"
            }
            if existing_job.last_status_message:
                reconnect_payload["last_status"] = existing_job.last_status_message
            yield f"data: {json.dumps(reconnect_payload, ensure_ascii=False)}\n\n"

            async with aclosing(_sse_lines(ctx, existing_job, reconnect_agent, cursor)) as lines:
                async for line in lines:
                    yield line

        return sse_response(
            reconnect_event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # NORMAL PATH: For new requests, do full setup
    try:
        llm_params = llm_params or {}
        llm_choice = {"profile": llm_profile, "params": llm_params}
        selected_agent, llm_override, llm_profile_info = get_agent_with_overrides(
            ctx, agent_name, llm_profile, requester=current_user, llm_params=llm_params)
        session_exists = await open_session_for_run(selected_agent, user_id, session_id, llm_profile, llm_choice)
    except HTTPException:
        # Registered above for the status stream; no run follows to release it.
        release_request_user_tree(request_id)
        raise
    if session_exists:
        session_title = None  # it names a session the run creates, not one it continues

    async def event_stream():
        # Check if server is already shutting down
        if app_state.shutdown_event and app_state.shutdown_event.is_set():
            yield ":server_shutdown\n\n"
            return

        yield ":ok\n\n"

        was_new_session = (session_id is None) or (not session_exists)
        actual_session_id = session_id
        refused = False  # the run was refused at the agent's session lock

        # Session presence (core/session_presence/): held before the job
        # starts through the save after it; a session the job creates comes
        # with the start event.
        refusal, held = await claim_session(
            selected_agent, actual_session_id, user_id, force)
        if refusal:
            yield f"data: {json.dumps({'type': 'error', 'request_id': request_id, 'error': refusal}, ensure_ascii=False)}\n\n"
            return

        # Create new background job (reconnects use the fast path above)
        async def agent_runner():
            """Run the agent and yield events"""
            # Whether a person can be asked while it runs (tool_approval), for as
            # long as it runs: set by the run itself, so a request refused under
            # the same id (a duplicate) never touches a live run's mark.
            set_run_attended(request_id, asks_a_person(attended, current_user, ctx.live_config().auth.enabled))
            try:
                async for ev in selected_agent.run_events(
                    task, request_id, actual_session_id,
                    llm_override=llm_override, llm_profile_info_override=llm_profile_info
                ):
                    # Here, not where the stream is read: the run waits at this
                    # event until it is passed on, so no save of it comes first.
                    carry_title(selected_agent, ev, session_title)
                    yield ev
            finally:
                release_run_attended(request_id)

        try:
            job = await job_manager.create_job(
                request_id=request_id,
                user_id=user_id,
                agent_name=agent_name or "default",
                session_id=session_id,
                agent_runner=agent_runner,
                llm_profile=llm_profile,
                llm_params=llm_params,
            )
        except DuplicateRequestIdError:
            # A concurrent request won the race for this caller-supplied
            # id (the 409 guard above cannot be atomic with create_job,
            # which only runs once this body is streamed). Refuse instead
            # of starting a second agent under the same id; the caller's
            # retry lands on the reconnect fast path.
            logger.warning(
                "SSE /events refused duplicate request_id=%s — a job is "
                "already running under it", request_id,
            )
            yield f"data: {json.dumps({'type': 'error', 'request_id': request_id, 'error': 'request_id is already running — reconnect instead of starting a second run'}, ensure_ascii=False)}\n\n"
            let_go(selected_agent, held, user_id)  # no job of ours runs it
            return
        except BaseException:
            # Nothing of ours runs the session, and the finally below that
            # would let it go is not entered yet: in this process a hold
            # nobody releases refuses every later run of that session.
            let_go(selected_agent, held, user_id)
            raise
        # Store task description for reconnect
        job.task_description = task

        def take_the_session(ev: dict) -> None:
            """The run's own stream holds a session the run creates, from its start event."""
            nonlocal actual_session_id, held, refused
            refused = refused or refused_before_the_run(ev)
            if ev.get("type") == "start" and ev.get("session_id"):
                actual_session_id = ev["session_id"]
                if not held:
                    held = hold_fresh_session(selected_agent, actual_session_id, user_id)
            if was_new_session or ev.get("type") == "start":
                set_run_metadata(selected_agent, actual_session_id, user_id, llm_profile, llm_choice)

        try:
            async with aclosing(_sse_lines(ctx, job, selected_agent, 0, take_the_session)) as lines:
                async for line in lines:
                    yield line
        except asyncio.CancelledError:
            # SSE connection cancelled (client disconnect)
            # Send cancellation event to client (if possible)
            cancelled_payload = {"type": "disconnected", "request_id": request_id, "message": "SSE connection closed, job continues in background"}
            try:
                yield f"data: {json.dumps(cancelled_payload, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            raise
        except Exception as e:
            # Other errors - send error event
            logger.error(f"Error in event stream for request {request_id}: {e}", exc_info=True)
            error_payload = {"type": "error", "message": str(e), "request_id": request_id}
            try:
                yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            raise
        finally:
            # NOTE: We do NOT cancel the job here! The job continues running in background.
            # The job will be cancelled only via explicit /cancel endpoint.

            # Persist session if job is completed -- not one its run was refused,
            # nor one somebody holds after it (after_run): another run of this
            # process has it, and the tracker holds that run's live state (a tool
            # call without its result, say), or an append that saves it itself.
            try:
                if job.status in (JobStatus.COMPLETED, JobStatus.FAILED) and not refused:
                    if actual_session_id and app_state.session_service:
                        # Shielded: a client that leaves cancels the stream's scope,
                        # and the save was cancelled again at its first await.
                        with anyio.CancelScope(shield=True):
                            await save_after_run(selected_agent, user_id, actual_session_id, llm_profile,
                                                 was_new_session, llm_choice)
            finally:
                # Whatever became of the save: skipped, the session stayed held.
                let_go(selected_agent, held, user_id)

                # Cleanup: release ownership only if job is done (a running
                # job's stream may reconnect and must keep its mapping)
                if job.status != JobStatus.RUNNING:
                    release_request_user_tree(request_id)

    return sse_response(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events")
async def events_get(
    request: Request,
    task: str,
    session_id: Optional[str] = Query(default=None),
    agent: Optional[str] = Query(default=None, alias="agent"),
    agent_name: Optional[str] = Query(default=None),
    llm_profile: Optional[str] = Query(default=None),
    request_id: Optional[str] = Query(default=None),
    force: bool = Query(default=False),
    session_title: Optional[str] = Query(default=None),
    attended: bool = Query(default=False),
    ctx: AppContext = Depends(app_context),
):
    """Stream agent events for a task (GET).

    Query parameters:
    - task: The task to execute
    - session_id: Optional session ID for conversation continuity
    - agent or agent_name: Optional agent to use
    - llm_profile: Optional LLM profile override
    - session_title: Optional title for the session, written with its first save
    - request_id: Optional request ID — reconnects if it matches a
      running job, otherwise the new run is keyed under it (parity
      with POST /run; the guards of validate_client_request_id apply)
    - attended: the client shows the run's questions to the person who
      started it (tool_approval asks there); default false

    Note: For long task texts, prefer POST /events to avoid URL length limits.
    """
    # Prioritize 'agent' parameter over 'agent_name' for backwards compatibility
    effective_agent_name = agent or agent_name
    return await _handle_events(
        ctx,
        request=request,
        task=task,
        session_id=session_id,
        agent_name=effective_agent_name,
        llm_profile=llm_profile,
        request_id=request_id,
        force=force,
        session_title=session_title,
        attended=attended,
    )


@router.post("/events")
async def events_post(request: Request, ctx: AppContext = Depends(app_context)):
    """Stream agent events for a task (POST).

    Accepts JSON body with fields:
    - task: The task to execute (required)
    - session_id: Optional session ID for conversation continuity
    - agent_name: Optional agent to use
    - llm_profile: Optional LLM profile override
    - llm_params: Optional object, what the chat set for the model (CHAT_LLM_PARAMS)
    - session_title: Optional title for the session, written with its first save
    - request_id: Optional request ID — reconnects if it matches a
      running job, otherwise the new run is keyed under it (parity
      with POST /run; the guards of validate_client_request_id apply)
    - attended: true when the client shows the run's questions to the
      person who started it (tool_approval asks there); default false

    This endpoint avoids URL length limits that affect GET /events
    when sending long task texts.
    """
    body = await parse_json_body(request)
    task = body.get("task")
    if not task:
        raise HTTPException(status_code=400, detail="Missing 'task' in request body")
    return await _handle_events(
        ctx,
        request=request,
        task=task,
        session_id=body.get("session_id"),
        agent_name=body.get("agent_name") or body.get("agent"),
        llm_profile=body.get("llm_profile"),
        request_id=body.get("request_id"),
        force=bool(body.get("force")),
        session_title=body.get("session_title"),
        attended=body.get("attended") is True,
        llm_params=llm_params_from(body.get("llm_params")),
    )
