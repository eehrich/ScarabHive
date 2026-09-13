"""
User Models and Schemas

Defines the data models for user authentication and authorization.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict


class UserRole(str, Enum):
    """User role enumeration for access control."""
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"


class UserBase(BaseModel):
    """Base user schema with common fields."""
    username: str = Field(..., min_length=3, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool = True
    role: UserRole = UserRole.USER


class UserCreate(UserBase):
    """Schema for user creation with password."""
    password: str = Field(..., min_length=8)


class UserRegister(BaseModel):
    """Self-registration (POST /auth/register, reachable without login).

    No role and no active flag: the server creates a plain active user.
    Sending either is rejected, so nobody registers themselves as admin.
    """
    model_config = {"extra": "forbid"}

    username: str = Field(..., min_length=3, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    email: EmailStr
    full_name: Optional[str] = None
    password: str = Field(..., min_length=8)


class UserUpdate(BaseModel):
    """Schema for user updates (all fields optional)."""
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[UserRole] = None
    password: Optional[str] = Field(None, min_length=8)


class UserSelfUpdate(BaseModel):
    """What a user may change on their own account (PATCH /auth/me).

    Role and active state are an admin's decision: sending them is rejected,
    not ignored, so a client never believes it changed them. A new password
    needs the current one, checked on the server.
    """
    model_config = {"extra": "forbid"}

    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    password: Optional[str] = Field(None, min_length=8)
    current_password: Optional[str] = None


class User(UserBase):
    """User schema returned to clients (no sensitive data)."""
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    last_login: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class UserInDB(User):
    """User schema with hashed password (internal use only)."""
    hashed_password: str
    api_key: Optional[str] = None


class Token(BaseModel):
    """JWT token response schema."""
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int  # seconds


class TokenData(BaseModel):
    """Token payload data."""
    username: Optional[str] = None
    user_id: Optional[int] = None
    role: Optional[UserRole] = None
    token_type: Optional[str] = "access"  # "access" or "refresh"


class LoginRequest(BaseModel):
    """Login request schema."""
    username: str
    password: str


class RefreshTokenRequest(BaseModel):
    """Refresh token request schema."""
    refresh_token: str


class PasswordResetRequest(BaseModel):
    """Password reset request schema."""
    email: EmailStr


class PasswordReset(BaseModel):
    """Password reset schema with token."""
    token: str
    new_password: str = Field(..., min_length=8)


class APIKeyResponse(BaseModel):
    """API key response schema."""
    api_key: str
    created_at: datetime
    note: str = "Save this key securely. It will not be shown again."
