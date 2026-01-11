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
import logging
from logging.handlers import RotatingFileHandler

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send, Message

if TYPE_CHECKING:
    from agent_system.config import AuthConfig

logger = logging.getLogger(__name__)


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
        self._setup_security_logger()
        SecurityAuditMiddleware._instance = self
    
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
        """Extract username from JWT token in Authorization header or cookie."""
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
                    return payload.get("sub", "anonymous")
            except Exception:
                pass
        
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
            level = "INFO" if allowed else "WARNING"
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
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_credentials=allow_credentials,
        allow_methods=allow_methods or ["*"],
        allow_headers=allow_headers or ["*"],
    )
    logger.info("CORS middleware configured")


def configure_security_middleware(
    app,
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
        rate_limit_enabled: Enable rate limiting
        requests_per_minute: Maximum requests per minute per IP
        security_headers_enabled: Enable security headers
        trusted_hosts: List of trusted host patterns (e.g., ["*.example.com"])
        audit_enabled: Enable security audit logging
    """
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