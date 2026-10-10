"""
Security Middleware

Provides rate limiting, CORS, security headers, and audit middleware.
Uses Pure ASGI implementation for better performance (avoids BaseHTTPMiddleware overhead).
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Sequence, TYPE_CHECKING
from urllib.parse import quote, unquote
import logging
from logging.handlers import RotatingFileHandler

from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send, Message

from agent_system.auth.enforcement import (
    ROLE_HIERARCHY,
    anonymous_may_reach,
    compile_endpoint_rules,
    first_matching_rule,
)
from agent_system.utils.logging import KeyInPathFilter, loggable_path

if TYPE_CHECKING:
    from agent_system.config import AuthConfig

logger = logging.getLogger(__name__)


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
        # in any case. Read otherwise, `bearer <key>` or a second Authorization header next to a cookie made the
        # two layers name different users.
        headers: dict = {}
        for name, value in scope.get("headers", []):
            headers.setdefault(name, value)
        token = None

        # 1. Try Authorization header (highest priority)
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        scheme, _, credentials = auth_header.partition(" ")
        if scheme.lower() == "bearer":
            token = credentials.strip()
        from agent_system.auth.security import bearer_api_key
        bearer_key = bearer_api_key(token)
        if bearer_key:
            token = None  # an API key, not a JWT: step 4 decides, and no cookie stands in for it

        # 2. Try cookie -- parsed by the parser behind the dependencies' request.cookies, so of two access_token
        # cookies both layers take the same one (the last): read the first, the middleware passed one user and
        # the route ran as the other.
        if not token and not bearer_key:
            from starlette.requests import cookie_parser
            token = cookie_parser(headers.get(b"cookie", b"").decode("latin-1")).get("access_token") or None

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
                elif not all(c.isalnum() or c in '_-.' for c in username):
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
        api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore").strip()
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
            if not isinstance(username, str) or not all(
                c.isalnum() or c in "_-." for c in username
            ):
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
        
        user_level = ROLE_HIERARCHY.get(user_role.lower(), 0)
        required_level = ROLE_HIERARCHY.get(min_role.lower(), 0)
        
        return user_level >= required_level
    
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
            if anonymous_may_reach(self.auth_config.anonymous_access, method, path):
                await self.app(scope, receive, send)
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


#: What the audit log sorts a request into; static files are never kept.
AUDIT_CATEGORIES = ("plugin", "api", "agent", "auth", "tools", "debug", "health", "other")
AUDIT_STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")
#: Where the Security Audit panel reads the log (api/admin_endpoints.py).
AUDIT_LOG_PATH = "/admin/security/audit"
AUDIT_LOGGER_NAME = "agent_system.security.audit"
#: Where the audit leaves the (username, role) it resolved for the endpoint security inside it.
IDENTITY_SCOPE_KEY = "agent_system.identity"


def security_audit_logger() -> logging.Logger:
    """The writer of logs/security.log, shared by every auditor in the process.

    One handler per process: the endpoint audit and the plugin route audit both
    write here, and a handler each wrote every line twice.
    """
    security_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    if not security_logger.handlers:
        security_logger.setLevel(logging.INFO)
        security_logger.propagate = False
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(log_dir / "security.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s",
                                               datefmt="%Y-%m-%d %H:%M:%S"))
        handler.addFilter(KeyInPathFilter())
        security_logger.addHandler(handler)
        logger.info("Security audit logger initialized: logs/security.log")
    return security_logger


def log_field(value: Any) -> str:
    """A value as one field of a security.log line, percent-encoded.

    A decoded path (or a name) may carry a newline, which would forge the next line,
    or " | user=root", which would forge a field of this one. Normal path characters stay.
    """
    return quote(str(value), safe="/:@!$&'()*+,;=[]")


class SecurityAuditMiddleware:
    """
    Security audit middleware (Pure ASGI implementation).

    Logs every HTTP request (static files aside) to logs/security.log and keeps
    the latest in memory for the Security Audit panel.
    """

    _instance: Optional["SecurityAuditMiddleware"] = None

    def __init__(
        self,
        app: ASGIApp,
        auth_config: "AuthConfig",
        enabled: bool = True,
        max_memory_entries: int = 1000,
    ):
        """
        Args:
            app: ASGI application
            auth_config: resolves who sent a request exactly as the endpoint security does
            enabled: Whether audit logging is enabled
            max_memory_entries: Maximum entries to keep in memory
        """
        self.app = app
        self.enabled = enabled
        self._audit_log: List[Dict[str, Any]] = []
        self._max_memory_entries = max_memory_entries
        # The log names the account the request authenticates as -- a verified token
        # of an existing active account or a valid API key -- never a claim anyone can write.
        self._identity = EndpointSecurityMiddleware(app, auth_config)
        SecurityAuditMiddleware._instance = self

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process request with audit logging."""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        category = self._get_category(path)
        if category == "static":  # never kept (too noisy): not worth an account lookup either
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        response_status = 0
        # Before the route runs: a request that changes its own credentials (DELETE /auth/api-key)
        # is named by what it authenticated with. The endpoint security inside reuses it.
        identity = scope[IDENTITY_SCOPE_KEY] = self._identity._extract_user_info(scope)

        async def send_wrapper(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)
            await send(message)

        start_time = time.time()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not response_status:
                response_status = 500  # what the server error middleware outside answers
            raise
        finally:
            self._audit_access(
                path=path,
                method=method,
                user_id=identity[0] or "anonymous",
                client_ip=client_ip,
                status_code=response_status,
                duration_ms=(time.time() - start_time) * 1000,
                category=category,
                allowed=200 <= response_status < 400,
            )

    def _get_category(self, path: str) -> str:
        """Categorize the endpoint."""
        if path.startswith("/plugins/"):
            return "plugin"
        elif path.startswith("/api/"):
            return "api"
        elif path.startswith("/run") or path.startswith("/stream"):
            return "agent"
        elif path.startswith("/static/") or path.startswith("/favicon"):
            return "static"
        elif path.startswith("/auth/") or path.startswith("/login") or path.startswith("/logout"):
            return "auth"
        elif path.startswith("/tools/"):
            return "tools"
        elif path.startswith("/debug/"):
            return "debug"
        elif path.startswith("/health") or path.startswith("/meta"):
            return "health"
        else:
            return "other"

    def _audit_access(
        self,
        path: str,
        method: str,
        user_id: str,
        client_ip: str,
        status_code: int,
        duration_ms: float,
        category: str,
        allowed: bool
    ) -> None:
        """Log endpoint access for auditing."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": loggable_path(path),  # the Security Audit panel shows it
            "method": method,
            "user_id": user_id,
            "client_ip": client_ip,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "category": category,
            "allowed": allowed
        }

        # An open audit panel reads the log every few seconds: its answered reads
        # would push out of memory what it is there to show. The file keeps them.
        if not (allowed and method == "GET" and path == AUDIT_LOG_PATH):
            self._audit_log.append(entry)
            if len(self._audit_log) > self._max_memory_entries:
                self._audit_log = self._audit_log[-self._max_memory_entries:]

        status_str = "ALLOWED" if allowed else "DENIED"
        security_audit_logger().log(
            logging.INFO if allowed else logging.WARNING,
            f"{status_str} | {log_field(method)} {log_field(path)} | user={log_field(user_id)} | ip={log_field(client_ip)} | "
            f"status={status_code} | {duration_ms:.1f}ms | {category}"
        )

    def get_audit_log(
        self,
        category: Optional[str] = None,
        status_classes: Sequence[str] = (),
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """The newest ``limit`` kept entries, newest first.

        Args:
            category: only this category (plugin, api, agent, ...)
            status_classes: only these status classes ("2xx" ... "5xx"); empty for all
            limit: Maximum entries to return
        """
        return [
            entry for entry in reversed(self._audit_log)
            if (category is None or entry["category"] == category)
            and (not status_classes or self._status_class(entry["status_code"]) in status_classes)
        ][:limit]

    @staticmethod
    def _status_class(status_code: int) -> str:
        """A request left without an answer (0: the client went away) counts with the server errors, as the panel counts it."""
        return f"{status_code // 100}xx" if status_code else "5xx"

    @classmethod
    def get_instance(cls) -> Optional["SecurityAuditMiddleware"]:
        """Get the singleton instance."""
        return cls._instance


def get_security_audit_middleware() -> Optional[SecurityAuditMiddleware]:
    """Get the global security audit middleware instance."""
    return SecurityAuditMiddleware.get_instance()


def security_audit_log(request: Request) -> SecurityAuditMiddleware:
    """The audit log this server keeps; 404 while it keeps none (authentication or audit off)."""
    auth = request.app.state.config.auth
    audit = SecurityAuditMiddleware.get_instance()
    if not (auth.enabled and auth.endpoint_security.audit_enabled) or audit is None:
        raise HTTPException(status_code=404, detail="The security audit log is off")
    return audit


class RateLimitMiddleware:
    """
    Rate limiting middleware (Pure ASGI implementation).
    
    Limits requests per IP address to prevent abuse.
    Uses Pure ASGI for better performance (no BaseHTTPMiddleware overhead).
    """
    
    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = 60,
        enabled: bool = True,
    ):
        """
        Initialize rate limiter.
        
        Args:
            app: ASGI application
            requests_per_minute: Maximum requests per minute per IP
            enabled: Whether rate limiting is enabled
        """
        self.app = app
        self.requests_per_minute = requests_per_minute
        self.enabled = enabled
        self.request_counts: Dict[str, list] = defaultdict(list)
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # Clean up stale IPs every 5 minutes
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process request with rate limiting."""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        
        # Get client IP from scope
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        
        # Clean up old requests (older than 1 minute)
        current_time = time.time()
        self.request_counts[client_ip] = [
            req_time for req_time in self.request_counts[client_ip]
            if current_time - req_time < 60
        ]
        
        # Periodically remove stale IPs entirely (no requests in last minute)
        if current_time - self._last_cleanup > self._cleanup_interval:
            self._last_cleanup = current_time
            stale_ips = [ip for ip, times in self.request_counts.items() if not times]
            for ip in stale_ips:
                del self.request_counts[ip]
        
        # Check rate limit
        if len(self.request_counts[client_ip]) >= self.requests_per_minute:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            # Send 429 response directly
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    [b"content-type", b"application/json"],
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"detail": "Too many requests. Please try again later."}',
            })
            return
        
        # Record this request
        self.request_counts[client_ip].append(current_time)
        
        # Process request
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """
    Security headers middleware (Pure ASGI implementation).
    
    Adds security headers to all responses.
    Allows SAMEORIGIN for plugin panels that need iframe embedding.
    Uses Pure ASGI for better performance (no BaseHTTPMiddleware overhead).
    """
    
    def __init__(self, app: ASGIApp):
        self.app = app
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add security headers to response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Get path for plugin detection
        path = scope.get("path", "")
        # Allow iframes for panels: the shell shows every panel in a frame
        # (agent_system.ui.catalog lists them).
        is_embeddable_path = (
            path.startswith("/plugins/") or
            path.startswith("/ui/") or
            path.startswith("/debug/")
        )
        
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Get existing headers
                headers = list(message.get("headers", []))
                header_names = {h[0].lower() for h in headers}
                
                # Add security headers if not already present
                if b"x-content-type-options" not in header_names:
                    headers.append((b"x-content-type-options", b"nosniff"))
                
                if b"x-frame-options" not in header_names:
                    if is_embeddable_path:
                        headers.append((b"x-frame-options", b"SAMEORIGIN"))
                        headers.append((b"content-security-policy", b"frame-ancestors 'self'"))
                    else:
                        headers.append((b"x-frame-options", b"DENY"))
                        headers.append((b"content-security-policy", b"frame-ancestors 'none'"))
                
                if b"x-xss-protection" not in header_names:
                    headers.append((b"x-xss-protection", b"1; mode=block"))
                
                if b"strict-transport-security" not in header_names:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                
                if b"referrer-policy" not in header_names:
                    headers.append((b"referrer-policy", b"strict-origin-when-cross-origin"))
                
                message = {
                    "type": message["type"],
                    "status": message["status"],
                    "headers": headers,
                }
            
            await send(message)
        
        await self.app(scope, receive, send_with_headers)


