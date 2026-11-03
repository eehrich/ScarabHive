"""Security headers on every response."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


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

