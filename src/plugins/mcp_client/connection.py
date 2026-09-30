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
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

from agent_system.paths import data_path
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

#: Seconds a new connection gets for its handshake at least, whatever the
#: server's answer timeout. Tests lower it.
_HANDSHAKE_FLOOR = 60.0


class MCPConnectionError(RuntimeError):
    """Raised when a server cannot be reached or refuses the handshake."""


class MCPServerGone(MCPConnectionError):
    """The transport closed under a call: a stdio server's process ended (a crash), or the remote
    hung up. ``unsent``: the request never left, so repeating it on a new connection is safe."""

    def __init__(self, message: str, *, unsent: bool):
        super().__init__(message)
        self.unsent = unsent


def _transport_closed(error: BaseException) -> Optional[bool]:
    """None if *error* is not a closed transport; else whether the request stayed unsent.

    anyio's stream errors carry no text at all -- the model saw "Tool invocation failed: " and
    nothing else, call after call, while the connection still counted as connected (measured
    2026-09-30: a stdio server crashed mid-render, every later call failed the same way)."""
    import anyio

    if isinstance(error, (anyio.ClosedResourceError, anyio.BrokenResourceError)):
        return True                                      # the write stream was already gone
    if isinstance(error, anyio.EndOfStream):
        return False
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED

    if isinstance(error, McpError) and getattr(error.error, "code", None) == CONNECTION_CLOSED:
        return False                                     # sent, and the answer never came
    return None


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
        self._closing = False           # the stop sentinel is queued: a new call would wait behind it
        self._stopping = False          # ... by stop(): this client closes, the server did nothing wrong
        self._ready: Optional[asyncio.Future] = None
        self._server_info: Dict[str, Any] = {}
        self._capabilities: Dict[str, Any] = {}
        self._protocol_version: Optional[str] = None

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done() and not self._closing

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
        if self._task is not None or self._closing:
            # One connection, one worker: a restart on the same object raced the old worker's
            # ending in every variant tried. ExternalServerPool.connect builds a new one.
            raise MCPConnectionError(f"MCP server '{self.name}': this connection was used already; "
                                     f"a new one starts the server again")

        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = loop.create_task(self._run(), name=f"mcp-client:{self.name}")

        try:
            # Bounded: a server that never answers initialize (a stdio
            # process that reads and stays silent) held start() -- and the
            # pool lock, and with it every other connect and close_all --
            # forever. The HTTP transports were bounded by httpx; stdio not.
            # The floor: a stdio program has to start first (npx may download
            # its package), and self.timeout is sized for answers, not that.
            limit = max(self.timeout, _HANDSHAKE_FLOOR)
            await asyncio.wait_for(self._ready, timeout=limit)
        except asyncio.TimeoutError:
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                await asyncio.wait({task})
            raise MCPConnectionError(
                f"MCP server '{self.name}' did not complete the handshake within {limit}s"
            ) from None
        except asyncio.CancelledError:
            # Not an Exception: without this branch a start cancelled
            # mid-handshake left the task -- and a stdio child -- running.
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                await asyncio.wait({task})
            raise
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
            self._closing = self._stopping = True
            await self._commands.put(None)
        await self._reap_task()

    async def _reap_task(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        # The drain in _serve takes up to timeout/2 and the SDK's stdio shutdown up to 2 s more (on
        # POSIX 2 s after closing stdin and 2 s after SIGTERM): a cancel inside that skips its next
        # step, and the task then waits for the child to exit by itself -- a minute, or for ever.
        limit = max(self.timeout, self.timeout / 2 + 5)
        # asyncio.wait never raises the task's outcome: a cancel from elsewhere (a loop torn down)
        # raised CancelledError into stop(), and close_all() then left the other servers running.
        await asyncio.wait({task}, timeout=limit)
        if not task.done():
            # The server never let go. Cancelling the task IS safe (anyio
            # unwinds the scopes inside that task); only __aexit__ from a
            # foreign task is not.
            logger.warning("MCP server '%s' did not close within %ss, cancelling", self.name, limit)
            task.cancel()
            await asyncio.wait({task})

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
            # A stop() during the handshake cancels the worker: start() hears it now, not when
            # its handshake deadline runs out.
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(MCPConnectionError(
                    f"MCP server '{self.name}' was stopped during the handshake"))
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
        finally:
            self._closing = True
            self._fail_pending(RuntimeError("the connection closed"))    # calls queued behind the sentinel

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
                # A caller that gave up (timeout, cancel) takes the call with
                # it; otherwise every hung call stayed pinned to the session
                # until stop().
                command.future.add_done_callback(
                    lambda future, task=task: task.cancel() if future.cancelled() else None)
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

    async def _run_command(self, session: Any, command: _Command) -> None:
        try:
            result = await command.run(session)
            if not command.future.done():
                command.future.set_result(result)
        except asyncio.CancelledError:
            # The caller gave up (its future is cancelled already), or the session closes under
            # the call: a crash in several calls at once ends up here, not in the branch below.
            if not command.future.done():
                command.future.set_exception(MCPConnectionError(
                    f"The connection to MCP server '{self.name}' was closed by this client (stop, disconnect) "
                    f"while the call ran; whether it took effect is unknown.") if self._stopping else MCPServerGone(
                    f"MCP server '{self.name}' closed the connection while this call ran; whether it "
                    f"took effect is unknown.", unsent=False))
            raise
        except BaseException as e:  # noqa: BLE001 - hand the error to the caller
            unsent = _transport_closed(e)
            if unsent is not None:
                # The session is dead: end the worker -- `connected` turns false at once, so the pool
                # starts the server again instead of queueing behind the sentinel -- and say what
                # happened instead of an empty text.
                self._closing = True
                self._commands.put_nowait(None)
                gone = MCPServerGone(
                    f"MCP server '{self.name}' closed the connection -- its process ended or the remote "
                    f"hung up ({_describe(e) or type(e).__name__}). The next call starts it again.",
                    unsent=unsent)
                gone.__cause__ = e
                e = gone
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
        """Call *name* and return its text blocks, or the raw payload.

        The return shape is the one the agent core has always seen: text,
        falling back to the structured result. Changing the shape here would
        ripple into every consumer of an external tool.
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
            message = _text(result) or "unknown error"
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

        text = _text(result)
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


#: Where image blocks from external tool results are written. None:
#: ``media/external_mcp`` in the data directory, looked up when used -- inside
#: media_ops' sandbox root (the data directory), so the model can re-load or
#: save them. Tests point this at a tmp_path.
_MEDIA_DIR: Path | None = None

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
            target_dir = (_MEDIA_DIR or data_path("media", "external_mcp")) / server
            target_dir.mkdir(parents=True, exist_ok=True)
            # The tool name is the foreign server's: "../../x" wrote outside
            # the media directory.
            safe_tool = re.sub(r"[^A-Za-z0-9_.-]", "_", tool)
            # The random part: 'a/b' and 'a_b', or one tool twice in the same
            # millisecond, wrote to one path and the later file replaced the first.
            filename = (f"{safe_tool}-{int(time.time() * 1000)}-{index}-{uuid.uuid4().hex[:8]}"
                        f"{_MIME_EXT.get(mime, '.bin')}")
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


def _text(result: Any) -> Optional[str]:
    """Every text block of a CallToolResult, in order, a blank line apart.

    None when there is no text block. Only the first used to be kept -- a
    rule carried over from the hand-written client, and every further
    block was lost to the model.
    """
    texts = [getattr(item, "text", "") or ""
             for item in getattr(result, "content", None) or []
             if getattr(item, "type", None) == "text"]
    return "\n\n".join(texts) if texts else None


def _structured(result: Any) -> Any:
    """Fall back to whatever the server sent when there is no text block."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # _multimodal_content is OUR key: tool_execution reads the files it
        # names and sends them to the model provider. A server that sets it
        # itself would have us upload any local file -- only
        # _persist_media_blocks may produce it. Renamed, not dropped: what
        # the server sent stays readable as data.
        if isinstance(structured, dict) and "_multimodal_content" in structured:
            structured = dict(structured)
            structured["server_multimodal_content"] = structured.pop("_multimodal_content")
            logger.warning("MCP server result carried the reserved key _multimodal_content; renamed")
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
