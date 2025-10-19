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

logger = logging.getLogger(__name__)

session_router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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


# Global session manager reference (injected by build_app)
_session_manager = None
_default_agent = None
_app_registry = None  # Registry to get agents by name


def set_session_manager(manager):
    """Set global session manager instance."""
    global _session_manager
    _session_manager = manager


def set_default_agent(agent):
    """Set global default agent instance for formatting."""
    global _default_agent
    _default_agent = agent


def set_app_registry(registry):
    """Set global app registry for agent lookup."""
    global _app_registry
    _app_registry = registry


@session_router.post("", response_model=Dict[str, str], status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Create a new conversation session (authenticated or anonymous)."""
    if not _session_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized"
        )
    
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"
    
    try:
        session = await _session_manager.create_session(
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
):
    """List all sessions for the current user (or anonymous if not authenticated)."""
    if not _session_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized"
        )
    
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"
    
    try:
        sessions = await _session_manager.list_sessions(user_id)
        
        # Transform to response models
        return [
            SessionResponse(
                session_id=s["session_id"],
                user_id=s["user_id"],
                title=s["title"],
                agent_name=s["agent_name"],
                llm_profile=s["llm_profile"],
                created_at=s["created_at"],
                updated_at=s["updated_at"],
                message_count=s.get("message_count", 0),
                last_agent_response=s.get("last_agent_response"),
                tags=s.get("tags", [])
            )
            for s in sessions
        ]
    
    except Exception as e:
        logger.exception("Failed to list sessions: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_router.get("/{session_id}")
async def get_session(
    session_id: str,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Get a specific session with full conversation history (authenticated or anonymous)."""
    if not _session_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized"
        )
    
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"
    
    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError
        
        session = await _session_manager.load_session(user_id, session_id)
        
        # Format assistant messages to HTML for frontend display using the CORRECT agent from session
        if session.get("messages"):
            # Get the agent that was used in this session
            session_agent_name = session.get("agent_name")
            formatting_agent = None
            
            # Try to get the specific agent from the session
            if session_agent_name and _app_registry:
                try:
                    from agent_system.servers.agent.server import Agent as _Agent
                    session_agent = _app_registry.get(session_agent_name)
                    if isinstance(session_agent, _Agent):
                        formatting_agent = session_agent
                    else:
                        logger.warning(f"Session agent '{session_agent_name}' is not an Agent instance, using default")
                except KeyError:
                    logger.warning(f"Session agent '{session_agent_name}' not found in registry, using default")
                except Exception as e:
                    logger.warning(f"Failed to get session agent '{session_agent_name}': {e}, using default")
            
            # Fallback to default agent if session agent not available
            if not formatting_agent:
                formatting_agent = _default_agent
            
            # Format messages using the correct agent's hooks
            if formatting_agent:
                try:
                    # Format each assistant message using agent's hooks
                    for msg in session["messages"]:
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


@session_router.patch("/{session_id}")
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """Update session metadata (title, tags, etc.) - works for authenticated and anonymous users."""
    if not _session_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized"
        )
    
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"
    
    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError
        
        # Update title if provided
        if request.title is not None:
            await _session_manager.rename_session(
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
            await _session_manager.update_session_metadata(
                user_id,
                session_id,
                **metadata_updates
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
    create_backup: bool = True
):
    """Delete a session (with optional backup) - works for authenticated and anonymous users."""
    if not _session_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized"
        )
    
    # Determine user_id: use username if authenticated, otherwise "anonymous"
    user_id = current_user.username if current_user else "anonymous"
    
    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError
        
        await _session_manager.delete_session(
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
):
    """Restore a session to the agent for continuation."""
    if not _session_manager:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session manager not initialized"
        )
    
    try:
        from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError
        
        session = await _session_manager.load_session(current_user.username, session_id)
        
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
