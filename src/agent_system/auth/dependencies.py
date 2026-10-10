"""
FastAPI Authentication Dependencies

Provides dependency injection functions for authentication and authorization.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from agent_system.auth.models import User, UserRole, public_user
from agent_system.auth.database import get_db, UserDatabase
from agent_system.auth.security import bearer_api_key, decode_access_token, verify_api_key, hash_api_key


# Security schemes
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_api_key: Optional[str] = Header(None),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Get the current authenticated user from JWT token (Bearer or Cookie) or API key.
    A caller that takes tokens only uses get_token_user: here a key in the
    Bearer value counts even when ``x_api_key`` is None.
    
    This dependency checks (in order):
    1. Bearer token in Authorization header
    2. JWT token in access_token cookie
    3. API key in X-API-Key header -- or an API key sent as the Bearer value
       (``security.bearer_api_key``), which then counts as that header, exactly
       as in EndpointSecurityMiddleware
    
    Args:
        request: FastAPI request object
        credentials: Optional Bearer token from Authorization header
        x_api_key: Optional API key from header
        db: Database instance
    
    Raises:
        HTTPException: If authentication fails
    
    Returns:
        Authenticated user
    """
    return await _authenticate(request, credentials, x_api_key, db, bearer_keys=True)


async def get_token_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
    db: UserDatabase,
) -> User:
    """The user of the request's access token (Bearer or cookie) -- never an API key, in either header.

    For the routes that check an admin themselves and may take a token only
    (user_management, agent_editor): the token path awaits nothing between the
    check and the route's write. Raises 401 as get_current_user does.
    """
    return await _authenticate(request, credentials, None, db, bearer_keys=False)


async def _authenticate(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
    x_api_key: Optional[str],
    db: UserDatabase,
    *,
    bearer_keys: bool,
) -> User:
    """get_current_user; ``bearer_keys=False`` takes a key in the Bearer value for the (invalid) token it is."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    token = None
    
    # 1. Try Bearer token from Authorization header (stripped, as the middleware reads it)
    if credentials:
        token = credentials.credentials.strip()
    bearer_key = bearer_api_key(token) if bearer_keys else None
    if bearer_key:
        if x_api_key and x_api_key.strip() != bearer_key:
            raise credentials_exception  # two different keys: whose request is it?
        x_api_key, token = bearer_key, None
    
    # 2. Try JWT token from cookie
    if not token and not bearer_key:
        token = request.cookies.get("access_token")
    
    # Process JWT token if found
    if token:
        token_data = decode_access_token(token)
        # Only real access tokens authenticate here. A refresh token is
        # longer-lived and must stay exchange-only (/auth/refresh checks the
        # mirror condition) -- accepting it would void the short expiry.
        if token_data and token_data.username and token_data.token_type == "access":
            user_in_db = db.get_user_by_username(token_data.username)
            # The id binds the token to this account: a later account under a
            # deleted user's name must not inherit that user's tokens. The
            # generation to its password: a change ends the logins made before.
            if (user_in_db and user_in_db.id == token_data.user_id
                    and token_data.generation == db.token_generation(user_in_db.id)):
                # What the request writes for this login holds only while it does (POST /auth/api-key)
                request.state.login_generation = token_data.generation
                # Convert to User (remove sensitive data)
                return public_user(user_in_db)
    
    # 3. Try API key
    if x_api_key:
        api_key_hash = hash_api_key(x_api_key)
        user_in_db = db.get_user_by_api_key(api_key_hash)
        if user_in_db and user_in_db.api_key:
            if verify_api_key(x_api_key, user_in_db.api_key):
                # API-key users never hit /auth/login, so this is their only
                # last_login writer. Throttled to once per hour and off-loop:
                # the old code wrote SQLite synchronously on EVERY request,
                # which both blocked the event loop and turned last_login
                # into "time of the latest request".
                last = user_in_db.last_login
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (last is None
                        or (datetime.now(timezone.utc) - last).total_seconds() > 3600):
                    await asyncio.to_thread(db.update_last_login, user_in_db.id)
                # Read with the key, after the wait above: a password changed since the lookup revoked it,
                # and a demotion or deactivation since the lookup is the account this request acts as
                request.state.login_generation = db.api_key_generation(api_key_hash)
                user_in_db = db.get_user_by_api_key(api_key_hash)
                if request.state.login_generation is None or user_in_db is None:
                    raise credentials_exception
                return public_user(user_in_db)
    
    raise credentials_exception


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Get the current active user.
    
    Raises:
        HTTPException: If user is inactive
    
    Returns:
        Active user
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user


async def require_admin(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Require admin role for the current user.
    
    Raises:
        HTTPException: If user is not an admin
    
    Returns:
        Admin user
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )
    return current_user


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_api_key: Optional[str] = Header(None),
    db: UserDatabase = Depends(get_db),
) -> Optional[User]:
    """
    Get the current user if authenticated, None otherwise.
    
    This is useful for optional authentication where endpoints can work
    with or without authentication.
    
    Returns:
        User if authenticated, None otherwise
    """
    try:
        return await get_current_user(request, credentials, x_api_key, db)
    except HTTPException:
        return None
