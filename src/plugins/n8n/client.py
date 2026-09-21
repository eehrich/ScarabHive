"""The two ways the plugin talks to n8n: the instance MCP and the public API.

Everything that WRITES goes through the instance MCP (``/mcp-server/http``):
create, update, tag, test. Everything that only READS and runs often goes
through the public API (``/api/v1``), for two measured reasons: the MCP
allows 100 requests per IP and five minutes while the public API has no
limit, and the MCP only sees workflows exposed to it (docs/n8n_facts.md
M-MCP-24, F-AUTH5, M-MCP-20). The public key therefore holds read scopes only,
and this client has no way to send anything but a GET with it.

One ``httpx.AsyncClient`` and one MCP handshake per process: a handshake
costs two of the hundred requests (initialize, initialized). n8n 2.39.9's MCP
is stateless and hands out no session id, so "initialized" is its own flag.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp-server/http"
API_PREFIX = "/api/v1"
PROTOCOL_VERSION = "2025-06-18"
# A 429 is waited out only when that is short; a longer wait belongs to the
# model, which can do something else meanwhile.
MAX_RETRY_AFTER_S = 10.0


class N8nError(Exception):
    """Something went wrong in a way the model can act on; the text says how.

    Never carries a key, a header or a cookie."""


class N8nNotFound(N8nError):
    """The public API answered 404 for this path."""


@dataclass
class McpResult:
    """One ``tools/call`` answer, before the per-tool reading of it."""

    payload: Any        # structuredContent, else the text parsed as JSON, else the text
    text: str
    is_error: bool


def _parse_rpc_body(response: httpx.Response) -> Optional[dict]:
    """A JSON-RPC message from a JSON body or from the last SSE ``data:`` line."""
    if not response.content:
        return None
    try:
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            messages = [line[5:].strip() for line in response.text.splitlines()
                        if line.startswith("data:") and line[5:].strip()]
            message = json.loads(messages[-1]) if messages else None
        else:
            message = response.json()
    except ValueError:
        raise _not_json("n8n MCP") from None
    if message is not None and not isinstance(message, dict):
        raise _not_json("n8n MCP")
    return message


def _not_json(what: str) -> N8nError:
    return N8nError(f"{what} answered something that is not JSON -- a proxy or login page in front "
                    f"of n8n? N8N_BASE_URL must point at n8n itself")


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.headers.get("retry-after", "60"))
    except ValueError:
        return 60.0


def to_result(raw: dict) -> McpResult:
    """``{"content": [...], "structuredContent": ..., "isError": ...}`` -> McpResult."""
    text = "".join(part["text"] for part in raw.get("content") or []
                   if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    payload: Any = raw.get("structuredContent")
    if not isinstance(payload, (dict, list)):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
    return McpResult(payload=payload, text=text, is_error=bool(raw.get("isError")))


def mcp_error(tool: str, result: McpResult) -> Optional[str]:
    """The error in a tool answer, or None when the answer is a result.

    One rule for all tools is wrong in both directions (M-MCP-6, M-MCP-31,
    M-MCP-44): n8n reports some failures without ``isError``, a failed TEST is
    a result and not a tool failure, and ``get_node_types`` mixes valid
    definitions with ``# Errors`` sections in one successful text.
    """
    # get_node_types needs no rule of its own: its text is never a JSON object,
    # so a "# Errors" section in it cannot count as a failure here --
    # validate.py reads those per node.
    payload = result.payload if isinstance(result.payload, dict) else {}
    if tool in ("validate_workflow", "validate_node_config") and "valid" in payload:
        # Code n8n cannot even parse comes back with isError AND a verdict
        # {"valid": false, "errors": [...]}: a finding, not a broken tool.
        return None
    if result.is_error:
        return (payload.get("error") or result.text or "n8n reported an error")[:500]
    if tool == "test_workflow":
        if payload.get("executionId"):
            return None
        if payload.get("success") is False or payload.get("status") == "error" or payload.get("error"):
            return str(payload.get("error") or "test_workflow did not start an execution")[:500]
        return None
    if tool in ("update_workflow", "create_workflow_from_code"):
        if payload.get("error") and not payload.get("workflowId"):
            return str(payload["error"])[:500]
        return None
    if payload.get("error"):
        return str(payload["error"])[:500]
    return None


class N8nClient:
    """The instance MCP (bearer key) and the public API (read-only key)."""

    def __init__(self, base_url: str, api_key: str, mcp_key: str, *,
                 timeout: float = 60.0, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._mcp_key = mcp_key
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)
        self._session_id: Optional[str] = None
        self._initialized = False
        self._session_lock = asyncio.Lock()
        self._next_id = 0
        self.server_info: dict = {}

    async def aclose(self) -> None:
        await self._http.aclose()
        self._session_id, self._initialized = None, False

    # ── public API ────────────────────────────────────────────────────────

    async def api_get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET ``/api/v1<path>``. The only verb this client has for the public API."""
        try:
            response = await self._http.get(
                API_PREFIX + path, params={k: v for k, v in (params or {}).items() if v is not None},
                headers={"X-N8N-API-KEY": self._api_key, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from None
        if response.status_code == 401:
            raise N8nError("the n8n public API refused the key -- check N8N_API_KEY")
        if response.status_code == 403:
            raise N8nError(f"the n8n public API key lacks the scope for GET {path}")
        if response.status_code == 404:
            raise N8nNotFound(f"not found: GET {path}")
        if response.status_code >= 400:
            raise N8nError(self._http_error(response, f"GET {path}"))
        try:
            return response.json()
        except ValueError:
            raise _not_json("the n8n public API") from None

    # ── instance MCP ──────────────────────────────────────────────────────

    async def mcp_call(self, tool: str, arguments: dict, *, timeout: Optional[float] = None) -> McpResult:
        """``tools/call`` on the instance MCP. Raises N8nError for transport and
        protocol failures; what the tool itself answered comes back unread."""
        body = {"jsonrpc": "2.0", "id": self._take_id(), "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
        message = await self._rpc(body, timeout=timeout)
        if "error" in message:
            error = message["error"]
            detail = error.get("message", error) if isinstance(error, dict) else error
            raise N8nError(f"n8n MCP {tool}: {detail}"[:500])
        result = message.get("result") or {}
        if not isinstance(result, dict):
            raise _not_json("n8n MCP")
        return to_result(result)

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _rpc(self, body: dict, *, timeout: Optional[float]) -> dict:
        for attempt in range(2):
            session_id = await self._ensure_session()
            response = await self._send(body, timeout=timeout, session_id=session_id)
            if response.status_code in (400, 404) and attempt == 0 and session_id \
                    and "disabled" not in response.text:
                # The server forgot the session this request carried (idle
                # timeout, restart): a new one, and the call once more.
                await self._forget_session(session_id)
                continue
            self._raise_for_mcp(response)
            message = _parse_rpc_body(response)
            if message is None:
                raise N8nError("n8n MCP answered without a message")
            return message
        raise N8nError("n8n MCP kept rejecting the request")

    async def _ensure_session(self) -> Optional[str]:
        """Handshake once; returns the session id to send, None on a stateless server."""
        async with self._session_lock:
            if self._initialized:
                return self._session_id
            init = {"jsonrpc": "2.0", "id": self._take_id(), "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                               "clientInfo": {"name": "scarabhive-n8n", "version": "1"}}}
            response = await self._send(init, timeout=None, session_id=None)
            self._raise_for_mcp(response)
            message = _parse_rpc_body(response) or {}
            result = message.get("result")
            info = result.get("serverInfo") if isinstance(result, dict) else None
            self.server_info = info if isinstance(info, dict) else {}
            self._session_id = response.headers.get("mcp-session-id")
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"},
                             timeout=None, session_id=self._session_id)
            self._initialized = True
            return self._session_id

    async def _forget_session(self, stale: str) -> None:
        """Drop a session only if no other call has replaced it meanwhile."""
        async with self._session_lock:
            if self._session_id == stale:
                self._session_id, self._initialized = None, False

    async def _send(self, body: dict, *, timeout: Optional[float], session_id: Optional[str]) -> httpx.Response:
        """One POST. A short 429 is waited out once; a longer one goes to the model."""
        for attempt in range(2):
            response = await self._post(body, timeout=timeout, session_id=session_id)
            if response.status_code != 429:
                return response
            wait = _retry_after(response)
            if attempt == 1 or wait > MAX_RETRY_AFTER_S:
                break
            await asyncio.sleep(wait)
        raise N8nError(f"n8n MCP rate limit reached (100 requests per IP and 5 minutes); "
                       f"retry in {int(wait)} s")

    async def _post(self, body: dict, *, timeout: Optional[float], session_id: Optional[str]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._mcp_key}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        kwargs: dict = {"headers": headers, "json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return await self._http.post(MCP_PATH, **kwargs)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from None
        except ValueError:
            # JSON has no NaN or Infinity; httpx refuses to encode them.
            raise N8nError("an argument is not valid JSON (NaN or Infinity?)") from None

    def _raise_for_mcp(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code in (401, 403):
            raise N8nError("n8n MCP refused the key -- check N8N_MCP_KEY (it may have been rotated)")
        if response.status_code == 404 and "disabled" in response.text:
            raise N8nError("the n8n instance MCP is switched off -- set N8N_MCP_MANAGED_BY_ENV and "
                           "N8N_MCP_ACCESS_ENABLED to true in n8n's docker-compose.yml")
        raise N8nError(self._http_error(response, "n8n MCP"))

    def _http_error(self, response: httpx.Response, what: str) -> str:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            detail = body.get("message", "")
        else:
            detail = "no JSON" if response.status_code >= 500 else response.text[:120]
        return f"{what} answered HTTP {response.status_code}: {detail}"[:300]

    def _unreachable(self, exc: Exception) -> N8nError:
        return N8nError(f"n8n not reachable at {self.base_url} ({type(exc).__name__}) -- is the "
                        f"container running? {self.base_url}/healthz tells")
