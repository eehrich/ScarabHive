"""One connection to one external MCP server, driven by the official SDK.

Why this is not just "call the SDK where the old client was called":

The SDK's transports and ``ClientSession`` are ``anyio`` context managers, and
anyio cancel scopes must be exited by the same task that entered them. Closing
a session from a different task raises

    RuntimeError: Attempted to exit cancel scope in a different task than it
    was entered in

which is exactly what the previous design did -- a dict of client objects with
``add_client``/``remove_client`` called from request handlers, from the CLI and
from app shutdown, all different tasks.

So a connection owns a single task. That task opens the transport, opens the
session, initialises, and then serves commands from a queue until it is told to
stop. Every public method here only ever puts work on that queue and awaits the
answer, which makes the whole object safe to use -- and to close -- from any
task.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

from agent_system.tools.base import ToolDef
from agent_system.tools.status import StatusPhase, publish_status

logger = logging.getLogger(__name__)

#: Transport names accepted in ``config/mcp_servers.yaml``, mapped to the SDK.
#: "streaming" is this project's historical name for streamable HTTP, and
#: "smithery" was an alias for it. "http" used to mean bare JSON-RPC POSTs,
#: which no current server answers (measured: HTTP 406) -- it is treated as
#: streamable HTTP too, which is what those endpoints actually speak today.
_STREAMABLE_HTTP_ALIASES = {"streaming", "streamable_http", "streamable-http", "http", "smithery"}
_SSE_ALIASES = {"sse", "http_sse", "http+sse"}
_STDIO_ALIASES = {"stdio", "local"}


class MCPConnectionError(RuntimeError):
    """Raised when a server cannot be reached or refuses the handshake."""


@dataclass
class _Command:
    """A unit of work for the connection task."""

    run: Callable[[Any], Awaitable[Any]]
    future: asyncio.Future = field(repr=False)


class ServerConnection:
    """A live session with one external MCP server.

    The session lives inside :meth:`_run`; nothing outside that task ever
    touches it. ``start``/``stop``/``list_tools``/``call_tool`` are safe to call
    from anywhere.
    """

    def __init__(
        self,
        name: str,
        config: Any,
        *,
        ssl_verify: bool = True,
        timeout: float = 30.0,
        client_name: str = "AgentSystem",
    ) -> None:
        self.name = name
        self.config = config
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        self.client_name = client_name

        self._commands: asyncio.Queue[Optional[_Command]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._ready: Optional[asyncio.Future] = None
        self._server_info: Dict[str, Any] = {}
        self._capabilities: Dict[str, Any] = {}
        self._protocol_version: Optional[str] = None

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def server_info(self) -> Dict[str, Any]:
        return dict(self._server_info)

    @property
    def capabilities(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    @property
    def protocol_version(self) -> Optional[str]:
        return self._protocol_version

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Connect and complete the MCP handshake, or raise.

        Returns only once the server has answered ``initialize`` -- a connection
        that reports success has really shaken hands, so a failing server shows
        up here and not later as a mysterious empty tool list.
        """
        if self.connected:
            return

        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = loop.create_task(self._run(), name=f"mcp-client:{self.name}")

        try:
            await self._ready
        except Exception:
            await self._reap_task()
            raise

    async def stop(self) -> None:
        """Close the session from the task that owns it.

        The sentinel makes the owning task fall out of its ``async with``
        blocks itself. Cancelling from here instead would be the very bug this
        class exists to avoid.
        """
        task = self._task
        if task is None:
            return
        if not task.done():
            await self._commands.put(None)
        await self._reap_task()

    async def _reap_task(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout)
        except asyncio.TimeoutError:
            # The server never let go. Cancelling the task IS safe (anyio
            # unwinds the scopes inside that task); only __aexit__ from a
            # foreign task is not.
            logger.warning("MCP server '%s' did not close within %ss, cancelling", self.name, self.timeout)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown must not raise
                pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("MCP server '%s' task ended with: %s", self.name, e)

    # ------------------------------------------------------------------ worker

    async def _run(self) -> None:
        """Own the transport and the session for this connection's whole life."""
        try:
            async with self._open_streams() as (read_stream, write_stream):
                from mcp import ClientSession

                async with ClientSession(read_stream, write_stream) as session:
                    init = await session.initialize()
                    self._record_handshake(init)
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    await self._serve(session)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - the failure has to reach start()
            if self._ready is not None and not self._ready.done():
                error = MCPConnectionError(
                    f"Could not connect to MCP server '{self.name}': {_describe(e)}"
                )
                error.__cause__ = e  # keep the traceback reachable
                self._ready.set_exception(error)
            else:
                logger.warning("MCP server '%s' connection ended: %s", self.name, _describe(e))
            self._fail_pending(e)

    async def _serve(self, session: Any) -> None:
        """Run queued commands against *session* until told to stop.

        Each command gets its own task rather than being awaited in the loop.
        Only the session's ENTRY and EXIT have to happen in this task -- using
        it concurrently is fine, and ``ClientSession`` multiplexes requests over
        one connection by JSON-RPC id anyway.

        Awaiting them inline instead made the queue strictly serial: one slow
        tool call blocked every other call to the same server until it either
        finished or the caller's timeout fired -- and those callers then saw
        "server did not answer", pointing at a server that was perfectly
        healthy. A single agent triggers this, because a turn's tool calls all
        run in parallel.
        """
        in_flight: set[asyncio.Task] = set()
        try:
            while True:
                command = await self._commands.get()
                if command is None:
                    return
                if command.future.cancelled():
                    continue
                task = asyncio.create_task(self._run_command(session, command))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            # Let calls that are already on the wire finish before the session
            # closes under them; otherwise stopping would turn every in-flight
            # call into a spurious error.
            #
            # The window is deliberately shorter than stop()'s: the reaper
            # waits self.timeout for THIS task and then cancels it, so a drain
            # given the same budget never gets to clean up -- the cancel
            # arrived first and the loop below was dead code, leaving a task
            # pinned to a dead session for the life of the process.
            #
            # The inner finally matters for the same reason: even when the
            # cancel does win the race, the tasks still have to be cancelled.
            if in_flight:
                try:
                    await asyncio.wait(in_flight, timeout=max(0.1, self.timeout / 2))
                finally:
                    for task in in_flight:
                        task.cancel()

    @staticmethod
    async def _run_command(session: Any, command: _Command) -> None:
        try:
            result = await command.run(session)
            if not command.future.done():
                command.future.set_result(result)
        except asyncio.CancelledError:
            if not command.future.done():
                command.future.cancel()
            raise
        except BaseException as e:  # noqa: BLE001 - hand the error to the caller
            if not command.future.done():
                command.future.set_exception(e)

    def _fail_pending(self, error: BaseException) -> None:
        """Wake up everyone still waiting after the connection died."""
        while True:
            try:
                command = self._commands.get_nowait()
            except asyncio.QueueEmpty:
                return
            if command is not None and not command.future.done():
                command.future.set_exception(
                    MCPConnectionError(f"MCP server '{self.name}' disconnected: {error}")
                )

    async def _submit(self, run: Callable[[Any], Awaitable[Any]], *, timeout: Optional[float] = None) -> Any:
        if not self.connected:
            raise MCPConnectionError(f"MCP server '{self.name}' is not connected")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._commands.put(_Command(run=run, future=future))
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise MCPConnectionError(
                f"MCP server '{self.name}' did not answer within {timeout or self.timeout}s"
            ) from None

    # --------------------------------------------------------------- transport

    @asynccontextmanager
    async def _open_streams(self) -> AsyncIterator[Any]:
        """Open the SDK transport that matches the configured ``transport``."""
        transport = (getattr(self.config, "transport", None) or "streaming").lower()
        headers = self._auth_headers()

        # Name first, then the transport's own requirements. The other order
        # reports "no url configured" for a misspelled transport, which sends
        # whoever wrote the config looking in the wrong place.
        known = _STREAMABLE_HTTP_ALIASES | _SSE_ALIASES | _STDIO_ALIASES
        if transport not in known:
            raise MCPConnectionError(
                f"MCP server '{self.name}' has unknown transport '{transport}'. Known: {sorted(known)}"
            )

        if transport in _STDIO_ALIASES:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            command = getattr(self.config, "command", None)
            if not command:
                raise MCPConnectionError(
                    f"MCP server '{self.name}' uses transport 'stdio' but has no 'command' configured"
                )
            params = StdioServerParameters(
                command=command,
                args=list(getattr(self.config, "args", None) or []),
                env=dict(getattr(self.config, "env", None) or {}) or None,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                yield read_stream, write_stream
            return

        url = getattr(self.config, "url", None)
        if not url:
            raise MCPConnectionError(f"MCP server '{self.name}' has no url configured")

        if transport in _SSE_ALIASES:
            from mcp.client.sse import sse_client

            # Same factory as the streamable transport: without it ssl_verify
            # was quietly ignored here and TLS verification stayed on, however
            # the operator had configured it.
            async with sse_client(
                url,
                headers=headers or None,
                timeout=self.timeout,
                httpx_client_factory=self._httpx_factory(),
            ) as streams:
                yield streams[0], streams[1]
            return

        from mcp.client.streamable_http import streamablehttp_client

        # httpx_client_factory is how the SDK lets us keep the ssl_verify
        # switch the old client had; without it the setting would silently
        # stop working.
        async with streamablehttp_client(
            url,
            headers=headers or None,
            timeout=self.timeout,
            httpx_client_factory=self._httpx_factory(),
        ) as (read_stream, write_stream, _get_session_id):
            yield read_stream, write_stream

    def _httpx_factory(self):
        import httpx
        from mcp.shared._httpx_utils import create_mcp_http_client

        ssl_verify = self.ssl_verify

        def factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
            if ssl_verify is not False:
                return create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
            # Build instead of mutate: httpx resolves verify when it constructs
            # the transport, so assigning it afterwards does nothing. Building
            # it here rather than fixing up the SDK's client also avoids
            # creating one just to throw it away.
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout if timeout is not None else httpx.Timeout(self.timeout),
                auth=auth,
                verify=False,
                follow_redirects=True,
            )

        return factory

    def _auth_headers(self) -> Dict[str, str]:
        from .auth import build_auth_headers

        headers = dict(build_auth_headers(getattr(self.config, "auth", None)))
        if getattr(self.config, "initialization_options", None):
            # Deliberate and loud: the old client sent these as a non-standard
            # "initializationOptions" field on initialize. No such field exists
            # in the protocol, the SDK builds the params itself, and servers
            # ignored it. Saying so beats dropping it quietly.
            logger.warning(
                "MCP server '%s' configures initialization_options; the protocol has no such "
                "field and they are NOT sent. Put credentials in auth: or in the url.",
                self.name,
            )
        return headers

    def _record_handshake(self, init: Any) -> None:
        info = getattr(init, "serverInfo", None)
        self._server_info = {
            "name": getattr(info, "name", None),
            "version": getattr(info, "version", None),
        }
        self._protocol_version = getattr(init, "protocolVersion", None)
        caps = getattr(init, "capabilities", None)
        self._capabilities = caps.model_dump(exclude_none=True) if hasattr(caps, "model_dump") else {}
        logger.info(
            "MCP server '%s' connected (%s, protocol %s)",
            self.name, self._server_info.get("name") or "unknown", self._protocol_version,
        )

    # ------------------------------------------------------------------- calls

    async def list_tools(self) -> List[ToolDef]:
        """Fetch the server's tools as this project's ``ToolDef`` objects."""
        async def run(session: Any) -> List[ToolDef]:
            result = await session.list_tools()
            return [
                ToolDef(
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema or {},
                )
                for t in (result.tools or [])
            ]

        return await self._submit(run)

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call *name* and return its first text block, or the raw payload.

        The return shape is the one the agent core has always seen: the text of
        the first text content item, falling back to the structured result.
        Changing it here would ripple into every consumer of an external tool.
        """
        request_id = arguments.get("request_id") or arguments.get("requestId")
        await self._publish(
            f"Starting tool call: {name}", StatusPhase.START, request_id,
            {"tool": name, "server": self.name, "arguments": _preview_arguments(arguments)},
        )

        async def run(session: Any) -> Any:
            return await session.call_tool(name, arguments)

        try:
            result = await self._submit(run)
        except Exception as e:
            await self._publish(
                f"Tool call failed: {name}", StatusPhase.ERROR, request_id,
                {"tool": name, "server": self.name, "error": str(e)},
            )
            raise

        if getattr(result, "isError", False):
            message = _first_text(result) or "unknown error"
            await self._publish(
                f"Tool call failed: {name}", StatusPhase.ERROR, request_id,
                {"tool": name, "server": self.name, "error": message},
            )
            raise RuntimeError(f"Tool call failed: {message}")

        # Image blocks first: dumped into the text path they either vanish
        # (text wins) or land as base64 INSIDE the JSON payload -- measured
        # live with blender's get_viewport_screenshot, one screenshot pushed
        # the conversation past the model's input limit and bricked the
        # session. Persist them and answer with the house contract
        # (_multimodal_content, path-based) that tool_execution already
        # turns into a real image part for the model.
        media = _persist_media_blocks(result, self.name, name)
        if media:
            texts = [getattr(item, "text", "") or ""
                     for item in getattr(result, "content", None) or []
                     if getattr(item, "type", None) == "text"]
            payload = {
                "status": "success",
                "message": "\n".join(t for t in texts if t) or (
                    f"{len(media)} media item(s) returned by {name}"),
                "_multimodal_content": media,
            }
            await self._publish(
                f"Tool call completed: {name}", StatusPhase.END, request_id,
                {"tool": name, "server": self.name,
                 "result_type": f"{len(media)} media item(s)"},
            )
            return payload

        text = _first_text(result)
        if text is not None:
            await self._publish(
                f"Tool call completed: {name}", StatusPhase.END, request_id,
                {
                    "tool": name, "server": self.name, "result_length": len(text),
                    "result_preview": text[:100] + "..." if len(text) > 100 else text,
                },
            )
            return text

        payload = _structured(result)
        await self._publish(
            f"Tool call completed: {name}", StatusPhase.END, request_id,
            {"tool": name, "server": self.name, "result_type": type(payload).__name__},
        )
        return payload

    async def _publish(self, message: str, phase: Any, request_id: Any, meta: Dict[str, Any]) -> None:
        try:
            await publish_status(self.name, message, request_id=request_id, phase=phase, meta=meta)
        except Exception:
            logger.debug("publish_status failed for %s on '%s'", message, self.name)


def _describe(error: BaseException) -> str:
    """Readable one-liner for an error, unwrapping anyio's exception groups.

    The SDK's transports run in task groups, so a refused connection, a 401 and
    a DNS failure all surface as a ``BaseExceptionGroup`` whose ``str()`` is the
    useless "unhandled errors in a TaskGroup (1 sub-exception)". The cause the
    operator needs sits inside, so dig it out -- nested groups included.
    """
    seen: List[str] = []
    stack: List[BaseException] = [error]
    while stack:
        current = stack.pop()
        inner = getattr(current, "exceptions", None)
        if inner:
            stack.extend(inner)
            continue
        text = str(current).strip()
        label = f"{type(current).__name__}: {text}" if text else type(current).__name__
        if label not in seen:
            seen.append(label)
    return "; ".join(seen) if seen else f"{type(error).__name__}: {error}"


#: Where image blocks from external tool results are written. Relative to the
#: repo root that API and CLI start from (the same assumption the stdio
#: server paths in mcp_servers.yaml make) and inside media_ops' sandbox
#: root (data/), so the model can re-load or save them. Tests point this
#: at a tmp_path.
_MEDIA_DIR = Path("data/media/external_mcp")

_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
             "image/gif": ".gif", "audio/wav": ".wav", "audio/mpeg": ".mp3",
             "audio/ogg": ".ogg", "audio/flac": ".flac"}

#: MCP content block types that carry inline base64 media. Video has NO
#: standard MCP content block -- a server shipping video does it as a blob
#: resource, which this path does not unpack (named gap, not an oversight).
_MEDIA_BLOCK_TYPES = ("image", "audio")


def _persist_media_blocks(result: Any, server: str, tool: str) -> List[Dict[str, str]]:
    """Write every image/audio content block to disk, as _multimodal_content items.

    Returns an empty list when there is nothing to persist -- including on a
    write failure: a broken disk must degrade to the old text behaviour, not
    take the tool call down.
    """
    items: List[Dict[str, str]] = []
    for index, block in enumerate(getattr(result, "content", None) or []):
        btype = getattr(block, "type", None)
        if btype not in _MEDIA_BLOCK_TYPES:
            continue
        data = getattr(block, "data", None)
        mime = getattr(block, "mimeType", None) or (
            "image/png" if btype == "image" else "application/octet-stream")
        if not data:
            continue
        try:
            raw = base64.b64decode(data)
            target_dir = _MEDIA_DIR / server
            target_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{tool}-{int(time.time() * 1000)}-{index}{_MIME_EXT.get(mime, '.bin')}"
            target = target_dir / filename
            target.write_bytes(raw)
        except Exception:
            logger.warning("Could not persist image block %d of %s.%s",
                           index, server, tool, exc_info=True)
            continue
        items.append({
            "type": btype,
            "path": str(target),
            "mime_type": mime,
            "description": f"{btype.capitalize()} returned by external tool {server}.{tool}",
        })
    return items


def _first_text(result: Any) -> Optional[str]:
    """Return the first text block of a CallToolResult, if there is one."""
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", None) == "text":
            return getattr(item, "text", "") or ""
    return None


def _structured(result: Any) -> Any:
    """Fall back to whatever the server sent when there is no text block."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    if hasattr(result, "model_dump"):
        # mode="json": a resource block carries its URL as a pydantic AnyUrl,
        # which the tool message's json.dumps cannot serialize.
        return result.model_dump(mode="json", exclude_none=True)
    return result


def _preview_arguments(arguments: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not arguments:
        return {}
    out = {}
    for key, value in arguments.items():
        text = str(value)
        out[key] = text[:50] + "..." if len(text) > 50 else text
    return out
