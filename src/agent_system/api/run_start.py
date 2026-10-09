"""What POST /run (run_routes) and /events (event_routes) both do around a run.

Before it: the caller's request id is checked (``validate_client_request_id``),
the agent picked with its LLM override and role gate (``llm_params_from``,
``get_agent_with_overrides``), the session opened (``open_session_for_run``).
While it goes: whether a person may be asked (``asks_a_person``), the session's
metadata and title (``set_run_metadata``, ``carry_title``), and whether the run
was refused at the agent's door (``refused_before_the_run``) -- then nothing of
it is saved. After it: the save (``save_after_run``). ``sse_response`` is the
streaming response both send. One module, so the two routes do these steps the
same way.

Holding the session across processes is not here but in session_writes, which
the appends and the chat commands share as well.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from agent_system import app_state
from agent_system.api.app_context import AppContext
from agent_system.core.session_presence import forget_stop
from agent_system.services.background_job_manager import get_background_job_manager
from agent_system.services.session_manager import SessionPermissionError
from agent_system.servers.agent.components.status_forwarding import in_line_with_a_live_run

logger = logging.getLogger(__name__)


def asks_a_person(attended: bool, current_user: Any, auth_enabled: bool) -> bool:
    """Whether a run may put questions to the person who started it: the client
    says it shows them (``attended``), and that person can answer -- signed in,
    or authentication is off. Nobody signed in with authentication on cannot (an
    anonymous visitor, an endpoint that let the request through without a user):
    the answer route takes a sign-in, so a question would only time out."""
    if not attended:
        return False
    if not auth_enabled:
        return True
    return current_user is not None and getattr(current_user, "is_authenticated", True) is not False


async def validate_client_request_id(client_request_id: str) -> str:
    """Guard a caller-supplied request_id before adopting it for a NEW run.

    Shared by POST /run and GET/POST /events (non-reconnect path). The id
    flows into log lines, ownership maps and cancellation-token keys, so:
      - format whitelist (8-64 url-safe chars) → 400;
      - 409 when the id is already live ANYWHERE (BackgroundJob, any
        registry agent, default agent) — the duplicate-dispatch guard: a
        caller retry that fires while the original run is still grinding
        gets a clean 409 instead of silently starting a second run;
      - 409 when the id and a live run's id are in line, one being the other
        followed by `_…`: that is how the server names a run's sub-runs, and
        every stream above a run takes what starts with its id and `_`. Such
        an id would read as a live run's sub-run and be handed its events, or
        make a live run's sub-runs read as its own. (A job dispatched again
        under its id while a sub-agent of its last attempt still works waits
        for that one, as it waits for a run of its own id.)
    """
    rid = str(client_request_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", rid):
        raise HTTPException(
            status_code=400,
            detail="invalid request_id: expected 8-64 chars [A-Za-z0-9_-]",
        )
    if in_line_with_a_live_run(rid):
        raise HTTPException(
            status_code=409,
            detail=f"request_id {rid} is in line with a run in flight (one extends the other by _)",
        )
    if await get_background_job_manager().is_request_active_anywhere(rid):
        raise HTTPException(
            status_code=409,
            detail=(
                f"request_id {rid} is already active — "
                "the original run is still in flight"
            ),
        )
    # A new run under an id that was stopped before (writer_jobs dispatches a run
    # again under its id): that stop was the earlier run's (core/session_presence.py).
    # Here and nowhere else -- the ids every other caller mints are new, and a stop
    # noted before their run registers is theirs.
    forget_stop(rid)
    return rid


def llm_params_from(raw: Any) -> dict:
    """A request's llm_params: an object (JSON body) or its JSON text (a form field); 400 if neither."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="llm_params is not valid JSON")
    from agent_system.llm.factory import chat_llm_params_problem
    problem = chat_llm_params_problem(raw)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    return dict(raw)


def _agent_not_found(agent_name: str) -> HTTPException:
    """POST /run's and /events' answer for an agent name nothing is registered under."""
    return HTTPException(
        status_code=404,
        detail=(
            f"agent_not_found:{agent_name}. "
            "Check the plugin name (registered tool server name, not "
            "the agent yaml filename) and that the plugin is loaded."
        ),
    )


