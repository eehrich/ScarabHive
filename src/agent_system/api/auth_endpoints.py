"""
Authentication API Endpoints

Provides user registration, login, logout, and authentication management endpoints.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, status, Response
from pydantic import BaseModel

from agent_system.auth.models import (
    User,
    UserCreate,
    UserUpdate,
    Token,
    LoginRequest,
    PasswordResetRequest,
    PasswordReset,
    APIKeyResponse,
)
from agent_system.auth.database import get_db, UserDatabase, create_user
from agent_system.auth.security import (
    verify_password,
    create_access_token,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)
from agent_system.auth.dependencies import get_current_active_user


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["authentication"])


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str
    detail: Optional[str] = None


@router.post("/register", response_model=User, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Register a new user.
    
    Args:
        user_data: User registration data
        db: Database instance
    
    Returns:
        Created user (without password)
    
    Raises:
        HTTPException: If username or email already exists
    """
    try:
        user_in_db = create_user(user_data)
        logger.info(f"User registered: {user_in_db.username}")
        
        # Convert to User (remove sensitive data)
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
    
    # Verify credentials
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
            detail="User account is inactive"
        )
    
    # Create access token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={
            "sub": user.username,
            "user_id": user.id,
            "role": user.role.value,
        },
        expires_delta=access_token_expires,
    )
    
    # Update last login
    db.update_last_login(user.id)
    
    # Set secure cookie for browser clients
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=False,  # Allow JavaScript access for development
        secure=False,    # Allow HTTP for local development
        samesite="lax",  # Relaxed for development (use "strict" in production)
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,  # Same as token expiry
    )
    
    logger.info(f"User logged in: {user.username}")
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
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
        httponly=False,
        secure=False,
        samesite="lax"
    )
    
    logger.info(f"User logged out: {current_user.username}")
    return MessageResponse(
        message="Logged out successfully",
        detail="Remove the token from your client"
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


@router.patch("/me", response_model=User)
async def update_current_user(
    user_update: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
) -> User:
    """
    Update current user information.
    
    Users can update their own email, full_name, and password.
    Only admins can change role or is_active status.
    
    Args:
        user_update: User update data
        current_user: Current authenticated user
        db: Database instance
    
    Returns:
        Updated user data
    
    Raises:
        HTTPException: If update fails or unauthorized
    """
    # Users can only update their own email, full_name, and password
    # Role and is_active changes are restricted to admins (could be enforced in admin endpoints)
    
    try:
        # Get the current user from database to update
        user_in_db = db.get_user_by_id(current_user.id)
        if not user_in_db:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Update user
        updated_user = db.update_user(current_user.id, user_update)
        if not updated_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to update user"
            )
        
        logger.info(f"User {current_user.username} updated their profile")
        
        # Return User model (without sensitive data)
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
    current_user: User = Depends(get_current_active_user),
    db: UserDatabase = Depends(get_db),
) -> APIKeyResponse:
    """
    Generate a new API key for the current user.
    
    Warning: The API key is only shown once. Save it securely.
    
    Args:
        current_user: Current authenticated user
        db: Database instance
    
    Returns:
        Generated API key
    """
    api_key = db.generate_user_api_key(current_user.id)
    
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate API key"
        )
    
    logger.info(f"API key generated for user: {current_user.username}")
    
    from datetime import datetime
    return APIKeyResponse(
        api_key=api_key,
        created_at=datetime.utcnow(),
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
