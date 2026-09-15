"""
Authentication and Security Utilities

Provides password hashing, JWT token management, and API key handling.
"""

from __future__ import annotations

import secrets
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import hashlib
import hmac

import bcrypt
from jose import JWTError, jwt

from agent_system.auth.models import PASSWORD_MAX_BYTES, TokenData, UserRole


# JWT settings (will be overridden by config)
# Use environment variable or generate secure random key
SECRET_KEY = os.environ.get("JWT_SECRET_KEY") or secrets.token_urlsafe(64)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080  # 7 days (7 * 24 * 60)
REFRESH_TOKEN_EXPIRE_DAYS = 30


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt directly."""
    # bcrypt requires bytes
    password_bytes = password.encode('utf-8')
    # Generate salt and hash
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    # Return as string (decode from bytes)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its bcrypt hash."""
    # bcrypt requires bytes
    password_bytes = plain_password.encode('utf-8')
    if len(password_bytes) > PASSWORD_MAX_BYTES:
        # bcrypt raises here; no stored password can be this long, so it is simply wrong.
        return False
    hashed_bytes = hashed_password.encode('utf-8')
    # Verify password
    return bcrypt.checkpw(password_bytes, hashed_bytes)


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
    secret_key: Optional[str] = None,
    algorithm: Optional[str] = None,
    token_type: str = "access",
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Payload data to encode
        expires_delta: Token expiration time delta (default: 30 minutes)
        secret_key: Secret key for signing (default: module SECRET_KEY)
        algorithm: JWT algorithm (default: HS256)
        token_type: Token type ("access" or "refresh")

    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire, "type": token_type})

    encoded_jwt = jwt.encode(
        to_encode,
        secret_key or SECRET_KEY,
        algorithm=algorithm or ALGORITHM
    )
    return encoded_jwt


def decode_access_token(
    token: str,
    secret_key: Optional[str] = None,
    algorithm: Optional[str] = None,
) -> Optional[TokenData]:
    """
    Decode and validate a JWT access token.

    Args:
        token: JWT token string
        secret_key: Secret key for validation (default: module SECRET_KEY)
        algorithm: JWT algorithm (default: HS256)

    Returns:
        TokenData if valid, None otherwise
    """
    try:
        payload = jwt.decode(
            token,
            secret_key or SECRET_KEY,
            algorithms=[algorithm or ALGORITHM]
        )
        username: Optional[str] = payload.get("sub")
        user_id: Optional[int] = payload.get("user_id")
        role_str: Optional[str] = payload.get("role")
        token_type: Optional[str] = payload.get("type", "access")

        if username is None:
            return None

        # Convert role string to enum
        role = None
        if role_str:
            try:
                role = UserRole(role_str)
            except ValueError:
                pass

        return TokenData(username=username, user_id=user_id, role=role, token_type=token_type)
    except JWTError:
        return None


def generate_api_key() -> str:
    """Generate a secure random API key."""
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage (using SHA-256)."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    """Verify an API key against its hash."""
    return hmac.compare_digest(hash_api_key(plain_key), hashed_key)


def set_jwt_config(secret_key: str, algorithm: str = "HS256", expire_minutes: int = 30, refresh_expire_days: int = 30) -> None:
    """
    Configure JWT settings from application config.

    Args:
        secret_key: Secret key for JWT signing
        algorithm: JWT algorithm
        expire_minutes: Token expiration time in minutes
        refresh_expire_days: Refresh token expiration time in days
    """
    global SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
    SECRET_KEY = secret_key
    ALGORITHM = algorithm
    ACCESS_TOKEN_EXPIRE_MINUTES = expire_minutes
    REFRESH_TOKEN_EXPIRE_DAYS = refresh_expire_days
