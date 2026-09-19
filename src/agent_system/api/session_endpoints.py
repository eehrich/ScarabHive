"""
Session Management API Endpoints

Provides CRUD endpoints for managing user conversation sessions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from agent_system.auth.models import User
from agent_system.auth.dependencies import get_current_active_user, get_optional_user
from agent_system.api.dependencies import get_session_manager, get_agent_optional, get_tool_registry
from agent_system.services.background_job_manager import get_background_job_manager

logger = logging.getLogger(__name__)

session_router = APIRouter(prefix="/api/sessions", tags=["sessions"])

#: How many session ids one ``/active`` poll may ask about. The sidebar asks for
#: what it shows -- roots plus the children of expanded nodes -- and a tree that
#: deep is a scrolling problem before it is a polling one. Cut rather than
#: refused: a poll is repeated seconds later anyway.
_ACTIVE_IDS_LIMIT = 200

#: How many files the descendants walk may read at the same time -- session files
#: and sub-indexes alike. The reads run together because they are waiting on disk,
#: not working, but a session file can be megabytes and a tree here can have
#: thousands of nodes: unbounded, one panel's request would decide how much of this
#: process's memory is parsed JSON, and would fill the shared thread pool that every
#: session save also queues in.
_DESCENDANT_READS_AT_ONCE = 16


async def _events_emitted(request_id: Optional[str]) -> Optional[int]:
    """How many events the run behind ``request_id`` has sent so far, or None.

    None for a run with no background job -- a ``/run`` carrying files, a
    sub-agent's run. Those stream inline and cannot be reconnected to anyway, so
    there is nothing for the number to be used for.
    """
    if not request_id:
        return None
    try:
        job = await get_background_job_manager().get_job(request_id)
    except Exception as err:  # noqa: BLE001 -- a missing job manager is not a failed session load
        logger.debug("Could not read the event count for %s: %s", request_id, err)
        return None
    return job.events_emitted if job is not None else None


async def _build_descendants_context_vars(
    session_manager,
    tool_registry,
    user_id: str,
    root_session_id: str,
) -> List[Dict[str, Any]]:
    """Walk the session hierarchy below ``root_session_id`` and collect
    context_vars for every descendant.

    Returns a recursive tree structure with one entry per direct child:

        [
            {
                "session_id": "sub_v5b_story_designer_6850002",
                "agent_name": "v5b_story_designer",
                "context_vars": {...},   # persisted + live-merged from tracker
                "children": [ ... recursive ... ]
            },
            ...
        ]

    For each session the persisted ``context_vars`` are merged with whatever
    the agent's session_tracker currently holds, so live updates show up
    even before the next checkpoint write.

    The topology is walked DOWN from the root, one parent at a time, because a
    sub-index is exactly one parent's children: ``list_child_sessions`` reads
    that one file. It used to come from ``list_sessions``, which merges the main
    index with EVERY per-parent sub-index -- for this user 51 MB across 1303
    files, about a second, to find the handful of ids below one root. Measured
    on that store: 978 ms for a root with two descendants, now under 40 ms.
    """
    if not session_manager:
        return []

    seen: set[str] = {root_session_id}
    reading = asyncio.Semaphore(_DESCENDANT_READS_AT_ONCE)

    async def children_of(parent_id: str) -> List[Dict[str, Any]]:
        """This parent's children -- one sub-index file, not the whole store.

        ``seen`` is not about the tree, which cannot loop: it is about a stale
        sub-index naming a session further up. Walking that would not end.
        """
        try:
            async with reading:
                # Under the same bound as the session files: a sub-index is a file read
                # too, and the biggest one on the user's store is 943 KB -- bigger than
                # most session files. Ungated, a node with a thousand children put a
                # thousand reads into the shared executor in one tick, ahead of whatever
                # a run was trying to save.
                listed = await session_manager.list_child_sessions(
                    user_id, parent_id, annotate_children=False)
        except Exception as err:  # noqa: BLE001 -- one unreadable branch, not a failed load
            # Warning, not debug: this is the only thing between the panel and the
            # truth, and what it hides looks exactly like a session with no children.
            logger.warning("Could not list the sub-sessions of %s: %s", parent_id, err)
            return []
        fresh = []
        for child in listed:
            sid = child.get("session_id")
            if sid and sid not in seen:
                seen.add(sid)
                fresh.append(child)
        return fresh

    async def _load_persisted_vars(sid: str) -> Dict[str, Any]:
        async with reading:
            try:
                data = await session_manager.load_session(user_id, sid)
                cv = data.get("context_vars")
                return cv if isinstance(cv, dict) else {}
            except Exception:
                return {}

    # Cache tool_registry agent lookups so we don't re-resolve per node
    agent_cache: Dict[str, Any] = {}

    def _resolve_agent(name: Optional[str]):
        if not name or not tool_registry:
            return None
        if name in agent_cache:
            return agent_cache[name]
        try:
            from agent_system.servers.agent.server import Agent as _Agent
            resolved = tool_registry.get(name)
            if isinstance(resolved, _Agent):
                agent_cache[name] = resolved
                return resolved
        except Exception:
            pass
        agent_cache[name] = None
        return None

    def _live_merge_vars(agent: Any, sid: str, persisted: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(persisted) if isinstance(persisted, dict) else {}
        if agent is None or not hasattr(agent, "_session_tracker"):
            return merged
        try:
            runtime = agent._session_tracker.get_session_template_vars(sid) or {}
        except Exception:
            runtime = {}
        if runtime:
            merged.update(runtime)
        return merged

    async def _walk(parent_id: str) -> List[Dict[str, Any]]:
        children = await children_of(parent_id)
        if not children:
            return []
        # One node's session file and its own children are independent of its
        # siblings', and every one of them is a file read that spends its time
        # waiting. Run them together: a branch then costs its DEPTH in round
        # trips rather than its size.
        async def node(child: Dict[str, Any]) -> Dict[str, Any]:
            # No guard on a missing session_id: children_of only passes on the ones
            # that have it. The old shape needed the guard, because its children came
            # from a list filtered on the PARENT id.
            sid = child["session_id"]
            agent_name = child.get("agent_name")
            agent = _resolve_agent(agent_name)
            persisted, below = await asyncio.gather(_load_persisted_vars(sid), _walk(sid))
            return {
                "session_id": sid,
                "agent_name": agent_name,
                "context_vars": _live_merge_vars(agent, sid, persisted),
                "children": below,
            }

        return list(await asyncio.gather(*(node(c) for c in children)))

    return await _walk(root_session_id)


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


@session_router.post("", response_model=Dict[str, str], status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Create a new conversation session (authenticated or anonymous)."""
    # session_manager injected via dependency

    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"

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
    user_id = current_user.username if current_user else "anonymous"

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


