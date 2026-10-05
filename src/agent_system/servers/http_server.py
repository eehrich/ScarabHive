"""Serve one tool server over HTTP: ``GET /health`` and ``POST /call``.

Used by the ``__main__`` of several plugins and by the http_server plugin. The
guards here are shared by both:

- without an API key the server binds only a loopback address, and ``/call``
  answers only requests whose Host header names a loopback host (a web page
  reaching 127.0.0.1 through DNS rebinding sends its own host name);
- with a key, ``/call`` needs ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``;
- parameters starting with ``_`` are dropped: the agent runtime sets them
  (``_session_id``, ``_user_id``) and plugins separate sessions and users by
  them, so an HTTP caller must not claim them.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
import uvicorn

from ..plugins import capabilities
from ..tools.base import ToolServer

AUTH_KEY_ENV = "HTTP_SERVER_AUTH_KEY"


class CallRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


def is_loopback_host(host: str) -> bool:
    """True if ``host`` (an address or ``localhost``) names this machine only."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal (e.g. a hostname) -- treat as non-loopback to be safe.
        return False


def caller_params(params: dict[str, Any]) -> dict[str, Any]:
    """The caller's parameters without the runtime's reserved ``_`` keys."""
    return {k: v for k, v in params.items() if not k.startswith("_")}


def _url_hostname(url: str) -> str:
    """The hostname of a URL ("//host:port" for a Host header); "" when malformed."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:  # e.g. "[::1"
        return ""


def check_bind_safety(host: str, auth_key: str | None) -> None:
    """Refuse to expose /call on a network interface without a key: it would
    let any network client run every tool of the served server."""
    if not is_loopback_host(host) and not auth_key:
        raise RuntimeError(
            f"http_server refuses to bind non-loopback host '{host}' "
            "without authentication: the /call endpoint would let any network "
            f"client invoke every wrapped tool. Set {AUTH_KEY_ENV}, "
            "or bind 127.0.0.1."
        )


def require_call_auth(auth_key: str | None):
    """FastAPI dependency guarding /call: the key when one is set, else a
    loopback Host header. The key is compared in constant time."""

    async def _require(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        authorization: str | None = Header(default=None),
        host: str | None = Header(default=None),
        origin: str | None = Header(default=None),
        content_type: str | None = Header(default=None),
    ) -> None:
        # A browser sends a cross-site POST without a preflight only with a
        # "simple" content type; FastAPI would parse its body as JSON anyway.
        # Requiring application/json forces the preflight, which goes unanswered.
        if (content_type or "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Content-Type must be application/json")
        if not auth_key:
            if not is_loopback_host(_url_hostname(f"//{host or ''}")):
                raise HTTPException(status_code=403, detail="Forbidden: host is not loopback")
            if origin is not None and not is_loopback_host(_url_hostname(origin)):
                raise HTTPException(status_code=403, detail="Forbidden: origin is not loopback")
            return
        provided = x_api_key
        if not provided and authorization and authorization.lower().startswith("bearer "):
            provided = authorization[len("bearer "):].strip()
        if not provided or not hmac.compare_digest(provided.encode("utf-8"), auth_key.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid API key")

    return _require


async def dispatch(server: Any, tool: str, params: dict[str, Any]) -> Any:
    """Call ``server`` the way the tool registry does: through call_with_status
    (it hands the plugin its ``_status``) when it has one, else call()."""
    if hasattr(server, "call_with_status"):
        return await server.call_with_status(tool, params)
    return await server.call(tool, params)


def create_app(server: ToolServer, auth_key: str | None) -> FastAPI:
    """The FastAPI app for ``server``: /health open, /call guarded."""
    app = FastAPI(title=f"Tool server: {server.name}")

    @app.get("/health")
    def health():
        return {"status": "ok", "server": server.name}

    @app.post("/call", dependencies=[Depends(require_call_auth(auth_key))])
    async def call(req: CallRequest):
        try:
            return await dispatch(server, req.tool, caller_params(req.params))
        except Exception as e:
            return {"error": f"Call failed: {e}", "tool": req.tool, "params": req.params}

    return app


async def serve_tool_server(
    server: ToolServer,
    host: str | None = None,
    port: int | None = None,
    auth_key: str | None = None,
) -> None:
    """Serve ``server`` until stopped. Binds 127.0.0.1 unless ``host`` says
    otherwise; the key defaults to the HTTP_SERVER_AUTH_KEY environment variable."""
    host = host or "127.0.0.1"
    auth_key = auth_key or os.getenv(AUTH_KEY_ENV) or None
    check_bind_safety(host, auth_key)
    config = uvicorn.Config(
        create_app(server, auth_key),
        host=host,
        port=port if port is not None else int(os.getenv("PORT", "9000")),
    )
    # The served plugin runs its start/stop hooks as under the tool registry.
    await capabilities.start_plugin(server)
    try:
        await uvicorn.Server(config).serve()
    finally:
        await capabilities.stop_plugin(server)
