"""
Admin API Endpoints

Provides administrative endpoints for user management.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Any, Dict
import logging

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Response
from pydantic import BaseModel

from agent_system import app_state
from agent_system.api.auth_endpoints import renew_own_login
from agent_system.auth.models import User, UserCreate, UserUpdate, UserRole, public_user
from agent_system.auth.database import get_db, PasswordChangedMeanwhile, UserDatabase
from agent_system.auth.dependencies import require_admin
from agent_system.auth.middleware import (
    AUDIT_CATEGORIES,
    AUDIT_LOG_PATH,
    AUDIT_STATUS_CLASSES,
    SecurityAuditMiddleware,
    security_audit_log,
)


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
    try:
        report = reload_app_config(request.app)
    except ConfigReloadUnavailable as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except ConfigReloadFailed as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info(
        "Admin '%s' reloaded config: %d server(s) refreshed",
        getattr(current_user, "username", "?"), len(report.get("refreshed", [])),
    )
    return ReloadConfigResponse(status="ok", report=report)


class ConfigReloadUnavailable(RuntimeError):
    """The app keeps no config service to reload from."""


class ConfigReloadFailed(ValueError):
    """The config on disk does not load; the running one stays."""


def reload_app_config(app: Any) -> Dict[str, Any]:
    """Re-read the config on disk into *app* and refresh the live plugin instances; the report of
    reload_plugin_configs. Shared by this endpoint and the Setup panel (a key written at runtime)."""
    cfg_service = getattr(app.state, "config_service", None)
    cfg_path = getattr(app.state, "config_path", None)
    if cfg_service is None:
        raise ConfigReloadUnavailable("config reload unavailable (no config service in app state)")
    try:
        fresh = cfg_service.load_config(config_path=cfg_path, force_reload=True)
    except Exception as e:
        # Bad edit on disk: keep the running config untouched, report the error.
        raise ConfigReloadFailed(f"config failed to parse, nothing reloaded: {e}") from e

    from agent_system.services.config_reload import reload_plugin_configs

    # Authentication is set up once, at start: the middleware, the signing key, the plugin route rules. A reloaded
    # auth section would change only what the app says -- who viewer_role takes for an admin, which panels a role
    # is shown -- while the one it started with is enforced: `auth.enabled: false` on disk made every signed-in
    # user an admin viewer. It stays as running until a restart.
    running_auth = app.state.config.auth
    auth_changed = fresh.auth != running_auth
    fresh.auth = running_auth
    report = reload_plugin_configs(fresh)
    if auth_changed:
        report["auth"] = "changed on disk: takes effect on a restart"
        logger.warning("Config reload: the auth section changed on disk and takes effect on a restart only")
    app.state.config = fresh  # subsequent reads see the fresh config
    return report


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
    users = [public_user(u) for u in users_in_db]
    
    logger.debug(f"Admin {admin_user.username} listed users (skip={skip}, limit={limit})")
    
    return UserListResponse(
        users=users,
        # Real total, not the page size -- clients paginate on skip+limit >= total.
        total=db.count_users(),
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
    
    return public_user(user_in_db)


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
        
        return public_user(user_in_db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.patch("/users/{user_id}", response_model=User)
async def update_user(
    user_id: int,
    update_data: UserUpdate,
    request: Request,
    response: Response,
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
        HTTPException: If user not found, or 400 if the email belongs to another user
            or the admin would demote or deactivate themselves
    """
    # Same guard as /demote and /deactivate: this route must not be the way around them.
    if user_id == admin_user.id and (
        update_data.role not in (None, UserRole.ADMIN) or update_data.is_active is False
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot demote or deactivate your own account"
        )
    own_password = user_id == admin_user.id and update_data.password is not None
    # One's own, as PATCH /auth/me: only while this login holds -- a reset from another process since is not
    # overwritten by the login it ended -- and the fresh cookie is for the count this change makes
    generation = request.state.login_generation if own_password else None
    try:
        updated_user = db.update_user(user_id, update_data, expected_generation=generation)
    except PasswordChangedMeanwhile:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The password was changed meanwhile; nothing was saved -- sign in again",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )

    logger.info(f"Admin {admin_user.username} updated user ID {user_id}")
    if own_password:
        renew_own_login(response, updated_user, generation)

    return public_user(updated_user)


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


def _apply_user_update(
    db: UserDatabase,
    user_id: int,
    update_data: UserUpdate,
    admin_user: User,
    did: str,
) -> User:
    """Apply one admin action to an account and return the account as clients see it.

    The common step of /activate, /deactivate, /promote and /demote: a 404
    for an unknown ID, else one log line "Admin <name> <did>".
    """
    updated_user = db.update_user(user_id, update_data)

    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )

    logger.info(f"Admin {admin_user.username} {did}")

    return public_user(updated_user)


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
    return _apply_user_update(
        db, user_id, UserUpdate(is_active=True), admin_user,
        f"activated user ID {user_id}")


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
    
    return _apply_user_update(
        db, user_id, UserUpdate(is_active=False), admin_user,
        f"deactivated user ID {user_id}")


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
    return _apply_user_update(
        db, user_id, UserUpdate(role=UserRole.ADMIN), admin_user,
        f"promoted user ID {user_id} to admin")


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
    
    return _apply_user_update(
        db, user_id, UserUpdate(role=UserRole.USER), admin_user,
        f"demoted user ID {user_id} from admin")


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
    from agent_system.core.request_context import request_user_map as _request_user_map
    from agent_system.servers.agent.server import Agent
    
    sessions = []
    
    try:
        # Iterate through all agents in registry
        for agent_name in app_state.app_registry.list():
            try:
                srv = app_state.app_registry.get(agent_name)
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


@router.get("/system")
async def system_status(
    admin_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Health of this process with the reasons behind it (admin only).

    ``status`` is the worst of ``checks``: servers that did not start (error),
    agent config errors found at start (error), LLMs paused after rate limits
    (warn), a checked-out commit newer than the one the process runs (warn).
    ``/health`` stays the plain liveness probe.
    """
    from agent_system.services.system_status import collect_system_status

    return await collect_system_status()


@router.get(AUDIT_LOG_PATH.removeprefix("/admin"))
async def security_audit(
    audit: SecurityAuditMiddleware = Depends(security_audit_log),
    admin_user: User = Depends(require_admin),
    category: Optional[Literal[AUDIT_CATEGORIES]] = None,
    status_classes: List[Literal[AUDIT_STATUS_CLASSES]] = Query([], alias="status"),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """The newest requests the security audit kept, newest first (admin only; 404 while the audit is off).

    ``status`` may repeat (``?status=4xx&status=5xx``); none means every status.
    """
    return {"entries": audit.get_audit_log(category, status_classes, limit)}
