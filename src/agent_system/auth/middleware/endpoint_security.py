"""Endpoint security: the ``endpoint_security`` rules of the config, enforced on every request."""

from __future__ import annotations

import logging
from typing import Any, List, Optional, TYPE_CHECKING
from urllib.parse import unquote

from starlette.types import ASGIApp, Receive, Scope, Send

from agent_system.auth.credentials import access_token_cookie, bearer_credentials, first_headers
from agent_system.auth.enforcement import (
    ROLE_HIERARCHY,
    anonymous_may_reach,
    anonymous_meets_role,
    compile_endpoint_rules,
    first_matching_rule,
    meets_role,
)
from agent_system.auth.models import USERNAME_RE

if TYPE_CHECKING:
    from agent_system.config import AuthConfig

logger = logging.getLogger(__name__)

#: Where the audit leaves the (username, role) it resolved for the endpoint security inside it.
IDENTITY_SCOPE_KEY = "agent_system.identity"


class EndpointSecurityMiddleware:
    """
    Endpoint security enforcement middleware (Pure ASGI implementation).
    
    Enforces endpoint_security rules from config/security.yaml (included by config.yaml) by checking
    JWT tokens and validating user roles for admin-only endpoints.
    """
    
    def __init__(self, app: ASGIApp, auth_config: "AuthConfig"):
        """
        Initialize endpoint security middleware.

        Args:
            app: ASGI application
            auth_config: Authentication configuration with endpoint_security rules
        """
        self.app = app
        self.auth_config = auth_config
        self._compiled_patterns: List[tuple] = []
        self._compile_patterns()
        # Lazy-loaded singleton handle for X-API-Key validation against UserDatabase.
        # None until first X-API-Key request reaches the middleware.
        self._user_db: Optional[Any] = None

    def _get_user_db(self) -> Any:
        """Lazy import + cache the global UserDatabase singleton.

        Imported lazily so the middleware module does not pull in the auth
        database (and its SQLite init side-effects) at import time. The
        underlying SQLite database opens a fresh connection per query, so the
        cached handle is safe to share across concurrent ASGI requests.
        """
        if self._user_db is None:
            from agent_system.auth.database import get_db
            self._user_db = get_db()
        return self._user_db
    
    def _compile_patterns(self) -> None:
        """Compile endpoint patterns into regex for efficient matching."""
        self._compiled_patterns = compile_endpoint_rules(self.auth_config.endpoint_security.rules)
    
    def _normalize_path(self, path: str) -> str:
        """Normalize path to prevent traversal attacks.
        
        - URL-decodes the path
        - Resolves .. and . segments
        - Removes duplicate slashes
        - Always starts with /
        - Cannot escape above root (/../ becomes /)
        
        Args:
            path: Raw URL path
            
        Returns:
            Normalized path string
        """
        # URL decode (handles %2e%2e for .. etc)
        decoded = unquote(path)
        
        try:
            # Split into parts and filter out empty/dangerous segments
            parts = decoded.split('/')
            normalized_parts: list[str] = []
            
            for part in parts:
                if part == '..':
                    # Go up one level (but don't go above root - just ignore)
                    if normalized_parts:
                        normalized_parts.pop()
                    # If no parts to pop, we're at root - ignore the ..
                elif part and part != '.':
                    normalized_parts.append(part)
            
            normalized = '/' + '/'.join(normalized_parts)
            return normalized
        except Exception:
            # Fallback: return original if normalization fails
            return path if path.startswith('/') else '/' + path
    
    def _get_endpoint_policy(self, method: str, path: str) -> tuple:
        """Get the security policy for an endpoint.
        
        Returns:
            Tuple of (requires_auth, min_role, policy_name)
        """
        method = method.upper()
        
        # Normalize path to prevent traversal attacks
        path = self._normalize_path(path)
        
        # Check configured rules in order
        rule = first_matching_rule(self._compiled_patterns, method, path)
        if rule is not None:
            requires_auth = rule.policy == "require_auth"
            min_role = rule.min_role if requires_auth else None
            return (requires_auth, min_role, rule.pattern)
        
        # No rule matched, use default policy
        default_requires_auth = self.auth_config.endpoint_security.default_policy == "require_auth"
        return (default_requires_auth, "user" if default_requires_auth else None, "default")
    
    def _extract_user_info(self, scope: Scope) -> tuple:
        """Extract user info from JWT token or X-API-Key header.

        Checks in priority order:
        1. Authorization: Bearer <token> header (JWT)
        2. access_token cookie (JWT)
        3. X-API-Key header (API key, looked up in UserDatabase) -- or an API key
           sent as the Bearer value (``security.bearer_api_key``: OpenAI clients
           send theirs that way), which then counts as that header

        Deliberately no ``?token=`` query parameter: a token in a URL ends up in
        access logs, browser history and Referer headers. EventSource, the one
        client that cannot set headers, sends the cookie same-origin anyway.

        JWT validation failures fall through to the X-API-Key lookup so that
        clients which combine an invalid/expired session with a valid API key
        (e.g. a still-mounted browser cookie alongside a programmatic header)
        are still authenticated. If neither path yields a valid user, returns
        (None, None).

        Returns:
            Tuple of (username, role) or (None, None) if not authenticated
        """
        from jose import jwt, JWTError

        # As the route dependencies read them (Starlette, HTTPBearer): the first of a repeated header, the scheme
        # in any case (auth/credentials.py). Read otherwise, `bearer <key>` or a second Authorization header next
        # to a cookie made the two layers name different users.
        headers = first_headers(scope)

        # 1. Try Authorization header (highest priority)
        token = bearer_credentials(headers)
        from agent_system.auth.security import bearer_api_key
        bearer_key = bearer_api_key(token)
        if bearer_key:
            token = None  # an API key, not a JWT: step 4 decides, and no cookie stands in for it

        # 2. Try cookie -- parsed by the parser behind the dependencies' request.cookies, so of two access_token
        # cookies both layers take the same one (the last): read the first, the middleware passed one user and
        # the route ran as the other.
        if not token and not bearer_key:
            token = access_token_cookie(headers)

        # Try to decode JWT if a token was found. Soft-fail to allow the
        # X-API-Key fallback below to still authenticate the request.
        if token:
            try:
                payload = jwt.decode(
                    token,
                    self.auth_config.secret_key,
                    algorithms=[self.auth_config.algorithm]
                )
                username = payload.get("sub")

                if payload.get("type", "access") != "access":
                    # Refresh tokens are exchange-only (/auth/refresh); they
                    # must not authenticate requests directly.
                    logger.debug("JWT is not an access token (type=%s) - rejected",
                                 payload.get("type"))
                elif not username or not isinstance(username, str):
                    logger.debug("JWT token has invalid or missing 'sub' claim")
                elif not USERNAME_RE.fullmatch(username):
                    logger.warning("JWT token has invalid username format")
                else:
                    identity = self._lookup_token_user(username, payload.get("user_id"), payload.get("gen", 0))
                    if identity != (None, None):
                        return identity
            except JWTError as e:
                logger.debug(f"JWT token error: {e}")

        # 4. Try X-API-Key header (API key auth via UserDatabase).
        # Duplicate X-API-Key headers are refused outright: two keys could name
        # two users, and nothing says which one the request is. (The header
        # map above keeps the first, as Starlette's Headers.get does for the
        # downstream FastAPI dependencies.)
        raw_headers = scope.get("headers", [])
        api_key_header_count = sum(1 for h in raw_headers if h[0].lower() == b"x-api-key")
        if api_key_header_count > 1:
            logger.warning("Multiple X-API-Key headers received; rejecting request")
            return (None, None)
        api_key = headers.get(b"x-api-key", "").strip()
        if bearer_key:
            if api_key and api_key != bearer_key:
                logger.warning("Different API keys in Authorization and X-API-Key; rejecting request")
                return (None, None)
            api_key = bearer_key
        if api_key:
            return self._lookup_api_key(api_key)

        return (None, None)

    # Constant dummy hash for the "key not in DB" branch of _lookup_api_key.
    # Used to keep verify_api_key timing identical between hit and miss so that
    # the SQLite lookup latency is the only remaining signal (~µs constant).
    _DUMMY_API_KEY_HASH = "0" * 64

    def _lookup_api_key(self, api_key: str) -> tuple:
        """Validate an X-API-Key against the user database.

        Uses the same flow as auth.dependencies.get_current_user:
        hash → DB lookup → hmac.compare_digest verify → return (username, role).

        Security invariants:
        - Never logs the raw key or the stored hash.
        - Always runs verify_api_key() (constant-time hmac.compare_digest)
          on EVERY call — even on a DB miss — to flatten timing differences
          between known and unknown keys.
        - Inactive users are rejected.
        - Username/role are sanitized identically to the JWT path so the
          downstream audit/role checks see the same shape.
        - Infrastructure errors (DB/import) are logged at WARNING so an
          operator notices silent 401-loops; only the exception class is
          logged (never the raw key).

        Returns:
            Tuple of (username, role) on success, (None, None) otherwise.
        """
        try:
            from agent_system.auth.security import hash_api_key, verify_api_key
        except ImportError as exc:
            logger.error(f"API-Key auth unavailable — security module import failed: {exc}")
            return (None, None)

        try:
            api_key_hash = hash_api_key(api_key)
            user_in_db = self._get_user_db().get_user_by_api_key(api_key_hash)
        except Exception as exc:  # noqa: BLE001
            # DB-side failure (corrupted file, locked, out of descriptors, ...)
            # — loud, no raw key. The message matters: logging only the class
            # name turned a descriptor exhaustion ("unable to open database
            # file") into an unexplained OperationalError, and the 401 storm it
            # caused read like an auth defect for hours. The exception carries
            # the hash at most, never the key, and SQLite does not echo bound
            # parameters into its messages.
            logger.error(
                "API-Key DB lookup failed (%s: %s); requests with X-API-Key "
                "will return 401",
                exc.__class__.__name__,
                exc,
            )
            return (None, None)

        # Always verify, even on miss, to equalise timing between hit/miss.
        stored_hash = user_in_db.api_key if user_in_db and user_in_db.api_key else self._DUMMY_API_KEY_HASH
        verified = verify_api_key(api_key, stored_hash)
        if not user_in_db or not verified or not user_in_db.is_active:
            return (None, None)
        return self._identity_of(user_in_db)

    def _lookup_token_user(self, username: str, user_id: Any, generation: Any = 0) -> tuple:
        """Resolve a verified access token to its account, like the API-key branch.

        The token only names the account: it must still exist under the id it was
        issued for and be active, and the role is the account's current one -- so a
        demoted, deactivated or deleted admin loses access now, not at token expiry.
        A password changed since it was issued (its generation) ends it the same way.
        """
        try:
            db = self._get_user_db()
            user_in_db = db.get_user_by_username(username)
            current = user_in_db and user_in_db.id == user_id and generation == db.token_generation(user_in_db.id)
        except Exception as exc:  # noqa: BLE001
            logger.error("JWT user DB lookup failed (%s: %s); request will return 401",
                         exc.__class__.__name__, exc)
            return (None, None)
        if not current or not user_in_db.is_active:
            logger.debug("JWT does not match an active account - rejected")
            return (None, None)
        return self._identity_of(user_in_db)

    @staticmethod
    def _identity_of(user_in_db: Any) -> tuple:
        """(username, role) of an account row, sanitised the same way for every auth path."""
        try:
            username = user_in_db.username
            if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
                logger.warning("Authenticated user has invalid username format")
                return (None, None)

            role_obj = user_in_db.role
            role = role_obj.value if hasattr(role_obj, "value") else str(role_obj)
            if not isinstance(role, str) or role.lower() not in ROLE_HIERARCHY:
                logger.warning(
                    "Authenticated user has unknown role %r; defaulting to 'user'",
                    role,
                )
                role = "user"

            return (username, role)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "User record could not be normalised (%s)",
                exc.__class__.__name__,
            )
            return (None, None)
    
    def _check_role(self, user_role: Optional[str], min_role: Optional[str]) -> bool:
        """Check if user has sufficient role.
        
        Returns:
            True if user has required role or higher
        """
        if min_role is None:
            return True
        
        if user_role is None:
            return False
        
        return meets_role(user_role, min_role)
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process request with security enforcement."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        
        # Get policy for this endpoint
        requires_auth, min_role, matched_pattern = self._get_endpoint_policy(method, path)
        
        # If no auth required, pass through
        if not requires_auth:
            await self.app(scope, receive, send)
            return
        
        # Auth required - check user
        # the audit around this layer has looked the account up already
        username, user_role = scope[IDENTITY_SCOPE_KEY] if IDENTITY_SCOPE_KEY in scope else self._extract_user_info(scope)

        # No user and auth required
        if username is None:
            # Check if anonymous access is allowed for this endpoint
            anonymous_access = self.auth_config.anonymous_access
            if anonymous_may_reach(anonymous_access, method, path):
                if anonymous_meets_role(min_role, "user", anonymous_access.role):
                    await self.app(scope, receive, send)
                    return
                safe_path = path.replace('\n', '').replace('\r', '')[:200]
                logger.info(f"[SECURITY] Forbidden: {method} {safe_path} - anonymous access, requires {min_role}")
                await self._send_error_response(send, 403, f"Insufficient permissions. Required role: {min_role}")
                return
            
            # Return 401
            safe_path = path.replace('\n', '').replace('\r', '')[:200]
            logger.info(f"[SECURITY] Unauthorized: {method} {safe_path} - no valid token")
            await self._send_error_response(send, 401, "Authentication required")
            return
        
        # Check role
        if min_role and not self._check_role(user_role, min_role):
            safe_path = path.replace('\n', '').replace('\r', '')[:200]
            safe_username = str(username).replace('\n', '').replace('\r', '')[:50] if username else 'unknown'
            logger.info(f"[SECURITY] Forbidden: {method} {safe_path} - user {safe_username} "
                       f"has role {user_role}, requires {min_role}")
            await self._send_error_response(
                send, 403, 
                f"Insufficient permissions. Required role: {min_role}"
            )
            return
        
        # User authenticated and authorized
        await self.app(scope, receive, send)
    
    async def _send_error_response(self, send: Send, status_code: int, detail: str) -> None:
        """Send an error response."""
        import json
        
        # Create detailed error response
        error_body = {
            "detail": detail,
            "error": "Unauthorized" if status_code == 401 else "Forbidden",
            "status_code": status_code
        }
        body = json.dumps(error_body).encode("utf-8")
        
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        
        # Add WWW-Authenticate header for 401 responses (RFC 7235)
        if status_code == 401:
            headers.append((b"www-authenticate", b'Bearer realm="AgentSystem API"'))
        
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })

