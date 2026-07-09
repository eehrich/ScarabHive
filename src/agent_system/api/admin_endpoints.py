"""
Admin API Endpoints

Provides administrative endpoints for user management.
"""

from __future__ import annotations

from typing import List, Optional, Any, Dict
import logging

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from pydantic import BaseModel

from agent_system.auth.models import User, UserCreate, UserUpdate, UserRole
from agent_system.auth.database import get_db, UserDatabase
from agent_system.auth.dependencies import require_admin


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


class UserListResponse(BaseModel):
    """User list response with pagination."""
    users: List[User]
    total: int
    skip: int
    limit: int


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str
    detail: Optional[str] = None


class ReloadConfigResponse(BaseModel):
    """Result of a deliberate config reload."""
    status: str
    report: Dict[str, Any]


@router.post("/reload-config", response_model=ReloadConfigResponse)
async def reload_config_endpoint(
    request: Request,
    current_user: User = Depends(require_admin),
):
    """Deliberately re-read the on-disk config and refresh live plugin instances
    (no restart, no dropped sessions/jobs).

    Only servers implementing ``reload_config()`` are refreshed (e.g. a sub-agent
    manager's ``allowed_agents`` / limits). Adding a brand-new agent/plugin
    definition still requires a restart. There is no file watcher — this is the
    explicit trigger (also reachable via ``agent-cli reload``).
    """
    cfg_service = getattr(request.app.state, "config_service", None)
    cfg_path = getattr(request.app.state, "config_path", None)
    if cfg_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="config reload unavailable (no config service in app state)",
        )

    try:
        fresh = cfg_service.load_config(config_path=cfg_path, force_reload=True)
    except Exception as e:
        # Bad edit on disk: keep the running config untouched, report the error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"config failed to parse, nothing reloaded: {e}",
        )

    from agent_system.services.config_reload import reload_plugin_configs

    report = reload_plugin_configs(fresh)
    request.app.state.config = fresh  # subsequent reads see the fresh config
    logger.info(
        "Admin '%s' reloaded config: %d server(s) refreshed",
        getattr(current_user, "username", "?"), len(report.get("refreshed", [])),
    )
    return ReloadConfigResponse(status="ok", report=report)


@router.get("/users", response_model=UserListResponse)
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> UserListResponse:
    """
    List all users (admin only).
    
    Args:
        skip: Number of users to skip
        limit: Maximum number of users to return
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        List of users with pagination info
    """
    users_in_db = db.list_users(skip=skip, limit=limit)
    
    # Convert to User models (remove sensitive data)
    users = [
        User(
            id=u.id,
            username=u.username,
            email=u.email,
            full_name=u.full_name,
            is_active=u.is_active,
            role=u.role,
            created_at=u.created_at,
            updated_at=u.updated_at,
            last_login=u.last_login,
        )
        for u in users_in_db
    ]
    
    logger.debug(f"Admin {admin_user.username} listed users (skip={skip}, limit={limit})")
    
    return UserListResponse(
        users=users,
        total=len(users),
        skip=skip,
        limit=limit,
    )


