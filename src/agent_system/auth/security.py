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
import logging

import bcrypt
from jose import JWTError, jwt

from agent_system.auth.models import PASSWORD_MAX_BYTES, TokenData, UserRole

logger = logging.getLogger(__name__)


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


#: Shorter signing keys are refused at startup.
MIN_SECRET_KEY_LENGTH = 32

#: sha256 of signing keys that are public, so anyone can sign an admin token with them: the
#: AuthConfig default, the development key config/config.yaml ships, the examples in the docs.
_PUBLIC_SECRET_KEY_SHA256 = frozenset({
    "c49cfe74258c7bf875ad025ab4aed98c7311d9d9929d732f0108f99f9ba1e8fb",  # AuthConfig.secret_key default
    "a293967483cb240a5873265fb8001b3b2ecc62507ad548dc83c2a985512221f9",  # config/config.yaml as shipped
    "ce4672e4f246127083e95215b3715799cac14f4e9129dd6eab816aac6aac6af7",  # docs/multi_user_authentication.md
    "3709d7f2d6e177c01b2443871411546cc15aa03b0466c339cb72c0f2621668c2",  # docs/security_hardening_design.md
    "fb944581fc56eb464b2fae7da22b7b47391476329c6ae27cc770d1cfb2ecf4ec",  # docs/reviews/2025-10-22/12_authentication_security.md
})


class WeakSecretKeyError(ValueError):
    """The JWT signing key would let others sign tokens: the server does not start with it."""


def check_secret_key(secret_key: str, *, reject_public: bool = False) -> None:
    """Refuse a signing key anyone could sign tokens with; a published one only warns unless
    ``reject_public``. An unset ``${AUTH_SECRET_KEY}`` in the config expands to "" -- that
    must stop the start, not sign every token with an empty key."""
    key = secret_key or ""
    how = "set a random one, e.g. python -c 'import secrets; print(secrets.token_hex(32))'"
    if not key.strip():
        raise WeakSecretKeyError(f"auth.secret_key is empty (an unset ${{AUTH_SECRET_KEY}} expands to ''): {how}")
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise WeakSecretKeyError(
            f"auth.secret_key has {len(key)} characters, at least {MIN_SECRET_KEY_LENGTH} are needed: {how}")
    if hashlib.sha256(key.encode()).hexdigest() in _PUBLIC_SECRET_KEY_SHA256:
        message = ("auth.secret_key is a published default -- anyone who has read this repository can "
                   f"sign an admin token for this server: {how}. auth.reject_default_secret_key: true "
                   "makes this a startup error.")
        if reject_public:
            raise WeakSecretKeyError(message)
        logger.error(message)


def set_jwt_config(secret_key: str, algorithm: str = "HS256", expire_minutes: int = 30, refresh_expire_days: int = 30,
                   reject_default_key: bool = False) -> None:
    """
    Configure JWT settings from application config.

    The key is checked first (check_secret_key): an empty or short one raises
    WeakSecretKeyError, a published one logs an error -- or raises with
    ``reject_default_key``.

    Args:
        secret_key: Secret key for JWT signing
        algorithm: JWT algorithm
        expire_minutes: Token expiration time in minutes
        refresh_expire_days: Refresh token expiration time in days
    """
    check_secret_key(secret_key, reject_public=reject_default_key)
    global SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
    SECRET_KEY = secret_key
    ALGORITHM = algorithm
    ACCESS_TOKEN_EXPIRE_MINUTES = expire_minutes
    REFRESH_TOKEN_EXPIRE_DAYS = refresh_expire_days
