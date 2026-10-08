"""
Authentication and Security Utilities

Provides password hashing, JWT token management, and API key handling.
"""

from __future__ import annotations

import logging
import secrets
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import hashlib
import hmac

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
#: This process enforces authentication: set_jwt_config, which the API calls at start when auth is on, sets it.
#: A run it wakes is told so (core/session_presence.spawn_wake).
AUTH_ENFORCED = False


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

        # a token from before generations were counted is one of the first: 0
        return TokenData(username=username, user_id=user_id, role=role, token_type=token_type,
                         generation=payload.get("gen", 0))
    except JWTError:
        return None


def generate_api_key() -> str:
    """Generate a secure random API key."""
    return secrets.token_urlsafe(32)


def bearer_api_key(token: Optional[str]) -> Optional[str]:
    """The API key in ``Authorization: Bearer <key>`` -- how OpenAI clients send theirs -- or None.

    A JWT always has two dots; a key from ``generate_api_key`` (``token_urlsafe``) never has
    one. So a dotless Bearer value is an API key, and the middleware and ``get_current_user``
    both treat it exactly like an ``X-API-Key`` header: they must decide alike, or one layer
    authenticates a user the other does not.
    """
    if token and "." not in token:
        return token
    return None


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage (using SHA-256)."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    """Verify an API key against its hash."""
    return hmac.compare_digest(hash_api_key(plain_key), hashed_key)


#: Shorter signing keys are refused at startup.
MIN_SECRET_KEY_LENGTH = 32

#: Every signing key the repository has printed -- config.yaml's, the examples in the docs,
#: reviews and templates, the tests' -- found in its history on 28.09.2026. The history keeps
#: them known for good, whatever the files say today. The guard below refuses or reports each of
#: them; the Setup panel (plugins/setup/status.py) names the same list. setup's
#: test_every_key_the_repository_prints_is_known holds every literal key a commit puts into a
#: file outside the tests against it.
PUBLISHED_SIGNING_KEYS = (
    "published-signing-key-replace-with-your-own-0000000000",
    "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION_USE_RANDOM_STRING",
    "your-secret-key-here-CHANGE-IN-PRODUCTION-min-32-chars",
    "your-secret-key-min-32-chars",
    "your-secret-here",
    "your-generated-secret",
    "YOUR_VERY_LONG_RANDOM_SECRET_KEY_HERE",
    "e4c8f2b9a7d3e1f5c6b8a2d9e7f1c3b5a8d2e6f9c1b4a7d3e8f2c5b9a1d6e3f7",
    "generate-secure-random-key",
    "generated-secure-key",
    "test-secret-key",
    "test-secret-key-12345",
    "test-secret-key-for-jwt",
    "test-secret-key-do-not-use-in-production",
    "test-key-min-32-chars-long-secure",
    "secure-secret-key-32chars!",
    "not-the-secret-" * 4,
    "your-secure-key-here-min-32-chars",  # user_management's auth_disabled.html offered it to paste, for months
    "test-only-secret-not-the-config-one",
    "test-only-secret-not-the-config-one-0123456789",
    "jwt-signing-key-42",
    "generated-for-this-installation",
    "own-key-of-this-installation-0123456789",
    "reloaded-own-key-0123456789abcdef",
)

#: sha256 of every key above and of the AuthConfig.secret_key default. It held five of them alone,
#: so nine published keys of 32 characters or more passed the start without a word -- even with
#: reject_default_secret_key.
_PUBLIC_SECRET_KEY_SHA256 = frozenset(
    hashlib.sha256(key.encode()).hexdigest()
    for key in (*PUBLISHED_SIGNING_KEYS, "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION"))


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
                   reject_default_key: bool = False, listens_beyond_loopback: bool = False) -> None:
    """
    Configure JWT settings from application config.

    The key is checked first (check_secret_key): an empty or short one raises
    WeakSecretKeyError, a published one logs an error -- or raises with
    ``reject_default_key``, or when the server listens beyond loopback
    (``listens_beyond_loopback``): there anyone it answers could sign an admin token.

    Args:
        secret_key: Secret key for JWT signing
        algorithm: JWT algorithm
        expire_minutes: Token expiration time in minutes
        refresh_expire_days: Refresh token expiration time in days
    """
    global SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS, AUTH_ENFORCED
    from agent_system.config.models import AuthConfig
    # The model's default stands in the repository, and an auth section without the line gets it:
    # refused whatever reject_default_key says -- everyone could sign a login with it.
    try:
        check_secret_key(secret_key, reject_public=reject_default_key or listens_beyond_loopback
                         or secret_key == AuthConfig.model_fields["secret_key"].default)
    except WeakSecretKeyError as exc:
        logger.critical("Refusing to start: %s", exc)  # the API log too, not only the console of the start
        raise
    SECRET_KEY = secret_key
    ALGORITHM = algorithm
    ACCESS_TOKEN_EXPIRE_MINUTES = expire_minutes
    REFRESH_TOKEN_EXPIRE_DAYS = refresh_expire_days
    AUTH_ENFORCED = True
