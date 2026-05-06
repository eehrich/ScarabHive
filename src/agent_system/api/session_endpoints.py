"""
Session Management API Endpoints

Provides CRUD endpoints for managing user conversation sessions.
"""

from __future__ import annotations

import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from agent_system.auth.models import User
from agent_system.auth.dependencies import get_current_active_user, get_optional_user
from agent_system.api.dependencies import get_session_manager, get_agent_optional, get_mcp_registry

logger = logging.getLogger(__name__)

session_router = APIRouter(prefix="/api/sessions", tags=["sessions"])


async def _build_descendants_context_vars(
    session_manager,
    mcp_registry,
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
    """
    if not session_manager:
        return []

    # The list_sessions index does not include context_vars, so we list once
    # to get the parent->children topology, then load each descendant's full
    # session file to read its persisted context_vars.
    sessions = await session_manager.list_sessions(user_id)
    children_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for entry in sessions:
        parent_info = entry.get("parent_session")
        if not isinstance(parent_info, dict):
            continue
        parent_id = parent_info.get("session_id")
        if not parent_id:
            continue
        children_by_parent.setdefault(parent_id, []).append(entry)

    async def _load_persisted_vars(sid: str) -> Dict[str, Any]:
        try:
            data = await session_manager.load_session(user_id, sid)
            cv = data.get("context_vars")
            return cv if isinstance(cv, dict) else {}
        except Exception:
            return {}

    # Cache mcp_registry agent lookups so we don't re-resolve per node
    agent_cache: Dict[str, Any] = {}

    def _resolve_agent(name: Optional[str]):
        if not name or not mcp_registry:
            return None
        if name in agent_cache:
            return agent_cache[name]
        try:
            from agent_system.servers.agent.server import Agent as _Agent
            resolved = mcp_registry.get(name)
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
        out: List[Dict[str, Any]] = []
        for child in children_by_parent.get(parent_id, []):
            sid = child.get("session_id")
            if not sid:
                continue
            agent_name = child.get("agent_name")
            agent = _resolve_agent(agent_name)
            persisted = await _load_persisted_vars(sid)
            merged = _live_merge_vars(agent, sid, persisted)
            out.append({
                "session_id": sid,
                "agent_name": agent_name,
                "context_vars": merged,
                "children": await _walk(sid),
            })
        return out

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

        # Filter out sub-agent sessions (they have parent_session field)
        # Sub-agent sessions should only be visible under their parent, not in the main list
        top_level_sessions = [s for s in sessions if "parent_session" not in s]

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


@session_router.get("/hierarchy", response_model=Dict[str, Any])
async def list_sessions_hierarchy(
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
):
    """List sessions in hierarchical structure based on parent_session field."""
    user_id = current_user.username if current_user else "anonymous"

    try:
        sessions = await session_manager.list_sessions(user_id)
        
        # Build map: session_id -> session data
        session_map = {s["session_id"]: s for s in sessions}
        
        # Build parent-child relationships from parent_session field
        # parent_map: child_session_id -> parent_session_id
        parent_map: Dict[str, str] = {}
        children_map: Dict[str, List[str]] = {}  # parent_id -> [child_ids]
        
        for session in sessions:
            session_id = session["session_id"]
            parent_info = session.get("parent_session")
            
            if parent_info and isinstance(parent_info, dict):
                parent_id = parent_info.get("session_id")
                if parent_id:
                    parent_map[session_id] = parent_id
                    if parent_id not in children_map:
                        children_map[parent_id] = []
                    children_map[parent_id].append(session_id)
        
        # Build hierarchical structure: root sessions (no parent) with nested children
        def build_session_node(session: Dict[str, Any]) -> Dict[str, Any]:
            """Build session node with children recursively."""
            session_id = session["session_id"]
            
            # Transform to response format
            node = {
                "session_id": session_id,
                "user_id": session["user_id"],
                "title": session["title"],
                "agent_name": session["agent_name"],
                "llm_profile": session["llm_profile"][0] if isinstance(session["llm_profile"], list) else session["llm_profile"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "message_count": session.get("message_count", 0),
                "last_agent_response": session.get("last_agent_response"),
                "tags": session.get("tags", []),
                "depth": session.get("depth", 0),
                "context_vars": session.get("context_vars", {}),  # Include context_vars for phase info
                "children": []
            }
            
            # Add children recursively
            child_ids = children_map.get(session_id, [])
            for child_id in child_ids:
                if child_id in session_map:
                    child_session = session_map[child_id]
                    child_node = build_session_node(child_session)
                    node["children"].append(child_node)
            
            return node
        
        # Find root sessions (sessions without a parent)
        root_sessions = []
        for session in sessions:
            session_id = session["session_id"]
            if session_id not in parent_map:
                # This is a root session
                root_sessions.append(build_session_node(session))
        
        # Sort root sessions by updated_at (most recent first)
        root_sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        
        return {
            "sessions": root_sessions,
            "total_count": len(sessions),
            "root_count": len(root_sessions)
        }

    except Exception as e:
        logger.exception("Failed to list sessions hierarchy: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.get("/{session_id}")
async def get_session(
    session_id: str,
    current_user: Optional[User] = Depends(get_optional_user),
    session_manager=Depends(get_session_manager),
    default_agent=Depends(get_agent_optional),
    mcp_registry=Depends(get_mcp_registry),
):
    """Get session with messages (authenticated or anonymous)."""
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"

    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

        session = await session_manager.load_session(user_id, session_id)

        # Resolve the session's agent (for both formatting and live-var injection)
        session_agent_name = session.get("agent_name")
        session_agent = None
        if session_agent_name and mcp_registry:
            try:
                from agent_system.servers.agent.server import Agent as _Agent
                resolved = mcp_registry.get(session_agent_name)
                if isinstance(resolved, _Agent):
                    session_agent = resolved
                else:
                    logger.debug(f"Session agent '{session_agent_name}' is not an Agent instance, skipping live lookups")
            except KeyError:
                logger.debug(f"Session agent '{session_agent_name}' not found in registry, skipping live lookups")
            except Exception as e:
                logger.warning(f"Failed to get session agent '{session_agent_name}': {e}, skipping live lookups")

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
        try:
            session["descendants_context_vars"] = await _build_descendants_context_vars(
                session_manager=session_manager,
                mcp_registry=mcp_registry,
                user_id=user_id,
                root_session_id=session_id,
            )
        except Exception as desc_err:
            logger.debug(f"Could not build descendants tree for {session_id}: {desc_err}")
            session["descendants_context_vars"] = []

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
    mcp_registry=Depends(get_mcp_registry),
):
    """Get messages from a session (authenticated only)."""
    # session_manager, default_agent, mcp_registry injected via dependency

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
            if session_agent_name and mcp_registry:
                try:
                    from agent_system.servers.agent.server import Agent as _Agent
                    session_agent = mcp_registry.get(session_agent_name)
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
    """Delete a session (with optional backup) - works for authenticated and anonymous users."""
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

        return {"status": "deleted", "session_id": session_id}

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
