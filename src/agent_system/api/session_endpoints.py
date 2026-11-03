"""
Session Management API Endpoints

Provides CRUD endpoints for managing user conversation sessions, and assembles
``session_router``: the list and its organisation are in session_listing_routes,
what a session is doing right now in session_live.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from agent_system import app_state
from agent_system.auth.session_access import viewer
from agent_system.auth.models import User
from agent_system.auth.dependencies import get_current_active_user, get_optional_user
from agent_system.api.app_context import NotAnAgent, registered_agent
from agent_system.api.dependencies import get_session_manager, get_agent_optional, get_tool_registry
from agent_system.api.session_http import session_errors_as_http
from agent_system.api.session_listing_routes import (  # noqa: F401 -- re-exported: the names tests and callers import
    _DESCENDANT_READS_AT_ONCE,
    _build_descendants_context_vars,
    list_session_children,
    list_sessions_hierarchy,
    list_sessions_for_chat,
    add_routes as _add_listing_routes,
    get_session_folders,
    put_session_folder,
    put_session_folders,
    resolve_session,
)
from agent_system.api.session_live import (  # noqa: F401 -- re-exported, as above
    _add_sessions_running_elsewhere,
    _run_progress,
    serve_live_state,
)
from agent_system.api.session_writes import resolve_agent_for_request
from agent_system.services.background_job_manager import get_background_job_manager

logger = logging.getLogger(__name__)

session_router = APIRouter(prefix="/api/sessions", tags=["sessions"])

#: How many session ids one ``/active`` poll may ask about. The sidebar asks for
#: what it shows -- roots plus the children of expanded nodes -- and a tree that
#: deep is a scrolling problem before it is a polling one. Cut rather than
#: refused: a poll is repeated seconds later anyway.
_ACTIVE_IDS_LIMIT = 200


class CreateSessionRequest(BaseModel):
    """Request model for creating a session."""
    title: str = "New Conversation"
    agent_name: str = "basic_agent"
    llm_profile: str = "default"
    session_id: Optional[str] = None


class UpdateSessionRequest(BaseModel):
    """Request model for updating session metadata."""
    title: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class SessionResponse(BaseModel):
    """Response model for session data."""
    session_id: str
    user_id: str
    title: str
    agent_name: str
    llm_profile: str
    created_at: str
    updated_at: str
    message_count: int
    last_agent_response: Optional[str] = None
    tags: List[str] = []


def _refuse_gated_agent(http_request: Request, agent_name: str, who: Any) -> None:
    """403 when *who* may not run *agent_name* under its role gate (metadata.min_role).

    A session stores the agent it is for, and whatever runs the session later --
    a wake runs it through agent-cli, as its user -- runs that agent. Refused here,
    the record never names an agent its owner may not run.
    """
    from agent_system.auth.agent_access import agent_min_role, agent_run_denial

    state = http_request.app.state
    registry = getattr(state, "tool_registry", None)
    min_role = agent_min_role(registry, agent_name) if registry is not None else None
    if min_role is None:
        return
    # The auth the process enforces (app.state.auth_config, see build_app); the
    # live config only where an app was built without it.
    auth = getattr(state, "auth_config", None)
    if auth is None:
        auth = getattr(getattr(state, "config", None), "auth", None)
    reason = agent_run_denial(min_role, who, auth)
    if reason:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Agent '{agent_name}' may not be run: {reason}")


@session_router.post("", response_model=Dict[str, str], status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest,
    http_request: Request,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Create a new conversation session (authenticated or anonymous)."""
    # session_manager injected via dependency

    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = viewer(current_user)
    # The session's user is who will run its agent: the account, or "anonymous".
    _refuse_gated_agent(http_request, request.agent_name,
                        current_user if current_user is not None else user_id)

    try:
        session = await session_manager.create_session(
            user_id=user_id,
            title=request.title,
            agent_name=request.agent_name,
            llm_profile=request.llm_profile,
            session_id=request.session_id
        )

        return {"session_id": session["session_id"], "status": "created"}

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        logger.exception("Failed to create session: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.get("", response_model=List[SessionResponse])
async def list_sessions(
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """List all sessions for the current user (or anonymous if not authenticated)."""
    # session_manager injected via dependency

    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = viewer(current_user)

    try:
        sessions = await session_manager.list_sessions(user_id)

        # Filter out sub-agent sessions (parent_session holds a truthy value).
        # Every index entry carries the KEY (None for top-level sessions), so
        # checking key existence filtered out everything -- the endpoint
        # returned a constant [].
        top_level_sessions = [s for s in sessions if not s.get("parent_session")]

        # Transform to response models
        return [
            SessionResponse(
                session_id=s["session_id"],
                user_id=s["user_id"],
                title=s["title"],
                agent_name=s["agent_name"],
                llm_profile=s["llm_profile"][0] if isinstance(s["llm_profile"], list) else s["llm_profile"],
                created_at=s["created_at"],
                updated_at=s["updated_at"],
                message_count=s.get("message_count", 0),
                last_agent_response=s.get("last_agent_response"),
                tags=s.get("tags", [])
            )
            for s in top_level_sessions
        ]

    except Exception as e:
        logger.exception("Failed to list sessions: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# The list, its folders, a node's children, /resolve and /listing -- ahead of the
# "/{session_id}" routes below, which would take "hierarchy" or "folders" for an id.
_add_listing_routes(session_router)


@session_router.get("/active", response_model=Dict[str, Any])
async def list_active_sessions(
    ids: str = "",
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
    agent=Depends(get_agent_optional),
):
    """Of the sessions named in ``ids``, which are running right now.

    Declared BEFORE ``/{session_id}``: FastAPI matches in order, and the
    parameterised route would swallow "active" as a session id.

    The caller names the sessions it is asking about -- the rows the sidebar has
    on screen. That is the question the sidebar actually has ("which of these is
    busy") and it keeps the answer small.

    Naming the ids is NOT what makes it safe, though; every entry is checked
    against the caller. A background job carries the user who started it, and a
    lock-held session usually has one in the tracker's metadata; only where
    neither names a user does this fall back to whose directory the session file
    sits in. Without that, this would be the one route here that answers about a
    session it never established belongs to the asker, and it hands out a
    ``request_id`` that ``POST /api/requests/{id}/cancel`` acts on -- an endpoint
    that, today, checks no ownership of its own.

    ``answered`` marks a run that has sent its answer and is only finishing.
    ``cancel_session`` spares those deliberately (cancelling one takes its
    background sub-agents with it), so a caller that cancels from this answer
    spares them too.

    ``attachable`` says whether a client may follow this run's stream: only a
    background job can be reconnected to. A ``/run`` carrying files and a
    sub-agent's run have no job -- they are reported (the sidebar marks them)
    but following them would answer 409 and read to the viewer as a lost
    connection.

    ``elsewhere`` marks a run this process is not the one running: a woken
    session, working in its own ``agent-cli`` process. It carries no
    ``request_id`` and is never ``attachable`` -- there is no stream to follow
    and no way to cancel it from here, because both walk THIS process. A caller
    can say that the session is busy and read it again when it is let go.

    Not reported: a session the run itself creates, until its first turn has
    named it. It has no row in the sidebar yet either, and the chat that started
    it follows its own run directly.
    """
    wanted = [s for s in (i.strip() for i in ids.split(",")) if s][:_ACTIVE_IDS_LIMIT]
    if not wanted:
        return {"active": {}}
    user_id = viewer(current_user)
    try:
        active = await get_background_job_manager().active_sessions()
    except Exception as e:
        logger.exception("Failed to read active sessions: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    answer = {}
    for session_id in wanted:
        info = active.get(session_id)
        if not info:
            continue
        owner = info.get("user_id")
        if owner is None:
            # Neither the job nor the tracker's metadata named a user: fall back to
            # whose directory the session file sits in.
            if not session_manager.belongs_to(user_id, session_id):
                continue
        elif owner != user_id:
            continue
        answer[session_id] = {"request_id": info.get("request_id"),
                              "agent_name": info.get("agent_name"),
                              "attachable": bool(info.get("attachable")),
                              "answered": bool(info.get("answered")),
                              "elsewhere": False}
    _add_sessions_running_elsewhere(wanted, user_id, agent, answer)
    return {"active": answer}


@session_router.get("/{session_id}")
async def get_session(
    session_id: str,
    descendants: bool = False,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
    default_agent=Depends(get_agent_optional),
    tool_registry=Depends(get_tool_registry),
):
    """Get session with messages (authenticated or anonymous).

    ``descendants=true`` adds ``descendants_context_vars``, the expensive half of
    this answer: building it opens EVERY session below this one. On this user's
    store 28 % of sessions have more than two hundred descendants and the largest
    has 3055 -- half a second to two seconds, on every open, for something only
    the Session Info panel shows. So the panel asks for it and nobody else does,
    and opening a session in the chat no longer pays for it.

    Off by default, which is the unusual direction for a field that used to be
    there. The alternative was for the chat to ask for LESS -- and that gives the
    chat's GET a different URL from the DELETE of the same session, which changes
    which of two paths the delete takes (see docs/mid_run_message_injection.md).
    Measured: it failed the test that pins the current one. The panel loads the
    same session at a different URL and that is fine -- it opens nothing.
    """
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = viewer(current_user)

    with session_errors_as_http(session_id, failed_to="get"):
        session = await session_manager.load_session(user_id, session_id)

        # Resolve the session's agent (for the live conversation and its template vars)
        session_agent_name = session.get("agent_name")
        session_agent = None
        if session_agent_name and tool_registry:
            try:
                session_agent = registered_agent(tool_registry, session_agent_name)
            except NotAnAgent:
                logger.debug(f"Session agent '{session_agent_name}' is not an Agent instance, skipping live lookups")
            except KeyError:
                logger.debug(f"Session agent '{session_agent_name}' not found in registry, skipping live lookups")
            except Exception as e:
                logger.warning(f"Failed to get session agent '{session_agent_name}': {e}, skipping live lookups")

        if session_agent is not None and hasattr(session_agent, "_session_tracker"):
            await serve_live_state(session, session_id, session_agent)

        # Build hierarchical descendants tree with their context_vars (live-merged).
        # The Session Info panel uses this so users can see vars set on sub-agent
        # sessions even when the top-level session has none yet (e.g. linear_book
        # in early phases — vars are only set on spawned sub-agents).
        session["descendants_context_vars"] = []
        if descendants:
            try:
                session["descendants_context_vars"] = await _build_descendants_context_vars(
                    session_manager=session_manager,
                    tool_registry=tool_registry,
                    user_id=user_id,
                    root_session_id=session_id,
                )
            except Exception as desc_err:
                logger.debug(f"Could not build descendants tree for {session_id}: {desc_err}")

        # The answers as the models wrote them -- Markdown, which the chat draws.
        return session


@session_router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    session_manager=Depends(get_session_manager),
):
    """Get messages from a session (authenticated only) -- the answers as the models wrote them."""
    with session_errors_as_http(session_id, failed_to="load"):
        return await session_manager.load_session(current_user.username, session_id)


async def _carry_title_to_run(session_id: str, user_id: str, title: str, registry, default_agent,
                              *, waiting_only: bool = False) -> bool:
    """Hand *title* to the run that holds *session_id* -- the caller's own run
    only. Its next save writes it (SessionService), as agent-cli writes a /title
    typed during the first turn once it is saved. ``waiting_only``: only in
    place of a title that run carries already."""
    title = (title or "").strip()
    if not title or default_agent is None:
        return False
    jobs = get_background_job_manager()
    info = (await jobs.active_sessions()).get(session_id)
    if not info or info.get("user_id") != user_id:
        return False
    # A run started without an agent name has "default" on its job
    agent = await resolve_agent_for_request(info.get("request_id"), jobs, registry, default_agent,
                                            info.get("agent_name"))
    tracker = getattr(agent, "_session_tracker", None)
    if tracker is None or (waiting_only and tracker.title_to_write(session_id) is None):
        return False
    tracker.carry_title(session_id, title)
    return True


def _titles_carried(session_id: str, registry, default_agent) -> list:
    """(tracker, title) for every agent of this process that carries a title
    for *session_id* -- from a run that has not written it."""
    agents = [default_agent]  # registered too, unless its name was taken
    for name in (registry.list() if registry is not None else []):
        try:
            agents.append(registry.get(name))
        except KeyError:
            continue
    carried = []
    for agent in agents:
        tracker = getattr(agent, "_session_tracker", None)
        title = tracker.title_to_write(session_id) if hasattr(tracker, "title_to_write") else None
        if isinstance(title, str):
            carried.append((tracker, title))
    return carried


@session_router.put("/{session_id}")
@session_router.patch("/{session_id}")
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
    default_agent=Depends(get_agent_optional),
    tool_registry=Depends(get_tool_registry),
):
    """Update session metadata (title, agent, LLM profile, or tags)."""
    # session_manager injected via dependency

    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = viewer(current_user)

    from agent_system.services.session_manager import SessionNotFoundError

    with session_errors_as_http(session_id, failed_to="update"):
        # Update title if provided
        if request.title is not None:
            # In turn with the session's saves (SessionService.save_lock): a
            # rename landing while one runs is written over by the copy that
            # save loaded -- a /title during a run's first save, or any later.
            session_service = app_state.session_service
            async with (session_service.save_lock(session_id) if session_service is not None
                        else contextlib.nullcontext()):
                # A record can be there before its run's first save (a
                # sub-agent's parent record): the title that run carries would
                # put the old name back with that save.
                await _carry_title_to_run(session_id, user_id, request.title,
                                          tool_registry, default_agent, waiting_only=True)
                # The rename names the session from then on: a title an agent
                # still carries from a run that never wrote it (its first save
                # failed, it ran on another agent) would put an older name back
                # at that agent's next save. The caller's run has this one.
                older = [(tracker, title) for tracker, title in
                         _titles_carried(session_id, tool_registry, default_agent)
                         if title != request.title.strip()]
                try:
                    await session_manager.rename_session(
                        user_id,
                        session_id,
                        request.title
                    )
                except SessionNotFoundError:
                    # Not written yet: its first run is still going -- the run
                    # writes the title with its first save.
                    if not await _carry_title_to_run(session_id, user_id, request.title,
                                                     tool_registry, default_agent):
                        raise
                else:
                    for tracker, title in older:
                        tracker.title_written(session_id, title)  # only if still that one

        # Update other metadata if provided
        metadata_updates = {}
        if request.tags is not None:
            metadata_updates["tags"] = request.tags
        if request.metadata is not None:
            metadata_updates.update(request.metadata)

        if metadata_updates:
            await session_manager.update_session_metadata(
                user_id,
                session_id,
                metadata_updates
            )

        return {"status": "updated", "session_id": session_id}


@session_router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
    create_backup: bool = True
):
    """Delete a session (with optional backup) - works for authenticated and anonymous users.

    Its runs are cancelled too: the session manager writes a deleted session no more, so they would go on for
    nothing -- also those another tab or client follows.
    """
    # session_manager injected via dependency

    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = viewer(current_user)

    with session_errors_as_http(session_id, failed_to="delete"):
        await session_manager.delete_session(
            user_id,
            session_id,
            create_backup=create_backup
        )
        cancelled = await get_background_job_manager().cancel_session(session_id)
        # The person's decision takes the session's file checkpoints with it (file_rewind).
        from agent_system.file_rewind import forget_session_files
        await forget_session_files(user_id, session_id)

        return {"status": "deleted", "session_id": session_id, "cancelled_requests": cancelled}


@session_router.post("/{session_id}/restore")
async def restore_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    session_manager=Depends(get_session_manager),
):
    """Restore a session to the agent for continuation."""
    # session_manager injected via dependency

    with session_errors_as_http(session_id, failed_to="restore"):
        session = await session_manager.load_session(current_user.username, session_id)

        # Return session data for frontend to use in /events call
        return {
            "status": "ready",
            "session_id": session_id,
            "message_count": len(session.get("messages", [])),
            "title": session.get("title")
        }


@session_router.get("/{session_id}/files/{name}")
async def session_file(
    request: Request,
    session_id: str,
    name: str,
    download: bool = False,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """A file an agent handed to the person in this session (services/session_files.py): shown inline (the chat's
    image), or saved with ``download=true``. Only to whoever may see the session; 404 alike for a session that is
    not theirs and a file that is not there."""
    from fastapi.responses import FileResponse

    from agent_system.auth.session_access import may_see_session
    from agent_system.services import session_files

    if (not session_files.URL_PATTERN.match(session_files.url_of(session_id, name))
            or not await may_see_session(request, current_user, session_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such file")
    owner = viewer(current_user)
    if not session_manager.belongs_to(owner, session_id):  # an admin looking at another user's session
        owner = await session_manager.find_session_owner_async(session_id) or owner
    path = session_files.find(session_files.root_beside(session_manager.storage_path), owner, session_id, name)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such file")
    # the file is the agent's, not ours: never sniffed into something else, never run as a page of this origin
    return FileResponse(path, filename=path.name, content_disposition_type="attachment" if download else "inline",
                        headers={"X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"})
