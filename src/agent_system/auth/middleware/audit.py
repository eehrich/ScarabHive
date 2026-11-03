"""The security audit: every request (static files aside) to logs/security.log, the latest in memory."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING
from urllib.parse import quote

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_system.auth.middleware.endpoint_security import IDENTITY_SCOPE_KEY, EndpointSecurityMiddleware
from agent_system.utils.logging import KeyInPathFilter, loggable_path

if TYPE_CHECKING:
    from agent_system.config import AuthConfig

logger = logging.getLogger(__name__)

#: What the audit log sorts a request into; static files are never kept.
AUDIT_CATEGORIES = ("plugin", "api", "agent", "auth", "tools", "debug", "health", "other")
AUDIT_STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")
#: Where the Security Audit panel reads the log (api/admin_endpoints.py).
AUDIT_LOG_PATH = "/admin/security/audit"
AUDIT_LOGGER_NAME = "agent_system.security.audit"


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