def _session_node(s: Dict[str, Any]) -> Dict[str, Any]:
    """Index metadata -> sidebar node. ``children`` stays empty: descendants are
    loaded on demand via ``/{session_id}/children``."""
    profile = s.get("llm_profile")
    return {
        "session_id": s.get("session_id"),
        "user_id": s.get("user_id"),
        "title": s.get("title"),
        "agent_name": s.get("agent_name"),
        "llm_profile": profile[0] if isinstance(profile, list) else profile,
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "message_count": s.get("message_count", 0),
        "last_agent_response": s.get("last_agent_response"),
        "tags": s.get("tags", []),
        "depth": s.get("depth", 1),
        "context_vars": s.get("context_vars", {}),
        "has_children": bool(s.get("has_children")),
        "children": [],
    }


@session_router.get("/hierarchy", response_model=Dict[str, Any])
async def list_sessions_hierarchy(
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Top-level sessions for the sidebar — ROOTS only.

    Each root carries ``has_children`` so the UI can render an expand toggle;
    the sub-sessions themselves are fetched on demand via
    ``/{session_id}/children``. Previously this returned the fully nested tree,
    which meant reading every per-parent sub-index and serialising tens of
    thousands of nodes that stay hidden until a node is expanded.
    """
    user_id = current_user.username if current_user else "anonymous"
    try:
        roots = await session_manager.list_root_sessions(user_id)
        return {
            "sessions": [_session_node(s) for s in roots],
            "root_count": len(roots),
        }
    except Exception as e:
        logger.exception("Failed to list sessions hierarchy: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.get("/active", response_model=Dict[str, Any])
async def list_active_sessions(
    ids: str = "",
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
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

    Not reported: a session the run itself creates, until its first turn has
    named it. It has no row in the sidebar yet either, and the chat that started
    it follows its own run directly.
    """
    wanted = [s for s in (i.strip() for i in ids.split(",")) if s][:_ACTIVE_IDS_LIMIT]
    if not wanted:
        return {"active": {}}
    user_id = current_user.username if current_user else "anonymous"
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
                              "answered": bool(info.get("answered"))}
    return {"active": answer}


