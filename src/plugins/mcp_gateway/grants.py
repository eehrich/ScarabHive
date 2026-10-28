"""What one run of an outside CLI may call through the gateway, behind a token only that run holds.

A caller in the API process (the one that starts the CLI) issues a grant for the run and revokes it when the run
ends; the CLI sends the token as ``Authorization: Bearer <token>``. Only the token's sha256 is kept, in memory --
a grant does not outlive the process, and another process cannot issue one the gateway would know.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from agent_system.core.cancellation import CancellationToken
from agent_system.servers.agent.components.tool_bindings import form_problem

#: Gateway instances serving now (an enabled plugin adds its name): a grant for one that does not serve is refused.
_serving: set[str] = set()
_grants: dict[str, "Grant"] = {}
_lock = threading.Lock()
_MISSING = object()


@dataclass
class Grant:
    gateway: str
    user_id: str
    tools: tuple[str, ...]          # fnmatch patterns on the flat tool name
    request_id: str                 # prefix of every call's request id
    pinned: dict[str, dict[str, Any]]  # the run's tool bindings: tool pattern -> {parameter: its only value}
    allowed: dict[str, dict[str, list[Any]]]  # tool pattern -> {path: allowed values}
    session_id: Optional[str]
    expires_at: float
    key: str                        # names the grant in logs, never the token
    cancel: CancellationToken = field(repr=False)
    calls: int = 0
    #: The loop its calls wait in (set by the first call): a revoke from another thread wakes it there.
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)

    def next_request_id(self) -> str:
        self.calls += 1
        return f"{self.request_id}_mcp{self.calls:03d}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_path(path: Any) -> None:
    parts = path.split(".") if isinstance(path, str) else []
    if not parts or any(not part or part == "[]" or "[" in part.removesuffix("[]") for part in parts):
        raise ValueError(f"allowed: {path!r} is no path (keys joined by '.', '[]' after a list, e.g. 'operations[].op')")


def issue(user_id: str, tools: list[str], *, request_id: str, pinned: Optional[dict[str, Any]] = None,
          allowed: Optional[dict[str, dict[str, list[Any]]]] = None, session_id: Optional[str] = None,
          ttl_s: float = 3600.0, gateway: str = "mcp_gateway") -> str:
    """A token for one run. ValueError for an argument that cannot hold, LookupError when ``gateway`` does not serve
    (not enabled, or not started in this process)."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id: the account the run's calls are made for")
    if not isinstance(tools, (list, tuple)) or not tools or not all(isinstance(t, str) and t for t in tools):
        raise ValueError("tools: a non-empty list of tool name patterns")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id: the prefix of the run's calls (cost attribution)")
    if not isinstance(ttl_s, (int, float)) or not math.isfinite(ttl_s) or ttl_s <= 0:
        raise ValueError("ttl_s: seconds the token holds, more than 0")
    problem = form_problem(pinned) if pinned else None
    if problem:
        raise ValueError(f"pinned: {problem}")
    pinned = {pattern: dict(values) for pattern, values in (pinned or {}).items()}
    allowed = dict(allowed or {})
    for pattern, rules in allowed.items():
        if not isinstance(pattern, str) or not pattern or not isinstance(rules, dict) or not rules:
            raise ValueError(f"allowed: {pattern!r} needs {{path: [values]}}")
        for path, values in rules.items():
            _check_path(path)
            if not isinstance(values, (list, tuple)) or not values:
                raise ValueError(f"allowed: {pattern} {path}: a non-empty list of values")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("session_id: a session id, or None")
    token = secrets.token_urlsafe(32)
    digest = _digest(token)
    grant = Grant(gateway=gateway, user_id=user_id, tools=tuple(tools), request_id=request_id, pinned=pinned,
                  allowed={p: {path: list(v) for path, v in rules.items()} for p, rules in allowed.items()},
                  session_id=session_id, expires_at=time.monotonic() + ttl_s, key=digest[:12],
                  cancel=CancellationToken(request_id))
    with _lock:  # under the lock that a stop takes: a grant cannot slip in beside a stopping gateway
        if gateway not in _serving:
            raise LookupError(f"mcp gateway {gateway!r} does not serve in this process (plugin not enabled?)")
        _sweep()
        _grants[digest] = grant
    return token


def revoke(token: str) -> bool:
    """End a grant: its token answers 401 from now on, a call of it still running is cancelled. False when there
    was none (unknown, revoked or expired before)."""
    with _lock:
        grant = _grants.pop(_digest(token), None) if isinstance(token, str) else None
    if grant is None:
        return False
    _cancel(grant)
    return True


def lookup(gateway: str, token: Optional[str]) -> Optional[Grant]:
    """The live grant of ``token`` on ``gateway``, or None."""
    if not token:
        return None
    with _lock:
        _sweep()
        grant = _grants.get(_digest(token))
    return grant if grant is not None and grant.gateway == gateway else None


def _sweep() -> None:
    now = time.monotonic()
    for digest in [d for d, g in _grants.items() if g.expires_at <= now]:
        _cancel(_grants.pop(digest))


def _cancel(grant: Grant) -> None:
    """Cancel the grant's token in the loop its calls wait in: an asyncio event set from another thread does not
    wake that loop."""
    loop = grant.loop
    try:
        running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and loop is not running and not loop.is_closed():
        loop.call_soon_threadsafe(grant.cancel.cancel)
    else:
        grant.cancel.cancel()


def serve(gateway: str, serving: bool) -> None:
    """Start or stop serving ``gateway``; stopping ends its grants (a later start serves new ones only)."""
    with _lock:
        if serving:
            _serving.add(gateway)
            return
        _serving.discard(gateway)
        ended = [_grants.pop(d) for d, g in list(_grants.items()) if g.gateway == gateway]
    for grant in ended:
        _cancel(grant)


def _walk(node: Any, parts: list[str], where: str) -> Iterator[tuple[str, Any]]:
    """(location, value) of every place ``parts`` names in ``node``; _MISSING where there is none."""
    if not parts:
        yield where, node
        return
    head, rest = parts[0], parts[1:]
    many = head.endswith("[]")
    key = head[:-2] if many else head
    here = f"{where}.{key}" if where else key
    child = node.get(key, _MISSING) if isinstance(node, dict) else _MISSING
    if child is _MISSING or (many and not isinstance(child, list)):
        yield here, _MISSING
    elif many:
        for index, item in enumerate(child):
            yield from _walk(item, rest, f"{here}[{index}]")
    else:
        yield from _walk(child, rest, here)


def refusal(grant: Grant, tool: str, arguments: dict[str, Any]) -> Optional[str]:
    """Why ``arguments`` break one of the grant's allowed values for ``tool``, or None. A place the rule names but
    the call leaves out is refused too: the tool's default could be a value the rule keeps out."""
    for pattern, rules in grant.allowed.items():
        if not fnmatch.fnmatchcase(tool, pattern):
            continue
        for path, values in rules.items():
            for where, value in _walk(arguments, path.split("."), ""):
                if value is _MISSING:
                    return f"{where} is required for this run, one of {values}"
                if value not in values:
                    return f"{where} must be one of {values} for this run, not {value!r}"
    return None
