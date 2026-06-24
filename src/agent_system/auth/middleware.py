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
from typing import Dict, List, Optional, Any, TYPE_CHECKING
from urllib.parse import unquote, parse_qs
import logging
from logging.handlers import RotatingFileHandler

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send, Message

if TYPE_CHECKING:
    from agent_system.config import AuthConfig

logger = logging.getLogger(__name__)


# Role hierarchy: higher value = more permissions
ROLE_HIERARCHY = {
    "guest": 1,
    "user": 2,
    "admin": 3,
}


class EndpointSecurityMiddleware:
    """
    Endpoint security enforcement middleware (Pure ASGI implementation).
    
    Enforces endpoint_security rules from config.yaml by checking
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
        import fnmatch
        import re
        
        self._compiled_patterns = []
        
        for rule in self.auth_config.endpoint_security.rules:
            pattern = rule.pattern.strip()
            
            # Parse method prefix if present (e.g., "POST /run")
            method = "*"
            path_pattern = pattern
            
            parts = pattern.split(" ", 1)
            if len(parts) == 2 and parts[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "*"):
                method = parts[0].upper()
                path_pattern = parts[1]
            
            # Convert glob pattern to regex
            regex_pattern = fnmatch.translate(path_pattern)
            
            # Only remove \Z for wildcard patterns (to allow prefix matching)
            # For exact matches (no wildcards), keep \Z for exact match
            if '*' in path_pattern or '?' in path_pattern:
                regex_pattern = regex_pattern.replace(r'\Z', '')
            
            try:
                compiled = re.compile(regex_pattern, re.IGNORECASE)
                self._compiled_patterns.append((compiled, method, rule))
            except re.error as e:
                logger.warning(f"Invalid pattern '{pattern}': {e}")
    
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
        for compiled_pattern, rule_method, rule in self._compiled_patterns:
            if rule_method != "*" and rule_method != method:
                continue
            
            if compiled_pattern.match(path):
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
        3. ?token=<token> query parameter (JWT, for WebSocket/SSE connections)
        4. X-API-Key header (API key, looked up in UserDatabase)

        JWT validation failures fall through to the X-API-Key lookup so that
        clients which combine an invalid/expired session with a valid API key
        (e.g. a still-mounted browser cookie alongside a programmatic header)
        are still authenticated. If neither path yields a valid user, returns
        (None, None).

        Returns:
            Tuple of (username, role) or (None, None) if not authenticated
        """
        from jose import jwt, JWTError

        headers = dict(scope.get("headers", []))
        token = None

        # 1. Try Authorization header (highest priority)
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

        # 2. Try cookie
        if not token:
            cookie_header = headers.get(b"cookie", b"").decode("utf-8", errors="ignore")
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("access_token="):
                    token = part[13:].strip()
                    break

        # 3. Try query parameter (for SSE/WebSocket where headers may not be available)
        if not token:
            query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
            if query_string:
                query_params = parse_qs(query_string)
                token_list = query_params.get("token", [])
                if token_list:
                    token = token_list[0].strip()

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
                role = payload.get("role", "user")

                if not username or not isinstance(username, str):
                    logger.debug("JWT token has invalid or missing 'sub' claim")
                elif not all(c.isalnum() or c in '_-.' for c in username):
                    logger.warning("JWT token has invalid username format")
                else:
                    if not isinstance(role, str) or role.lower() not in ROLE_HIERARCHY:
                        logger.debug(f"JWT token has unknown role: {role}, defaulting to 'user'")
                        role = "user"
                    return (username, role)
            except JWTError as e:
                logger.debug(f"JWT token error: {e}")

        # 4. Try X-API-Key header (API key auth via UserDatabase).
        # Count duplicate X-API-Key headers BEFORE collapsing to a dict —
        # Python dict() keeps the LAST tuple, while Starlette's Headers.get
        # (used by downstream FastAPI dependencies) returns the FIRST. Sending
        # two values could otherwise authenticate one user at the middleware
        # and a different user at the endpoint. Reject the ambiguous case.
        raw_headers = scope.get("headers", [])
        api_key_header_count = sum(1 for h in raw_headers if h[0].lower() == b"x-api-key")
        if api_key_header_count > 1:
            logger.warning("Multiple X-API-Key headers received; rejecting request")
            return (None, None)
        api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore").strip()
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
            # DB-side failure (corrupted file, locked, etc.) — loud, no raw key.
            logger.error(
                "API-Key DB lookup failed (%s); requests with X-API-Key will return 401",
                exc.__class__.__name__,
            )
            return (None, None)

        # Always verify, even on miss, to equalise timing between hit/miss.
        stored_hash = user_in_db.api_key if user_in_db and user_in_db.api_key else self._DUMMY_API_KEY_HASH
        verified = verify_api_key(api_key, stored_hash)
        if not user_in_db or not verified or not user_in_db.is_active:
            return (None, None)

        try:
            username = user_in_db.username
            if not isinstance(username, str) or not all(
                c.isalnum() or c in "_-." for c in username
            ):
                logger.warning("API-Key user has invalid username format")
                return (None, None)

            role_obj = user_in_db.role
            role = role_obj.value if hasattr(role_obj, "value") else str(role_obj)
            if not isinstance(role, str) or role.lower() not in ROLE_HIERARCHY:
                logger.warning(
                    "API-Key user has unknown role %r; defaulting to 'user'",
                    role,
                )
                role = "user"

            return (username, role)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "API-Key user record could not be normalised (%s)",
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
        username, user_role = self._extract_user_info(scope)
        
        # No user and auth required
        if username is None:
            # Check if anonymous access is allowed for this endpoint
            if self.auth_config.anonymous_access.enabled:
                import fnmatch
                for allowed in self.auth_config.anonymous_access.allowed_endpoints:
                    allowed = allowed.strip()
                    allowed_method = "*"
                    allowed_path = allowed
                    
                    parts = allowed.split(" ", 1)
                    if len(parts) == 2:
                        allowed_method = parts[0].upper()
                        allowed_path = parts[1]
                    
                    if (allowed_method == "*" or allowed_method == method.upper()) and \
                       fnmatch.fnmatch(path, allowed_path):
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