@session_router.get("/{session_id}/children", response_model=Dict[str, Any])
async def list_session_children(
    session_id: str,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Direct sub-sessions of one parent — reads exactly that parent's sub-index."""
    user_id = current_user.username if current_user else "anonymous"
    try:
        children = await session_manager.list_child_sessions(user_id, session_id)
        return {"sessions": [_session_node(s) for s in children], "count": len(children)}
    except ValueError as e:  # invalid session id
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Failed to list children of %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


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
    user_id = current_user.username if current_user else "anonymous"

    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

        session = await session_manager.load_session(user_id, session_id)

        # Resolve the session's agent (for both formatting and live-var injection)
        session_agent_name = session.get("agent_name")
        session_agent = None
        if session_agent_name and tool_registry:
            try:
                from agent_system.servers.agent.server import Agent as _Agent
                resolved = tool_registry.get(session_agent_name)
                if isinstance(resolved, _Agent):
                    session_agent = resolved
                else:
                    logger.debug(f"Session agent '{session_agent_name}' is not an Agent instance, skipping live lookups")
            except KeyError:
                logger.debug(f"Session agent '{session_agent_name}' not found in registry, skipping live lookups")
            except Exception as e:
                logger.warning(f"Failed to get session agent '{session_agent_name}': {e}, skipping live lookups")

        # A session with a run still in flight: hand out what the run has, not
        # what is on disk. The persisted list ends at the last FINISHED turn --
        # _finalize_request writes at the end of a request -- so opening such a
        # session mid-run showed the history up to that point and then the live
        # stream, with everything in between missing. Same "in-flight means the
        # tracker holds the truth" rule the context_vars merge below applies,
        # and gated on the session lock rather than on the live state being
        # present: that state outlives the run it belongs to.
        #
        # The lock is the gate, and it is a hair narrower than it looks: the run
        # releases it just BEFORE its final save, so for the length of that write
        # this falls back to disk and shows the previous turn. The next load is
        # right. Named rather than papered over -- closing it means moving the
        # release past the save, which is the run's lifecycle, not this endpoint's.
        if session_agent is not None and hasattr(session_agent, "_session_tracker"):
            try:
                is_running, owner_request_id = session_agent._session_tracker.check_session_locked(session_id)
            except Exception as lock_err:
                logger.debug(f"Could not read the session lock for {session_id}: {lock_err}")
                is_running, owner_request_id = False, None
            if is_running:
                try:
                    live = session_agent.get_live_conversation(session_id)
                except Exception as live_err:
                    logger.debug(f"Could not read live messages for {session_id}: {live_err}")
                    live = None
                if live:
                    from agent_system.services.session_service import _msg_to_dict, _add_estimated_tokens
                    messages = [_msg_to_dict(m) for m in live]
                    # The panel sums estimated_tokens and counts how many messages carried
                    # one; the persisted path adds them, so the live one has to as well or
                    # the token figure reads 0 for exactly the sessions worth watching.
                    # Off the loop, as there: the estimator probes media files.
                    await asyncio.to_thread(_add_estimated_tokens, messages)
                    session["messages"] = messages
                    # How much of the run's stream these messages already account for, so a
                    # client attaching next can ask the run to skip just that much. Without
                    # it the reconnect either replays turns the client has (duplicates) or
                    # drops the buffer whole -- which loses whatever the run emitted between
                    # this response and the attach, up to and including its final answer.
                    session["live_events_seen"] = await _events_emitted(owner_request_id)

        # Inject live runtime template_vars from the agent's session tracker.
        # save_session persists context_vars only after messages are committed
        # (see services/session_service.py). For sessions still in-flight (no
        # messages saved yet, e.g. a Pipeline run currently in phase 3) the
        # tracker holds the truth — without this merge the Session Info panel
        # shows "No context variables set" while the run is active.
        if session_agent is not None and hasattr(session_agent, "_session_tracker"):
            try:
                runtime_vars = session_agent._session_tracker.get_session_template_vars(session_id)
            except Exception as tracker_err:
                logger.debug(f"Could not read runtime template_vars for {session_id}: {tracker_err}")
                runtime_vars = None
            if runtime_vars:
                existing = session.get("context_vars")
                if not isinstance(existing, dict):
                    existing = {}
                merged = {**existing, **runtime_vars}
                session["context_vars"] = merged

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

        # Format assistant messages to HTML for frontend display
        if session.get("messages"):
            # Only format if we found the exact session agent (no fallback to avoid wrong hook settings)
            formatting_agent = session_agent
            if formatting_agent:
                try:
                    # CRITICAL: Create a COPY of messages for formatting to avoid modifying stored session
                    # The session dict is loaded from storage and modifications would persist on next load
                    import copy
                    formatted_messages = copy.deepcopy(session["messages"])

                    # Format each assistant message using agent's hooks
                    for msg in formatted_messages:
                        if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls"):
                            # Only format if not already formatted
                            if not msg.get("content_format") or msg.get("content_format") != "html":
                                try:
                                    formatted_content, content_format = await formatting_agent._hook_manager.execute_format_output_hooks(
                                        output=msg["content"],
                                        request_id="session_load",
                                        session_id=session_id,
                                        output_format='html'
                                    )
                                    msg["content"] = formatted_content
                                    msg["content_format"] = content_format
                                except Exception as format_error:
                                    logger.warning(f"Failed to format message in session {session_id}: {format_error}")
                                    # Keep original content if formatting fails
                                    msg["content_format"] = "text"

                    # Replace session messages with formatted copy (only affects this HTTP response)
                    session["messages"] = formatted_messages

                except Exception as hook_error:
                    logger.warning(f"Failed to access hooks for formatting session {session_id}: {hook_error}")
                    # Return session without formatting if hook access fails

        return session

    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
    except SessionPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    except Exception as e:
        logger.exception("Failed to get session %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    session_manager=Depends(get_session_manager),
    default_agent=Depends(get_agent_optional),
    tool_registry=Depends(get_tool_registry),
):
    """Get messages from a session (authenticated only)."""
    # session_manager, default_agent, tool_registry injected via dependency

    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

        session = await session_manager.load_session(current_user.username, session_id)

        # Format assistant messages to HTML for frontend display using the CORRECT agent from session
        if session.get("messages"):
            # Get the agent that was used in this session
            session_agent_name = session.get("agent_name")
            formatting_agent = None

            # Try to get the specific agent from the session
            # IMPORTANT: Do NOT fallback to default_agent if session agent not found!
            # Different agents have different hook configurations (e.g., markdown_formatter enabled/disabled).
            # Using a different agent's hooks would apply wrong formatting settings.
            if session_agent_name and tool_registry:
                try:
                    from agent_system.servers.agent.server import Agent as _Agent
                    session_agent = tool_registry.get(session_agent_name)
                    if isinstance(session_agent, _Agent):
                        formatting_agent = session_agent
                    else:
                        logger.debug(f"Session agent '{session_agent_name}' is not an Agent instance, skipping formatting")
                except KeyError:
                    logger.debug(f"Session agent '{session_agent_name}' not found in registry, skipping formatting")
                except Exception as e:
                    logger.warning(f"Failed to get session agent '{session_agent_name}': {e}, skipping formatting")

            # Only format if we found the exact session agent (no fallback to avoid wrong hook settings)
            if formatting_agent:
                try:
                    # CRITICAL: Create a COPY of messages for formatting to avoid modifying stored session
                    # The session dict is loaded from storage and modifications would persist on next load
                    import copy
                    formatted_messages = copy.deepcopy(session["messages"])

                    # Format each assistant message using agent's hooks
                    for msg in formatted_messages:
                        if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls"):
                            # Only format if not already formatted
                            if not msg.get("content_format") or msg.get("content_format") != "html":
                                try:
                                    formatted_content, content_format = await formatting_agent._hook_manager.execute_format_output_hooks(
                                        output=msg["content"],
                                        request_id="session_load",
                                        session_id=session_id,
                                        output_format='html'
                                    )
                                    msg["content"] = formatted_content
                                    msg["content_format"] = content_format
                                except Exception as format_error:
                                    logger.warning(f"Failed to format message in session {session_id}: {format_error}")
                                    # Keep original content if formatting fails
                                    msg["content_format"] = "text"

                    # Replace session messages with formatted copy (only affects this HTTP response)
                    session["messages"] = formatted_messages

                except Exception as hook_error:
                    logger.warning(f"Failed to access hooks for formatting session {session_id}: {hook_error}")
                    # Return session without formatting if hook access fails

        return session

    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
    except SessionPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    except Exception as e:
        logger.exception("Failed to load session %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.put("/{session_id}")
@session_router.patch("/{session_id}")
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """Update session metadata (title, agent, LLM profile, or tags)."""
    # session_manager injected via dependency

    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"

    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

        # Update title if provided
        if request.title is not None:
            await session_manager.rename_session(
                user_id,
                session_id,
                request.title
            )

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

    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
    except SessionPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    except Exception as e:
        logger.exception("Failed to update session %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


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
    user_id = current_user.username if current_user else "anonymous"

    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

        await session_manager.delete_session(
            user_id,
            session_id,
            create_backup=create_backup
        )
        cancelled = await get_background_job_manager().cancel_session(session_id)

        return {"status": "deleted", "session_id": session_id, "cancelled_requests": cancelled}

    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
    except SessionPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    except Exception as e:
        logger.exception("Failed to delete session %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.post("/{session_id}/restore")
async def restore_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    session_manager=Depends(get_session_manager),
):
    """Restore a session to the agent for continuation."""
    # session_manager injected via dependency

    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

        session = await session_manager.load_session(current_user.username, session_id)

        # Return session data for frontend to use in /events call
        return {
            "status": "ready",
            "session_id": session_id,
            "message_count": len(session.get("messages", [])),
            "title": session.get("title")
        }

    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
    except SessionPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    except Exception as e:
        logger.exception("Failed to restore session %s: %s", session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
