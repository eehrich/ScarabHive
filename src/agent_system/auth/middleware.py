"""
Security Middleware

Provides rate limiting, CORS, and security headers middleware.
Uses Pure ASGI implementation for better performance (avoids BaseHTTPMiddleware overhead).
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, Optional
import logging

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send, Message


logger = logging.getLogger(__name__)


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
        is_embeddable_path = path.startswith("/plugins/") or path.startswith("/debug/")
        
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
) -> None:
    """
    Configure all security middleware.
    
    Args:
        app: FastAPI application
        rate_limit_enabled: Enable rate limiting
        requests_per_minute: Maximum requests per minute per IP
        security_headers_enabled: Enable security headers
        trusted_hosts: List of trusted host patterns (e.g., ["*.example.com"])
    """
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