def get_agent_with_overrides(ctx: AppContext, agent_name: Optional[str] = None,
                             llm_profile: Optional[str] = None,
                             *, requester: Any, llm_params: Optional[dict] = None):
    """Get agent instance with optional overrides.

    Args:
        agent_name: Name of agent to use (None = use default global agent)
        llm_profile: LLM profile to use (None = use agent's configured profile)
        llm_params: what a chat set for its model (llm_params_from checked it); without a
            profile they apply to the agent's own primary, which is no switch of model
        requester: the caller the endpoint resolved; an agent it may not run
            (its role gate) is answered as an unknown agent (404), the default
            agent -- which has a name only the server knows -- with a 403.
            Keyword and required: a run started without asking is the hole.

    Returns:
        Tuple of (agent_instance, llm_override, llm_profile_info)
        - agent_instance: The selected agent
        - llm_override: LLM client to pass to run_events (None if using agent's default)
        - llm_profile_info: Profile info string for status display (None if no override)
    """
    selected_agent = ctx.agent
    llm_override = None
    llm_profile_info = None

    # Override agent if specified. When the caller passes an
    # EXPLICIT agent_name we must NOT silently fall back to the
    # default agent — that turns an "agent name typo" or a "plugin
    # not loaded" bug into a chat_agent run that returns HTTP 200,
    # which downstream batch dispatchers (writer-jobs book_generation
    # / fix) treat as success. Strict 404 instead, so the caller's
    # job-row goes 'failed' with a useful error.
    if agent_name and agent_name != selected_agent.name:
        try:
            selected_agent = app_state.app_registry.get(agent_name)  # type: ignore[attr-defined]
            from agent_system.servers.agent.server import Agent as _Agent
            if not isinstance(selected_agent, _Agent):
                raise HTTPException(status_code=400, detail=f"'{agent_name}' is not an agent")
            # CRITICAL: Inject app_state.session_service (same as CLI line 1252 and build_app line 354-357)
            # ALWAYS inject, even if attribute exists, to refresh the reference
            selected_agent._session_service = app_state.session_service
            logger.debug(f"Injected SessionService into agent '{agent_name}' via /run endpoint")
        except KeyError:
            logger.warning(
                "Agent '%s' not found — returning 404 (no silent fallback)",
                agent_name,
            )
            raise _agent_not_found(agent_name)
        except HTTPException:
            # The deliberate 400 ("'x' is not an agent") must keep its
            # status — the generic handler below turned it into a 500.
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to get agent: {str(e)}")

    # The role gate, for the named agent and the default one alike -- before
    # an LLM client is built for a run that is not going to happen.
    if ctx.gate_refuses(selected_agent, requester):
        if agent_name:
            raise _agent_not_found(agent_name)
        raise HTTPException(status_code=403, detail="Permission denied")

    # Create LLM override if profile specified. Resolved against the LIVE
    # config -- a profile added by a reload was "not found" here and fell
    # back to the default profile.
    live = ctx.live_config()
    if llm_params and not llm_profile:
        llm_profile = selected_agent.agent_config.default_llm_profile
    if llm_profile and live.llm_system and live.llm_system.profiles:
        if llm_profile not in live.llm_system.profiles:
            # LLM profile not found - fallback to default profile
            default_profile = live.llm_system.default_profile
            logger.warning(f"LLM profile '{llm_profile}' not found, falling back to default profile '{default_profile}'")
            llm_profile = default_profile

        try:
            from agent_system.llm.factory import override_for_profile
            llm_override, llm_profile_info = override_for_profile(
                live, getattr(selected_agent, "agent_config", None), llm_profile, llm_params or None)
        except Exception as e:
            logger.error(f"Failed to create LLM override: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to apply LLM profile: {str(e)}")

    return selected_agent, llm_override, llm_profile_info


