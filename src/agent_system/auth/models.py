"""
User Models and Schemas

Defines the data models for user authentication and authorization.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional
from pydantic import AfterValidator, BaseModel, EmailStr, Field, ConfigDict
from pydantic_core import PydanticCustomError

PASSWORD_MAX_BYTES = 72  # bcrypt refuses longer passwords


def _check_password_bytes(password: str) -> str:
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise PydanticCustomError("password_too_long", f"The password is longer than {PASSWORD_MAX_BYTES} bytes")
    return password


Password = Annotated[str, Field(min_length=8), AfterValidator(_check_password_bytes)]


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
    password: Password


class UserRegister(BaseModel):
    """Self-registration (POST /auth/register, reachable without login).

    No role and no active flag: the server creates a plain active user.
    Sending either is rejected, so nobody registers themselves as admin.
    """
    model_config = {"extra": "forbid"}

    username: str = Field(..., min_length=3, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    email: EmailStr
    full_name: Optional[str] = None
    password: Password


class UserUpdate(BaseModel):
    """Schema for user updates (all fields optional)."""
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[UserRole] = None
    password: Optional[Password] = None


class UserSelfUpdate(BaseModel):
    """What a user may change on their own account (PATCH /auth/me).

    Role and active state are an admin's decision: sending them is rejected,
    not ignored, so a client never believes it changed them. A new password
    needs the current one, checked on the server.
    """
    model_config = {"extra": "forbid"}

    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    password: Optional[Password] = None
    current_password: Optional[str] = None


class ChatPreferences(BaseModel):
    """How the web chat shows a run (Settings -> Chat)."""
    model_config = {"extra": "forbid"}

    # When a run's step sections fold away: once it has answered, as soon as the next
    # step starts, or never.
    fold_steps: Literal["at_end", "at_next_step", "never"] = "at_end"
    # Whether a step's thinking is shown folded.
    thinking: Literal["collapsed", "expanded"] = "collapsed"
    # Whether a sub-agent's run, shown inside the call that started it, starts open.
    sub_agents: Literal["expanded", "collapsed"] = "expanded"
    # Whether a sub-agent's answer, inside its run, is shown folded. Folded by default:
    # several sub-agents streaming their answers at once turned the chat into a wall
    # that kept moving under the reader.
    sub_agent_output: Literal["collapsed", "expanded"] = "collapsed"


class UserPreferences(BaseModel):
    """What a user chose about how things are shown, kept with their account.

    Unknown keys are rejected rather than stored: a client that sends a name nobody
    reads would believe it changed something.
    """
    model_config = {"extra": "forbid"}

    chat: ChatPreferences = Field(default_factory=ChatPreferences)


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
    generation: int = 0  # the account's password changes when issued (UserDatabase.token_generation)


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
    new_password: Password


class APIKeyResponse(BaseModel):
    """API key response schema."""
    api_key: str
    created_at: datetime
    note: str = "Save this key securely. It will not be shown again."
