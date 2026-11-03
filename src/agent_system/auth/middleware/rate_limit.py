"""Rate limiting per client address."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Dict

from starlette.types import ASGIApp, Receive, Scope, Send

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
        
        # Periodically remove stale IPs entirely (no requests in last minute). Judged by their last request:
        # the list of an IP that does not come back is never pruned above, so it is never empty -- asking for
        # an empty one kept every address that ever called, for the life of the process.
        if current_time - self._last_cleanup > self._cleanup_interval:
            self._last_cleanup = current_time
            stale_ips = [ip for ip, times in self.request_counts.items()
                         if ip != client_ip and (not times or current_time - times[-1] >= 60)]
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

