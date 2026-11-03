"""Putting the security middleware on an app: CORS, and the rest in the order they must wrap each other."""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from agent_system.auth.middleware.audit import SecurityAuditMiddleware
from agent_system.auth.middleware.endpoint_security import EndpointSecurityMiddleware
from agent_system.auth.middleware.headers import SecurityHeadersMiddleware
from agent_system.auth.middleware.rate_limit import RateLimitMiddleware

if TYPE_CHECKING:
    from agent_system.config import AuthConfig

logger = logging.getLogger(__name__)


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

    # Rate limiting. The anonymous share applies while anonymous access is on: otherwise a caller without
    # credentials reaches only the sign-in and the public routes, and the login page keeps the full limit.
    if rate_limit_enabled:
        anonymous = auth_config.anonymous_access if auth_config and auth_config.enabled else None
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=requests_per_minute,
            enabled=True,
            anonymous_multiplier=anonymous.rate_limit_multiplier if anonymous and anonymous.enabled else 1.0,
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