def configure_cors(
    app,
    allow_origins: Optional[list] = None,
    allow_credentials: bool = True,
    allow_methods: Optional[list] = None,
    allow_headers: Optional[list] = None,
) -> None:
    """
    Configure CORS middleware.

    Args:
        app: FastAPI application
        allow_origins: Allowed origins (default: ["*"])
        allow_credentials: Allow credentials
        allow_methods: Allowed methods (default: ["*"])
        allow_headers: Allowed headers (default: ["*"])

    Security: credentials are never combined with a wildcard origin. Starlette's
    CORSMiddleware does NOT answer wildcard+credentials with a literal "*"; it
    reflects the *request* Origin and emits Access-Control-Allow-Credentials:true
    whenever a cookie is present. So allow_origins=["*"] + allow_credentials=True
    lets any cross-origin page (e.g. another port/subdomain that still receives
    the SameSite=Lax auth cookie) read authenticated responses. When that
    combination is requested we drop credentials and warn; to use credentialed
    CORS, configure an explicit cors_origins allowlist (no "*").
    """
    resolved_origins = allow_origins or ["*"]
    if allow_credentials and "*" in resolved_origins:
        logger.warning(
            "CORS: allow_credentials=True is incompatible with wildcard origin "
            "'*' (Starlette reflects arbitrary origins with credentials). "
            "Disabling credentials for CORS. Set an explicit cors_origins "
            "allowlist to enable credentialed cross-origin requests."
        )
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_origins,
        allow_credentials=allow_credentials,
        allow_methods=allow_methods or ["*"],
        allow_headers=allow_headers or ["*"],
    )
    logger.info(
        "CORS middleware configured (origins=%s, credentials=%s)",
        resolved_origins,
        allow_credentials,
    )