async def open_session_for_run(selected_agent, user_id: str, session_id: Optional[str],
                               llm_profile: Optional[str], llm_choice: dict) -> bool:
    """SessionService.open_for_run for /run and /events; whether the session
    existed. 403 for another user's session; without an id there is nothing
    to open -- the run creates its session and names it in the start event.

    While a run of this process has the session, the tracker holds that
    run's state -- a session it has not saved yet reads as new, and must
    not be reset under it (in_use). Another user's such run answers 403:
    the metadata this writes would hand them its session.
    """
    if not session_id:
        return False
    running = (await get_background_job_manager().active_sessions()).get(session_id)
    if running is not None and running.get("user_id") not in (None, user_id):
        raise HTTPException(status_code=403,
                            detail=f"Permission denied: session {session_id} belongs to another user")
    try:
        return await app_state.session_service.open_for_run(
            selected_agent, user_id, session_id,
            llm_profile or selected_agent.agent_config.default_llm_profile,
            in_use=running is not None, llm_choice=llm_choice)
    except SessionPermissionError as e:
        raise HTTPException(status_code=403, detail=f"Permission denied: {e}")


def carry_title(target_agent: Any, event: dict, title: Optional[str]) -> None:
    """A title the caller gave the session a run starts: handed to the run
    at its start event -- the one that names a new session -- and written
    by its first save (SessionService), as agent-cli writes a /title typed
    before the first message. Only text is a title: a JSON number or a
    file part of that name is not one."""
    if not isinstance(title, str) or not title.strip():
        return
    if event.get("type") == "start" and event.get("session_id"):
        target_agent._session_tracker.carry_title(event["session_id"], title.strip())


def set_run_metadata(selected_agent: Any, session_id: Optional[str], user_id: str,
                     llm_profile: Optional[str], llm_choice: dict) -> None:
    """What the session of a run records about it, set on the agent's tracker: whose it is, the
    agent and the LLM profile it runs on, and what the caller chose for the model."""
    selected_agent._session_tracker.set_session_metadata(session_id, {
        "user_id": user_id,
        # The real agent name, never the literal "default" —
        # a later append persists this field to disk and the
        # session UI resolves it against the registry.
        "agent_name": selected_agent.name,
        # Use override llm_profile if provided, otherwise agent's default
        "llm_profile": llm_profile or selected_agent.agent_config.default_llm_profile,
        "llm_choice": llm_choice,
    })


async def save_after_run(selected_agent: Any, user_id: str, session_id: str, llm_profile: Optional[str],
                         was_new_session: bool, llm_choice: dict) -> None:
    """Save the session after its run: under the agent that ran it and the profile it ran on -- the
    caller's override, else the agent's own default. ``after_run``: SessionService leaves a session
    somebody holds after the run (another run of this process, an append saving it itself)."""
    effective_llm_profile = llm_profile or selected_agent.agent_config.default_llm_profile
    await app_state.session_service.save_session(
        selected_agent,
        user_id,
        session_id,
        selected_agent.name,
        effective_llm_profile,
        was_new_session,
        after_run=True,
        llm_choice=llm_choice,
    )


def refused_before_the_run(event: dict) -> bool:
    """Whether a run event says the run was refused before it started (Agent.run_events): at the agent's
    session lock (another run of this process has the session), by its role gate, or because the session is
    another user's. Nothing of this request may be saved to it."""
    from agent_system.servers.agent.server import refused_before_the_run as refused

    return refused(event)


def sse_response(stream, **kwargs) -> StreamingResponse:
    """A streaming response whose generator is closed as soon as its client has gone.

    Starlette cancels a response whose client went away but never closes its body
    generator. Caught at a yield -- in the middle of a send, or at a stream's
    farewell line -- the generator waited there for the garbage collector, and its
    ``finally`` with it: the job's reader count, the run's session save, its hold on
    the session, the request's owner entries.
    """
    async def close() -> None:
        # A coroutine function: handed `stream.aclose` itself, Starlette takes it for a plain
        # callable, calls it in a thread -- and the awaitable it returns is never awaited.
        await stream.aclose()

    return StreamingResponse(stream, background=BackgroundTask(close), **kwargs)
