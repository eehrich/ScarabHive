"""
Authentication API Endpoints

Provides user registration, login, logout, and authentication management endpoints.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
import functools
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status, Response
from pydantic import BaseModel

from agent_system.auth.models import (
    User,
    UserCreate,
    UserRegister,
    UserRole,
    UserPreferences,
    UserSelfUpdate,
    UserUpdate,
    Token,
    LoginRequest,
    RefreshTokenRequest,
    PasswordResetRequest,
    PasswordReset,
    APIKeyResponse,
    public_user,
)
from agent_system.auth.database import get_db, PasswordChangedMeanwhile, UserDatabase
from agent_system.config.models import RegistrationConfig
# Module import on purpose: the expiry values are rebound by
# set_jwt_config() at startup. A from-import copies the value at import
# time, so tokens were issued with the module DEFAULTS (7d/30d) instead of
# the configured lifetimes. Read them via the module attribute at call time.
from agent_system.auth import security as _security
from agent_system.auth.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    decode_access_token,
)
from agent_system.auth.dependencies import get_current_active_user


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["authentication"])


@functools.cache
def _unknown_user_hash() -> str:
    """A hash to check against when the account does not exist, so that answer costs as long as a wrong password."""
    return get_password_hash(secrets.token_urlsafe(16))


def _claims(user, generation: int) -> dict:
    """What a token of this account says: who, which account, the role, and the password generation it is valid for.

    The generation is the one the caller read BEFORE it checked the credentials: read after, a password changed in
    between -- by another process, the CLI -- would pass its new count to a login the change has already ended.
    """
    return {"sub": user.username, "user_id": user.id, "role": user.role.value, "gen": generation}


def _set_session_cookie(response: Response, access_token: str) -> None:
    """The browser's login: the access token as an HttpOnly cookie."""
    # Use both max_age and expires for maximum browser compatibility
    cookie_max_age = _security.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,  # Secure: JavaScript cannot access
        secure=False,   # Allow HTTP for local development (set True in production)
        samesite="lax",  # CSRF protection
        max_age=cookie_max_age,
        expires=cookie_max_age,  # also set expires for older browsers
    )