def configure_security_middleware(
    app,
    auth_config: Optional["AuthConfig"] = None,
    rate_limit_enabled: bool = True,
    requests_per_minute: int = 60,
    security_headers_enabled: bool = True,
    trusted_hosts: Optional[list] = None,
    audit_enabled: bool = True,
) -> None:
    """
    Configure all security middleware.
    
    Args:
        app: FastAPI application
        auth_config: Authentication configuration for endpoint security enforcement
        rate_limit_enabled: Enable rate limiting
        requests_per_minute: Maximum requests per minute per IP
        security_headers_enabled: Enable security headers
        trusted_hosts: List of trusted host patterns (e.g., ["*.example.com"])
        audit_enabled: Enable security audit logging
    """
    # Endpoint security enforcement (must be early to block unauthorized requests)
    if auth_config and auth_config.enabled:
        app.add_middleware(
            EndpointSecurityMiddleware,
            auth_config=auth_config,
        )
        logger.info("Endpoint security enforcement middleware enabled")

    # Security audit. add_middleware prepends, so it wraps the endpoint security (keeping its
    # 401/403 and reusing the account it looked up) and sits inside the rate limiter and host
    # check on purpose: a flood they refuse must not reach the audit's buffer, file and lookups.
    if audit_enabled and auth_config and auth_config.enabled:
        app.add_middleware(SecurityAuditMiddleware, auth_config=auth_config)
        logger.info("Security audit middleware enabled")

    # Rate limiting
    if rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=requests_per_minute,
            enabled=True,
        )
        logger.info(f"Rate limiting enabled: {requests_per_minute} requests/minute")
    
    # Security headers
    if security_headers_enabled:
        app.add_middleware(SecurityHeadersMiddleware)
        logger.info("Security headers middleware enabled")
    
    # Trusted host middleware
    if trusted_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=trusted_hosts,
        )
        logger.info(f"Trusted host middleware enabled: {trusted_hosts}")