@router.get("/users/{user_id}", response_model=User)
async def get_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Get user by ID (admin only).
    
    Args:
        user_id: User ID
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        User data
    
    Raises:
        HTTPException: If user not found
    """
    user_in_db = db.get_user_by_id(user_id)
    
    if not user_in_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )
    
    logger.debug(f"Admin {admin_user.username} retrieved user {user_in_db.username}")
    
    return User(
        id=user_in_db.id,
        username=user_in_db.username,
        email=user_in_db.email,
        full_name=user_in_db.full_name,
        is_active=user_in_db.is_active,
        role=user_in_db.role,
        created_at=user_in_db.created_at,
        updated_at=user_in_db.updated_at,
        last_login=user_in_db.last_login,
    )


@router.post("/users", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user_admin(
    user_data: UserCreate,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Create a new user (admin only).
    
    Args:
        user_data: User creation data
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Created user
    
    Raises:
        HTTPException: If username or email already exists
    """
    try:
        user_in_db = db.create_user(user_data)
        logger.info(f"Admin {admin_user.username} created user: {user_in_db.username}")
        
        return User(
            id=user_in_db.id,
            username=user_in_db.username,
            email=user_in_db.email,
            full_name=user_in_db.full_name,
            is_active=user_in_db.is_active,
            role=user_in_db.role,
            created_at=user_in_db.created_at,
            updated_at=user_in_db.updated_at,
            last_login=user_in_db.last_login,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.patch("/users/{user_id}", response_model=User)
async def update_user(
    user_id: int,
    update_data: UserUpdate,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Update a user (admin only).
    
    Args:
        user_id: User ID to update
        update_data: Update data
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Updated user
    
    Raises:
        HTTPException: If user not found
    """
    updated_user = db.update_user(user_id, update_data)
    
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )
    
    logger.info(f"Admin {admin_user.username} updated user ID {user_id}")
    
    return User(
        id=updated_user.id,
        username=updated_user.username,
        email=updated_user.email,
        full_name=updated_user.full_name,
        is_active=updated_user.is_active,
        role=updated_user.role,
        created_at=updated_user.created_at,
        updated_at=updated_user.updated_at,
        last_login=updated_user.last_login,
    )


@router.delete("/users/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> MessageResponse:
    """
    Delete a user (admin only).
    
    Args:
        user_id: User ID to delete
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Success message
    
    Raises:
        HTTPException: If user not found or trying to delete self
    """
    # Prevent admin from deleting themselves
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )
    
    if db.delete_user(user_id):
        logger.info(f"Admin {admin_user.username} deleted user ID {user_id}")
        return MessageResponse(
            message=f"User {user_id} deleted successfully"
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )


@router.post("/users/{user_id}/activate", response_model=User)
async def activate_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Activate a user account (admin only).
    
    Args:
        user_id: User ID to activate
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Updated user
    """
    update_data = UserUpdate(is_active=True)
    updated_user = db.update_user(user_id, update_data)
    
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )
    
    logger.info(f"Admin {admin_user.username} activated user ID {user_id}")
    
    return User(
        id=updated_user.id,
        username=updated_user.username,
        email=updated_user.email,
        full_name=updated_user.full_name,
        is_active=updated_user.is_active,
        role=updated_user.role,
        created_at=updated_user.created_at,
        updated_at=updated_user.updated_at,
        last_login=updated_user.last_login,
    )


@router.post("/users/{user_id}/deactivate", response_model=User)
async def deactivate_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Deactivate a user account (admin only).
    
    Args:
        user_id: User ID to deactivate
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Updated user
    
    Raises:
        HTTPException: If trying to deactivate self
    """
    # Prevent admin from deactivating themselves
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account"
        )
    
    update_data = UserUpdate(is_active=False)
    updated_user = db.update_user(user_id, update_data)
    
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )
    
    logger.info(f"Admin {admin_user.username} deactivated user ID {user_id}")
    
    return User(
        id=updated_user.id,
        username=updated_user.username,
        email=updated_user.email,
        full_name=updated_user.full_name,
        is_active=updated_user.is_active,
        role=updated_user.role,
        created_at=updated_user.created_at,
        updated_at=updated_user.updated_at,
        last_login=updated_user.last_login,
    )


@router.post("/users/{user_id}/promote", response_model=User)
async def promote_to_admin(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Promote a user to admin (admin only).
    
    Args:
        user_id: User ID to promote
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Updated user
    """
    update_data = UserUpdate(role=UserRole.ADMIN)
    updated_user = db.update_user(user_id, update_data)
    
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )
    
    logger.info(f"Admin {admin_user.username} promoted user ID {user_id} to admin")
    
    return User(
        id=updated_user.id,
        username=updated_user.username,
        email=updated_user.email,
        full_name=updated_user.full_name,
        is_active=updated_user.is_active,
        role=updated_user.role,
        created_at=updated_user.created_at,
        updated_at=updated_user.updated_at,
        last_login=updated_user.last_login,
    )


@router.post("/users/{user_id}/demote", response_model=User)
async def demote_from_admin(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Demote a user from admin to regular user (admin only).
    
    Args:
        user_id: User ID to demote
        admin_user: Current admin user
        db: Database instance
    
    Returns:
        Updated user
    
    Raises:
        HTTPException: If trying to demote self
    """
    # Prevent admin from demoting themselves
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot demote your own account"
        )
    
    update_data = UserUpdate(role=UserRole.USER)
    updated_user = db.update_user(user_id, update_data)
    
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )
    
    logger.info(f"Admin {admin_user.username} demoted user ID {user_id} from admin")
    
    return User(
        id=updated_user.id,
        username=updated_user.username,
        email=updated_user.email,
        full_name=updated_user.full_name,
        is_active=updated_user.is_active,
        role=updated_user.role,
        created_at=updated_user.created_at,
        updated_at=updated_user.updated_at,
        last_login=updated_user.last_login,
    )


class ActiveSessionInfo(BaseModel):
    """Information about an active session/request."""
    request_id: str
    user_id: str
    agent_name: str
    session_id: Optional[str] = None
    duration_seconds: float
    status: str  # running, cancelling
    llm_profile: Optional[str] = None


class ActiveSessionsResponse(BaseModel):
    """Response containing active sessions."""
    sessions: List[ActiveSessionInfo]
    total: int


@router.get("/active-sessions", response_model=ActiveSessionsResponse)
async def list_active_sessions(
    admin_user: User = Depends(require_admin),
) -> ActiveSessionsResponse:
    """
    List all currently active agent sessions (admin only).
    
    Returns active requests across all agents with:
    - Request ID (for cancellation)
    - User running the request
    - Agent name
    - Session ID
    - Duration (how long it's been running)
    - Status (running/cancelling)
    - LLM profile
    
    Args:
        admin_user: Current admin user (verified by require_admin)
    
    Returns:
        List of active sessions with details
    """
    # Import here to avoid circular imports
    from agent_system.app import _request_user_map, _app_registry
    from agent_system.servers.agent.server import Agent
    
    sessions = []
    
    try:
        # Iterate through all agents in registry
        for agent_name in _app_registry.list():
            try:
                srv = _app_registry.get(agent_name)
                if not isinstance(srv, Agent):
                    continue
                
                # Get active requests from this agent
                active_requests_info = srv._request_manager.get_active_requests_info()
                
                for req_info in active_requests_info:
                    request_id = req_info['request_id']
                    
                    # Get user_id from the global map
                    user_id = _request_user_map.get(request_id, "unknown")
                    
                    # Get session_id and metadata from session tracker
                    session_id = srv._session_tracker.get_session_for_request(request_id)
                    session_metadata = {}
                    if session_id:
                        session_metadata = srv._session_tracker.get_session_metadata(session_id) or {}
                    
                    # Determine status
                    status_str = "cancelling" if req_info.get('is_cancelled', False) else "running"
                    
                    sessions.append(ActiveSessionInfo(
                        request_id=request_id,
                        user_id=user_id,
                        agent_name=agent_name,
                        session_id=session_id,
                        duration_seconds=req_info.get('duration_seconds', 0),
                        status=status_str,
                        llm_profile=session_metadata.get('llm_profile')
                    ))
                    
            except Exception as e:
                logger.debug(f"Failed to get active requests from agent {agent_name}: {e}")
                continue
                
    except Exception as e:
        logger.exception(f"Failed to list active sessions: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list active sessions: {str(e)}"
        )
    
    # Sort by duration (longest first)
    sessions.sort(key=lambda s: s.duration_seconds, reverse=True)
    
    logger.info(f"Admin {admin_user.username} listed {len(sessions)} active sessions")
    
    return ActiveSessionsResponse(
        sessions=sessions,
        total=len(sessions)
    )


@router.post("/active-sessions/{request_id}/cancel")
async def cancel_active_session(
    request_id: str,
    admin_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Cancel an active session by request ID (admin only).

    Delegates to the canonical ``BackgroundJobManager.cancel_job`` path
    that walks the agent registry + sets the cancellation token + force-
    cancels the task after the grace period. The previously-duplicated
    walk-the-registry loop lived here AND in ``/api/requests/{rid}/
    cancel`` AND was the only one that worked correctly for sub-agent
    requests — consolidated 2026-06-27 so there's a single
    implementation everyone shares.

    Args:
        request_id: The request ID to cancel
        admin_user: Current admin user (verified by require_admin)

    Returns:
        Status of the cancellation
    """
    from agent_system.services.background_job_manager import (
        get_background_job_manager,
    )

    try:
        success = await get_background_job_manager().cancel_job(
            request_id, force_timeout=5.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Admin %s: cancel for request_id=%s failed: %s",
            admin_user.username, request_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel request: {exc}",
        )

    if success:
        logger.info(
            "Admin %s cancelled request %s",
            admin_user.username, request_id,
        )
        return {"status": "cancelled", "request_id": request_id}
    return {
        "status": "not_found",
        "request_id": request_id,
        "message": "Request not found or already completed",
    }