def renew_own_login(response: Response, user, generation: int) -> None:
    """A password change ends every login made before; the browser that changed its own account's stays in.

    ``generation`` is the account's count read BEFORE the change: the cookie is valid for the count this change
    makes, so a change another process added meanwhile (an admin's reset from the CLI) ends this login as well.
    """
    _set_session_cookie(response, create_access_token(
        data=_claims(user, generation + 1),
        expires_delta=timedelta(minutes=_security.ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type="access",
    ))


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str
    detail: Optional[str] = None


def registration_settings(request: Request) -> RegistrationConfig:
    """auth.registration of the running app; the defaults when it carries no config."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    settings = getattr(auth, "registration", None)
    return settings if isinstance(settings, RegistrationConfig) else RegistrationConfig()


@router.post("/register", response_model=User, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserRegister,
    db: UserDatabase = Depends(get_db),
    settings: RegistrationConfig = Depends(registration_settings),
) -> User:
    """
    Register a new user, as auth.registration says: refused when it is disabled,
    created inactive when an admin has to approve it, with its default role.

    Reachable without login, so the caller chooses nothing about its own
    privileges: role and is_active are not part of the request (422).

    Raises:
        HTTPException: If username or email already exists
    """
    if not settings.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Self-registration is disabled on this server")
    try:
        user_in_db = db.create_user(UserCreate(
            **user_data.model_dump(), role=UserRole(settings.default_role),
            is_active=not settings.require_approval))
        logger.info(f"User registered: {user_in_db.username}")

        # Convert to User (remove sensitive data)
        return public_user(user_in_db)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/login", response_model=Token)
async def login(
    login_data: LoginRequest,
    response: Response,
    db: UserDatabase = Depends(get_db),
) -> Token:
    """
    Login and get access token.

    Accepts either username or email for login.
    Sets both JSON response and HttpOnly cookie for compatibility.

    Args:
        login_data: Login credentials (username or email + password)
        response: FastAPI response object to set cookie
        db: Database instance

    Returns:
        JWT access token

    Raises:
        HTTPException: If credentials are invalid
    """
    # Get user from database - try username first, then email
    user = db.get_user_by_username(login_data.username)
    if not user:
        # Try as email if username lookup failed
        user = db.get_user_by_email(login_data.username)
    # the generation first, then the hash checked against it (_claims)
    generation = db.token_generation(user.id) if user else 0
    user = user and db.get_user_by_id(user.id)

    # Verify credentials
    if not user:
        verify_password(login_data.password, _unknown_user_hash())
    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive -- a new account may still need an administrator's activation"
        )

    # Create access token
    access_token_expires = timedelta(minutes=_security.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data=_claims(user, generation),
        expires_delta=access_token_expires,
        token_type="access",
    )

    # Create refresh token
    refresh_token_expires = timedelta(days=_security.REFRESH_TOKEN_EXPIRE_DAYS)
    refresh_token = create_access_token(
        data=_claims(user, generation),
        expires_delta=refresh_token_expires,
        token_type="refresh",
    )

    # Update last login
    db.update_last_login(user.id)

    # Set secure cookie for browser clients
    _set_session_cookie(response, access_token)

    logger.info(f"User logged in: {user.username}")

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=_security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    response: Response,
    current_user: User = Depends(get_current_active_user),
) -> MessageResponse:
    """
    Logout (removes HttpOnly cookie and returns success message).

    Note: JWT tokens are stateless, so logout is handled by:
    1. Removing the HttpOnly cookie (server-side)
    2. Client should also clear localStorage/sessionStorage tokens

    Args:
        response: FastAPI response object to delete cookie
        current_user: Current authenticated user

    Returns:
        Success message
    """
    # Delete the cookie
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=False,
        samesite="lax"
    )

    logger.info(f"User logged out: {current_user.username}")
    return MessageResponse(
        message="Logged out successfully",
        detail="Remove the token from your client"
    )


@router.post("/refresh", response_model=Token)
async def refresh_token(
    refresh_request: RefreshTokenRequest,
    db: UserDatabase = Depends(get_db),
) -> Token:
    """
    Refresh access token using refresh token.

    Args:
        refresh_request: Refresh token request with refresh_token
        db: Database instance

    Returns:
        New access token (and optionally new refresh token)

    Raises:
        HTTPException: If refresh token is invalid or expired
    """
    # Decode and validate refresh token
    token_data = decode_access_token(refresh_request.refresh_token)

    if not token_data or token_data.token_type != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user from database
    user = db.get_user_by_username(token_data.username)
    if not user or user.id != token_data.user_id or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    # issued before the password changed: it would mint fresh tokens for a login that ended
    if token_data.generation != db.token_generation(user.id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create new access token
    access_token_expires = timedelta(minutes=_security.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data=_claims(user, token_data.generation),  # the count just checked, not one read again
        expires_delta=access_token_expires,
        token_type="access",
    )

    # Optionally create new refresh token (rotation)
    refresh_token_expires = timedelta(days=_security.REFRESH_TOKEN_EXPIRE_DAYS)
    new_refresh_token = create_access_token(
        data=_claims(user, token_data.generation),
        expires_delta=refresh_token_expires,
        token_type="refresh",
    )

    logger.info(f"Token refreshed for user: {user.username}")

    return Token(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=_security.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=User)
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Get current user information.

    Args:
        current_user: Current authenticated user

    Returns:
        Current user data
    """
    return current_user


@router.get("/me/preferences", response_model=UserPreferences)
async def get_my_preferences(
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
):
    """The signed-in user's display preferences, every key filled in."""
    return db.get_preferences(current_user.id)


@router.put("/me/preferences", response_model=UserPreferences)
async def put_my_preferences(
    preferences: UserPreferences,
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
):
    """Replace the signed-in user's display preferences (the whole object: what is left out is the default)."""
    return db.set_preferences(current_user.id, preferences)


@router.patch("/me", response_model=User)
async def update_current_user(
    user_update: UserSelfUpdate,
    response: Response,
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Update the current user's email, full name or password.

    Role and is_active are not accepted here (422) -- that is the admin
    endpoints' job. Changing the password requires ``current_password``, and
    ends every login made before, the account's API key included; the browser
    that changed it gets a new cookie and stays in (a client holding its token
    logs in again).

    Raises:
        HTTPException: 403 on a wrong current password, 400 if it is missing,
            409 if the password was changed elsewhere after the request came in.
    """
    try:
        # the generation first, then the hash checked against it, as at login; the change below is written only
        # while it still holds (a reset by an admin meanwhile wins, and this request gets a 409)
        generation = db.token_generation(current_user.id)
        user_in_db = db.get_user_by_id(current_user.id)
        if not user_in_db:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        if user_update.password is not None:
            if not user_update.current_password:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="current_password is required to change the password"
                )
            if not verify_password(user_update.current_password, user_in_db.hashed_password):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Current password is incorrect"
                )

        try:
            updated_user = db.update_user(current_user.id, UserUpdate(
                email=user_update.email,
                full_name=user_update.full_name,
                password=user_update.password,
            ), expected_generation=generation if user_update.password is not None else None)
        except PasswordChangedMeanwhile:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The password was changed meanwhile; nothing was saved -- sign in again",
            )
        if not updated_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to update user"
            )

        logger.info(f"User {current_user.username} updated their profile")
        if user_update.password is not None:
            renew_own_login(response, updated_user, generation)

        # Return User model (without sensitive data)
        return public_user(updated_user)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Failed to update user {current_user.username}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user"
        )


@router.post("/api-key", response_model=APIKeyResponse)
async def generate_api_key(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
) -> APIKeyResponse:
    """
    Generate a new API key for the current user.

    Warning: The API key is only shown once. Save it securely.

    Args:
        request: carries the generation of the login that asks (get_current_user)
        current_user: Current authenticated user
        db: Database instance

    Returns:
        Generated API key
    """
    try:
        # Only while that login holds: a password changed since -- from another process too -- ended it
        api_key = db.generate_user_api_key(current_user.id, expected_generation=request.state.login_generation)
    except PasswordChangedMeanwhile:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not api_key:  # the account is gone: deleted since the login was checked
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info(f"API key generated for user: {current_user.username}")

    from datetime import datetime, timezone
    return APIKeyResponse(
        api_key=api_key,
        created_at=datetime.now(timezone.utc),
    )


@router.delete("/api-key", response_model=MessageResponse)
async def revoke_api_key(
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
) -> MessageResponse:
    """
    Revoke the current user's API key.

    Args:
        current_user: Current authenticated user
        db: Database instance

    Returns:
        Success message
    """
    if db.revoke_user_api_key(current_user.id):
        logger.info(f"API key revoked for user: {current_user.username}")
        return MessageResponse(message="API key revoked successfully")
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No API key found to revoke"
        )


@router.post("/password-reset-request", response_model=MessageResponse)
async def request_password_reset(
    reset_request: PasswordResetRequest,
    db: UserDatabase = Depends(get_db),
) -> MessageResponse:
    """
    Request a password reset (placeholder - requires email service).

    Note: This is a placeholder. In production, this would send an email
    with a reset token.

    Args:
        reset_request: Password reset request data
        db: Database instance

    Returns:
        Success message
    """
    # Check if user exists (but don't reveal if email is registered)
    user = db.get_user_by_email(reset_request.email)

    if user:
        logger.info(f"Password reset requested for: {user.username}")
        # TODO: Send email with reset token

    # Always return success to prevent user enumeration
    return MessageResponse(
        message="If the email exists, a password reset link has been sent",
        detail="Check your email for reset instructions"
    )


@router.post("/password-reset", response_model=MessageResponse)
async def reset_password(
    reset_data: PasswordReset,
) -> MessageResponse:
    """
    Reset password with token (placeholder - requires token validation).

    Note: This is a placeholder. In production, this would validate a
    reset token and update the password.

    Args:
        reset_data: Password reset data with token

    Returns:
        Success message
    """
    # TODO: Implement token validation and password reset
    logger.warning("Password reset endpoint called but not fully implemented")

    return MessageResponse(
        message="Password reset functionality not yet implemented",
        detail="Use admin tools to reset passwords"
    )