class SecurityAuditMiddleware:
    """
    Security audit middleware (Pure ASGI implementation).
    
    Logs all HTTP requests for security auditing.
    Stores in-memory buffer for UI and writes to security.log file.
    """
    
    _security_logger: Optional[logging.Logger] = None
    _instance: Optional["SecurityAuditMiddleware"] = None
    
    def __init__(
        self,
        app: ASGIApp,
        enabled: bool = True,
        max_memory_entries: int = 1000,
        log_allowed: bool = True,
    ):
        """
        Initialize security audit middleware.
        
        Args:
            app: ASGI application
            enabled: Whether audit logging is enabled
            max_memory_entries: Maximum entries to keep in memory
            log_allowed: Whether to log allowed requests (vs only denied)
        """
        self.app = app
        self.enabled = enabled
        self._audit_log: List[Dict[str, Any]] = []
        self._max_memory_entries = max_memory_entries
        self._log_allowed = log_allowed
        # Lazy handle for X-API-Key → username resolution against UserDatabase.
        self._user_db: Optional[Any] = None
        self._setup_security_logger()
        SecurityAuditMiddleware._instance = self

    def _get_user_db(self) -> Any:
        """Lazy-load the global UserDatabase singleton (see EndpointSecurityMiddleware)."""
        if self._user_db is None:
            from agent_system.auth.database import get_db
            self._user_db = get_db()
        return self._user_db
    
    def _setup_security_logger(self) -> None:
        """Setup dedicated security audit file logger."""
        if SecurityAuditMiddleware._security_logger is not None:
            return
        
        security_logger = logging.getLogger("agent_system.security.audit")
        security_logger.setLevel(logging.INFO)
        security_logger.propagate = False
        
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        
        handler = RotatingFileHandler(
            log_dir / "security.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        security_logger.addHandler(handler)
        
        SecurityAuditMiddleware._security_logger = security_logger
        logger.info("Security audit logger initialized: logs/security.log")
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process request with audit logging."""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        
        # Extract request info
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        
        # Extract user from JWT token in headers
        user_id = self._extract_user_from_headers(scope)
        
        # Track response status
        response_status = 0
        
        async def send_wrapper(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)
            await send(message)
        
        start_time = time.time()
        
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.time() - start_time) * 1000
            
            # Determine category
            category = self._get_category(path)
            
            # Log the access
            allowed = 200 <= response_status < 400
            self._audit_access(
                path=path,
                method=method,
                user_id=user_id,
                client_ip=client_ip,
                status_code=response_status,
                duration_ms=duration_ms,
                category=category,
                allowed=allowed
            )
    
    def _extract_user_from_headers(self, scope: Scope) -> str:
        """Extract username from JWT token (Bearer/cookie) or X-API-Key header.

        Audit-only path: returns the username for logging, does NOT authorize.
        JWT extraction is unverified (decode-only) because EndpointSecurityMiddleware
        is responsible for the actual auth decision; we just want a name in the log.

        For X-API-Key requests we still look up the hash in UserDatabase so the
        audit log shows the real username instead of 'anonymous' (without that
        lookup the audit log would lie about who hit /run).
        """
        headers = dict(scope.get("headers", []))

        # Try Authorization header first
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
        token = None

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            # Try cookie
            cookie_header = headers.get(b"cookie", b"").decode("utf-8", errors="ignore")
            if cookie_header:
                for cookie in cookie_header.split(";"):
                    cookie = cookie.strip()
                    if cookie.startswith("access_token="):
                        token = cookie[13:]
                        break

        if token:
            try:
                # Decode JWT without verification (we just want the username for logging)
                import base64
                import json

                # JWT format: header.payload.signature
                parts = token.split(".")
                if len(parts) >= 2:
                    # Decode payload (add padding if needed)
                    payload_b64 = parts[1]
                    padding = 4 - len(payload_b64) % 4
                    if padding != 4:
                        payload_b64 += "=" * padding
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                    sub = payload.get("sub")
                    if sub:
                        return sub
            except Exception:
                pass

        # Fall back to X-API-Key lookup so audit log reflects the real user.
        # Mirror the strict checks of EndpointSecurityMiddleware._lookup_api_key
        # so the audit log never attributes a request to a user whose key
        # would actually be rejected (inactive account, hash collision, etc.).
        raw_headers = scope.get("headers", [])
        if sum(1 for h in raw_headers if h[0].lower() == b"x-api-key") > 1:
            # Ambiguous request — match EndpointSecurityMiddleware: stay anonymous.
            return "anonymous"
        api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore").strip()
        if api_key:
            try:
                from agent_system.auth.security import hash_api_key, verify_api_key
                user = self._get_user_db().get_user_by_api_key(hash_api_key(api_key))
                if (
                    user
                    and user.username
                    and user.api_key
                    and user.is_active
                    and verify_api_key(api_key, user.api_key)
                ):
                    return user.username
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Audit user-lookup failed (%s); logging as anonymous",
                    exc.__class__.__name__,
                )

        return "anonymous"
    
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
        elif path.startswith("/mcp/"):
            return "mcp"
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
        # Skip static file logging (too noisy)
        if category == "static":
            return
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        entry = {
            "timestamp": timestamp,
            "path": path,
            "method": method,
            "user_id": user_id,
            "client_ip": client_ip,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
            "category": category,
            "allowed": allowed
        }
        
        # Keep limited entries in memory for UI display
        self._audit_log.append(entry)
        if len(self._audit_log) > self._max_memory_entries:
            self._audit_log = self._audit_log[-self._max_memory_entries:]
        
        # Log to file
        if SecurityAuditMiddleware._security_logger:
            status_str = "ALLOWED" if allowed else "DENIED"
            SecurityAuditMiddleware._security_logger.log(
                logging.INFO if allowed else logging.WARNING,
                f"{status_str} | {method} {path} | user={user_id} | ip={client_ip} | "
                f"status={status_code} | {duration_ms:.1f}ms | {category}"
            )
    
    def get_audit_log(
        self,
        category: Optional[str] = None,
        limit: int = 100,
        status_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get recent audit log entries from memory buffer.
        
        Args:
            category: Filter by category (plugin, api, agent, etc.)
            limit: Maximum entries to return
            status_filter: Comma-separated status code ranges (2xx,3xx,4xx,5xx) or None for all
        """
        entries = self._audit_log
        
        if category:
            entries = [e for e in entries if e["category"] == category]
        
        # Support multiple status filters (comma-separated: "2xx,4xx,5xx")
        if status_filter:
            filters = [f.strip() for f in status_filter.split(',')]
            if filters:
                filtered_entries = []
                for entry in entries:
                    status_code = entry["status_code"]
                    for filter_range in filters:
                        if filter_range == "2xx" and 200 <= status_code < 300:
                            filtered_entries.append(entry)
                            break
                        elif filter_range == "3xx" and 300 <= status_code < 400:
                            filtered_entries.append(entry)
                            break
                        elif filter_range == "4xx" and 400 <= status_code < 500:
                            filtered_entries.append(entry)
                            break
                        elif filter_range == "5xx" and status_code >= 500:
                            filtered_entries.append(entry)
                            break
                entries = filtered_entries
        
        return entries[-limit:]
    
    @classmethod
    def get_instance(cls) -> Optional["SecurityAuditMiddleware"]:
        """Get the singleton instance."""
        return cls._instance


def get_security_audit_middleware() -> Optional[SecurityAuditMiddleware]:
    """Get the global security audit middleware instance."""
    return SecurityAuditMiddleware.get_instance()


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
        # Allow iframes for plugin panels and debug dashboards
        is_embeddable_path = (
            path.startswith("/plugins/") or 
            path.startswith("/debug/") or
            path.startswith("/api/security/audit")
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
    
    # Security audit (must be first to capture all requests)
    if audit_enabled:
        app.add_middleware(
            SecurityAuditMiddleware,
            enabled=True,
        )
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