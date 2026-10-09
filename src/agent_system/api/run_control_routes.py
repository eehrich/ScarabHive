"""What a client does to a run in flight, or to a session beside the runs.

Ask a run's status, cancel it, append a message to it (or, once it is over, to
its session), append to a session no run has, and create a session id; the two
force_optimize routes only say that manual optimization is gone. A request id is
all most of these are given, so each first makes sure the caller owns the run it
reaches (``_refuse_foreign_request``). Starting a run is in run_routes and
event_routes; the session writes are in session_writes.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_system import app_state
from agent_system.api.app_context import AppContext, app_context, chat_agent, parse_json_body
from agent_system.api.session_writes import (
    append_and_persist,
    resolve_agent_for_request,
    session_agent_name,
    settling_agent,
)
from agent_system.auth.session_access import viewer
from agent_system.core.request_context import get_request_user, request_user_map
from agent_system.services.background_job_manager import JobStatus, get_background_job_manager
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)

router = APIRouter()


async def _refuse_foreign_request(request_id: str, current_user: Any, *, reaches_below: bool = False) -> None:
    """403 unless every run the request id reaches is the caller's own, or the caller is an admin.

    A request id is all these endpoints are given. Without this, anyone signed in
    who learned one could read another user's run, stop it, or put words into it --
    which the run then acts on with its owner's tools.

    An id may be nobody's and still be somebody's run: a sub-run is named by its
    caller's id and `_…`, and forgotten by the owner map when its caller's turn ends,
    while it may work on -- so the owner is that of the nearest known run at or above
    the id. A cancel (``reaches_below``) stops every run whose id extends the one
    given, so there every known run below it counts too. A request nobody is known to
    own (a restart forgot it) is left alone: nothing of anybody's is reached through it.
    """
    if current_user is None or getattr(current_user, "role", None) == "admin":
        return
    job_manager = get_background_job_manager()
    owners = set()
    parts = request_id.split("_")
    for n in range(len(parts), 0, -1):
        above = "_".join(parts[:n])
        job = await job_manager.get_job(above)
        owner = job.user_id if job is not None else get_request_user(above, default=None)
        if owner is not None:
            owners.add(owner)
            break
    if reaches_below:
        below = f"{request_id}_"
        owners.update(user for rid, user in list(request_user_map.items()) if rid.startswith(below))
        owners.update(job["user_id"] for job in await job_manager.get_all_jobs(include_completed=True)
                      if job["request_id"].startswith(below))
    if owners - {current_user.username}:
        raise HTTPException(status_code=403, detail="Access denied to this request")


@router.get("/api/requests/{request_id}/status")
async def get_request_status(request_id: str, request: Request, ctx: AppContext = Depends(app_context)):
    """Get the status of a request for reconnection purposes.

    Used by the WebUI before it follows a run again after a reload: GET /events
    with an id the job manager no longer holds would start a new run. Returns
    whether the request is still running or completed.
    """
    # First check BackgroundJobManager for more accurate status
    job_manager = get_background_job_manager()
    job = await job_manager.get_job(request_id)
    await _refuse_foreign_request(request_id, await ctx.enforce_endpoint_security(request))
    # A finished MIRROR (POST /run) answers as if there had been no job: its
    # caller had the answer, and "completed" with the job's keys is what the
    # writer reconcile takes as proof that a book run is done.
    if job and job.mirror and job.status != JobStatus.RUNNING:
        job = None
    if job:
        return {
            "request_id": request_id,
            "status": job.status.value,
            "completed": job.status != JobStatus.RUNNING,
            "error": job.error_message,
            "sse_clients": job.sse_client_count,
            "events_buffered": len(job.events),
        }

    # Fallback to session tracker — checks the DEFAULT agent first
    # (cheap), then walks the agent registry so requests running on
    # sub-agent servers (linear_book, v5b_story_designer,
    # cover_artist, ...) are seen too. Without the walk this
    # endpoint reported ``unknown/no_active_run`` for a /run that
    # was actively grinding on a non-default agent — the writer
    # reconcile pass consumed that as "run lost" and re-queued the
    # job for resume, double-running multi-hour generations. Same
    # per-agent blind-spot class as the 2026-06-27 cancel
    # regression, fixed the same way (registry walk).
    is_active = await ctx.agent._session_tracker.is_request_active(request_id)
    if not is_active:
        is_active = await job_manager.is_request_active_anywhere(request_id)

    if is_active:
        return {
            "request_id": request_id,
            "status": "running",
            "completed": False
        }

    # Request not active in either tracker.
    #
    # 2026-06-27 fix: previously returned ``status='completed',
    # completed=true`` which LIED about the run's outcome — there
    # is no positive evidence the request finished successfully,
    # only that this process doesn't know about it (typical case:
    # agent-api restarted and the BackgroundJob died with it).
    # The writer-side reconcile pass (producer.py
    # _reconcile_agent_api_orphans) consumed that lie as success
    # and marked the queue row 'done' → silent data loss
    # (2026-06-26 incident root cause). Frontend's poll loop
    # also treated it as a clean completion and rendered an
    # empty result.
    #
    # New contract: status='unknown', completed=false +
    # error+reason so the writer-side reconcile leaves the row
    # for the sweep (= Resume) and the WebUI lets the run go
    # instead of following it.
    return {
        "request_id": request_id,
        "status": "unknown",
        "completed": False,
        "reason": "no_active_run",
        "error": "Request not found — the server may have restarted while this run was active",
    }


@router.post("/api/requests/{request_id}/cancel")
async def cancel_request(request_id: str, request: Request, force: bool = Query(default=False),
                         ctx: AppContext = Depends(app_context)):
    """Cancel an active request by its ID.

    Delegates to ``BackgroundJobManager.cancel_job`` which is the
    single source of truth for writer-side cancellation: it sets
    the cancellation token, walks the agent registry to call
    ``cancel_request`` on the server that actually owns the
    request (linear_book, v5b_story_designer, cover_artist, ...),
    falls back to the default agent for chat_agent-style requests,
    and force-cancels the asyncio task after the grace period.

    See ``BackgroundJobManager.cancel_job`` for the layered
    semantics. The 2026-06-27 cancel regression (sub-agent
    requests reported 'cancelled' but kept running) is closed
    there — every caller of this endpoint, the admin endpoint at
    ``/admin/active-sessions/{rid}/cancel``, and the writer-jobs
    propagation helper now use the same path.

    Args:
        request_id: The request ID to cancel
        force: If True, force-cancel after 5s if agent doesn't respond to graceful cancel
    """
    logger = logging.getLogger(__name__)
    logger.info(
        "Cancel request received for request_id=%s (force=%s)",
        request_id, force,
    )
    await _refuse_foreign_request(request_id, await ctx.enforce_endpoint_security(request), reaches_below=True)
    success = await get_background_job_manager().cancel_job(
        request_id, force_timeout=5.0 if force else 0.0,
    )
    if success:
        return {"status": "cancelled", "request_id": request_id}
    return {
        "status": "not_found",
        "request_id": request_id,
        "message": "Request not found or already completed",
    }


async def _session_owner_agent(ctx: AppContext, request: Request, sid: str, user_id: str) -> Any:
    """The agent whose SessionTracker holds a stored session -- every agent carries its own, and a
    conversation of openai_api runs on the agent its model names. Written through another agent's tracker, a
    message was read back into a copy no run of the session looks at, and put back or saved over by the one
    that does. The agent of a turn settling it (settling_agent), else the one the record names, else the entry
    agent: a session without a record, or whose agent is gone."""
    settling = settling_agent(request, sid)
    if settling is not None:
        return settling
    ran_with = await session_agent_name(sid, user_id)
    return (chat_agent(request, ran_with) if ran_with else None) or ctx.agent


@router.post("/events/{request_id}/append")
async def append_event(
    request_id: str,
    request: Request,
    session_id: Optional[str] = Query(default=None),
    fallback: str = Query(default="session"),
    force: bool = Query(default=False),
    ctx: AppContext = Depends(app_context),
):
    """Append a user message to an existing active request or session.

    If `session_id` query parameter is provided, append directly to session.
    Body: { "content": "the user message" }

    `fallback` controls what happens when the request is not active anymore:
    - "session" (default): append to the request's persisted session (the
      message is stored but only answered by the next run).
    - "none": return 404 so the caller can start a new request instead.

    Note: When appending to a session (not an active request), the session is
    persisted to disk automatically.
    """
    logger = logging.getLogger(__name__)
    from fastapi import HTTPException

    # ========================================
    # SECURITY: Enforce endpoint authentication
    # ========================================
    current_user = await ctx.enforce_endpoint_security(request)
    user_id = viewer(current_user)

    body = await parse_json_body(request)

    content = body.get('content')
    if not content:
        raise HTTPException(status_code=400, detail="Missing 'content' in body")

    if session_id:
        owner_agent = await _session_owner_agent(ctx, request, session_id, user_id)
        # Ownership check before mutating someone else's session (IDOR) -- against the tracker the append
        # writes, not the entry agent's (a session without an owner on disk passes there for anybody).
        await ctx.verify_session_owner(session_id, current_user, owner_agent._session_tracker)
        # Append directly to persisted session using agent method
        logger.debug("Appending to session %s: %.120s", session_id, content)
        if not await append_and_persist(owner_agent, session_id, content, user_id, force,
                                        entry_agent=ctx.agent):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"status": "appended", "session_id": session_id}

    # Mid-run appends must reach the agent instance that owns the run:
    # runs started with agent_name execute on that agent, not on the
    # default agent this endpoint is bound to.
    target_agent = await resolve_agent_for_request(
        request_id, get_background_job_manager(), app_state.app_registry, ctx.agent
    )
    await _refuse_foreign_request(request_id, current_user)

    logger.debug("Append request received for request_id=%s (agent=%s): %.120s",
                 request_id, target_agent.name, content)
    try:
        appended = await target_agent.append_user_message(request_id, content)
    except HTTPException:
        # Let agent-level HTTPExceptions bubble up
        raise
    except Exception as e:
        logger.exception("Unexpected error in append_user_message for %s: %s", request_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    if appended:
        # Active request - will be persisted when request completes
        return {"status": "appended", "request_id": request_id}

    if fallback == "none":
        # Caller handles the finished-run case itself (e.g. starts a new
        # request) instead of parking the message in the session unanswered.
        raise HTTPException(status_code=404, detail="Request not active")

    # If request not found/finished, try to append into the persisted session for this request
    sid = target_agent._session_tracker.get_session_for_request(request_id)
    if sid:
        await ctx.verify_session_owner(sid, current_user, target_agent._session_tracker)
        logger.debug("Request %s already finished; appending to session %s", request_id, sid)
        if await append_and_persist(target_agent, sid, content, user_id, force, entry_agent=ctx.agent):
            return {"status": "appended", "session_id": sid}

    raise HTTPException(status_code=404, detail="Request not found or already completed")


@router.post("/sessions")
async def create_session(ctx: AppContext = Depends(app_context)):
    """Create a new session id for multi-turn conversations."""
    sid = short_id()
    # Pre-create empty session in agent using the component API
    ctx.agent._session_tracker.set_session_messages(sid, [])
    return {"session_id": sid}


@router.post("/sessions/{session_id}/append")
async def append_to_session_endpoint(session_id: str, request: Request,
                                     force: bool = Query(default=False),
                                     ctx: AppContext = Depends(app_context)):
    """Append a user message directly to a session (no active request required).

    This endpoint adds a user message to an existing session and persists it to disk.
    """
    logger = logging.getLogger(__name__)

    # ========================================
    # SECURITY: Enforce endpoint authentication
    # ========================================
    current_user = await ctx.enforce_endpoint_security(request)
    user_id = viewer(current_user)

    body = await parse_json_body(request)
    try:
        content = body.get('content')
        if not content:
            raise HTTPException(status_code=400, detail="Missing 'content' in body")

        logger.debug("Session append request for session_id=%s: %.120s", session_id, content)

        owner_agent = await _session_owner_agent(ctx, request, session_id, user_id)
        # Ownership check before mutating someone else's session (IDOR) -- against the tracker the append
        # writes, not the entry agent's (a session without an owner on disk passes there for anybody).
        await ctx.verify_session_owner(session_id, current_user, owner_agent._session_tracker)

        if not await append_and_persist(owner_agent, session_id, content, user_id, force,
                                        entry_agent=ctx.agent):
            raise HTTPException(status_code=404, detail="Session not found")

        return {"status": "appended", "session_id": session_id}
    except HTTPException:
        # Client errors (400 missing content, 403 ownership, 404) must
        # keep their status — the generic handler below turned them
        # into 500s.
        raise
    except Exception as e:
        logger.exception("Failed to append to session %s: %s", session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


def _manual_optimization_is_gone() -> HTTPException:
    """What both force_optimize routes answer: 410, and where optimization went."""
    return HTTPException(
        status_code=410,
        detail=(
            "Manual optimization was removed: context optimization and "
            "summarization run via the context_engineer/"
            "context_summarizer hook plugins during LLM calls."
        ),
    )


@router.post("/sessions/{session_id}/force_optimize")
async def force_optimize_session(session_id: str, request: Request, ctx: AppContext = Depends(app_context)):
    """Gone: context optimization runs automatically via hook plugins.

    The manual path died with the hook migration -- no Agent carries a
    ``token_optimizer`` anymore, so this endpoint answered "ok" for a
    long time without doing anything. 410 tells the caller the truth
    instead of pretending success. No shipped consumer (UI or repo
    code) calls it.
    """
    await ctx.enforce_endpoint_security(request)
    raise _manual_optimization_is_gone()


@router.post("/sessions/force_optimize")
async def force_optimize_all_sessions(request: Request, ctx: AppContext = Depends(app_context)):
    """Gone -- see force_optimize_session."""
    await ctx.enforce_endpoint_security(request)
    raise _manual_optimization_is_gone()
