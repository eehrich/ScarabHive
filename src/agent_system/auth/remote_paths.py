"""Clients other than this machine reach only the paths the operator lists.

An API that listens on the network for one purpose -- a platform's webhook --
would otherwise offer everything else to the network too: the login, the
self-registration, every agent. ``network.remote_paths`` names what a remote
client may reach; everything else answers it 404, before any other layer
sees the request. Clients on this machine (loopback) are not affected.
Unset, nothing is restricted (the API's behaviour before this setting).

The client is the ASGI scope's: the direct peer, or what a proxy on loopback
forwards (uvicorn trusts forwarded headers from 127.0.0.1 only). "This machine"
means loopback: the machine's own LAN address counts as remote, and a plain
port forwarder on loopback (``netsh portproxy``, ``ssh -L``) makes whatever it
forwards look local.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Iterable

from starlette.responses import PlainTextResponse


def is_local(scope: dict[str, Any]) -> bool:
    """A network peer always has an IP address here; anything else -- no
    client (a Unix socket), a name ("testclient") -- is an in-process caller."""
    client = scope.get("client")
    if not client:
        return True
    try:
        return ipaddress.ip_address(str(client[0])).is_loopback
    except ValueError:
        return True


def install(app: Any, network: Any) -> None:
    """Called last in ``build_app``: add_middleware prepends, so the guard is the
    outermost layer and a remote client meets it before any other."""
    paths = getattr(network, "remote_paths", None)
    if paths is not None:
        app.add_middleware(RemotePathGuard, paths=paths)


class RemotePathGuard:
    def __init__(self, app: Any, paths: Iterable[str]) -> None:
        self.app = app
        # Exact paths: a prefix would let "/x/../auth/register" style tricks or
        # a sibling route through; a listed path reaches its one route only.
        self.paths = frozenset(str(p) for p in paths)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and not is_local(scope) and scope.get("path") not in self.paths:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)
