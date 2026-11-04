"""
Security Middleware

Provides rate limiting, CORS, security headers, and audit middleware.
Uses Pure ASGI implementation for better performance (avoids BaseHTTPMiddleware overhead).

One module per middleware; this package re-exports them, so
``from agent_system.auth.middleware import X`` reads as before the split:

    endpoint_security.py  EndpointSecurityMiddleware (the endpoint_security rules)
    audit.py              SecurityAuditMiddleware, logs/security.log, the audit panel's log
    rate_limit.py         RateLimitMiddleware
    headers.py            SecurityHeadersMiddleware
    setup.py              configure_cors, configure_security_middleware
"""

from agent_system.auth.enforcement import (  # noqa: F401 -- re-exported: the old module's names
    ROLE_HIERARCHY,
    anonymous_may_reach,
    anonymous_meets_role,
    compile_endpoint_rules,
    first_matching_rule,
)
from agent_system.auth.middleware.audit import (
    AUDIT_CATEGORIES,
    AUDIT_LOG_PATH,
    AUDIT_LOGGER_NAME,
    AUDIT_STATUS_CLASSES,
    SecurityAuditMiddleware,
    get_security_audit_middleware,
    log_field,
    security_audit_log,
    security_audit_logger,
)
from agent_system.auth.middleware.endpoint_security import IDENTITY_SCOPE_KEY, EndpointSecurityMiddleware
from agent_system.auth.middleware.headers import SecurityHeadersMiddleware
from agent_system.auth.middleware.rate_limit import RateLimitMiddleware
from agent_system.utils.logging import KeyInPathFilter, loggable_path  # noqa: F401 -- re-exported, as above
from agent_system.auth.middleware.setup import configure_cors, configure_security_middleware

__all__ = [
    "AUDIT_CATEGORIES",
    "AUDIT_LOG_PATH",
    "AUDIT_LOGGER_NAME",
    "AUDIT_STATUS_CLASSES",
    "IDENTITY_SCOPE_KEY",
    "ROLE_HIERARCHY",
    "EndpointSecurityMiddleware",
    "RateLimitMiddleware",
    "SecurityAuditMiddleware",
    "SecurityHeadersMiddleware",
    "configure_cors",
    "configure_security_middleware",
    "get_security_audit_middleware",
    "log_field",
    "security_audit_log",
    "security_audit_logger",
]
