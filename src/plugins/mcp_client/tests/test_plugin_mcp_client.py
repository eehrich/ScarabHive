"""Tests for the external MCP client plugin.

These drive a REAL MCP server (``probe_server.py``, started over stdio) rather
than a mock. That is deliberate: the implementation this replaces had a green
unit-test suite while being unable to talk to any current server at all -- it
never sent ``notifications/initialized`` and announced protocol 2024-11-05.
Mocks would have kept saying yes.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from plugins.mcp_client.connection import MCPConnectionError, ServerConnection
from plugins.mcp_client.manager import ExternalServerPool

PROBE = str(Path(__file__).parent / "probe_server.py")


def make_config(**overrides):
    """A RemoteMCPConfig-shaped object (the code reads attributes, not keys)."""
    config = types.SimpleNamespace(
        url=None, transport="stdio", enabled=True, auth=None,
        initialization_options=None, tools=None, description=None,
        command=sys.executable, args=[PROBE], env=None,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def tool_config(blocked=None, allowed=None):
    return types.SimpleNamespace(blocked=blocked or [], allowed=allowed or [])


@pytest.fixture
async def connection():
    conn = ServerConnection("probe", make_config(), timeout=30.0)
    await conn.start()
    try:
        yield conn
    finally:
        await conn.stop()


class TestImageResults:

    """Image content blocks must become the house multimodal contract.



    Before 2026-09-02 an image block either vanished (a text block won) or

    its base64 landed inside the JSON payload -- one blender screenshot

    pushed a live session past the model input limit.

    """



    @pytest.fixture(autouse=True)

    def _media_dir(self, tmp_path, monkeypatch):

        from plugins.mcp_client import connection as conn_mod

        monkeypatch.setattr(conn_mod, "_MEDIA_DIR", tmp_path)

        self.media_dir = tmp_path



    async def test_image_result_is_persisted_not_inlined(self, connection):

        result = await connection.call_tool("picture", {})

        assert isinstance(result, dict)

        items = result["_multimodal_content"]

        assert len(items) == 1 and items[0]["type"] == "image"

        assert items[0]["mime_type"] == "image/png"

        saved = Path(items[0]["path"])

        assert saved.exists() and saved.read_bytes().startswith(b"\x89PNG")

        # The whole point: no base64 payload in what the model reads as text.

        assert "iVBOR" not in str(result)



    async def test_text_next_to_an_image_survives(self, connection):

        result = await connection.call_tool("captioned_picture", {})

        assert result["message"] == "a red pixel"

        assert len(result["_multimodal_content"]) == 1



    async def test_audio_blocks_take_the_same_path(self):
        """MCP AudioContent carries the same data/mimeType pair as images.
        Unit-level against the helper: the wire path is pinned by the image
        tests, and the probe server has no audio tool to speak of."""
        import base64
        from plugins.mcp_client import connection as conn_mod
        block = types.SimpleNamespace(
            type="audio",
            data=base64.b64encode(b"RIFFxxxxWAVE").decode(),
            mimeType="audio/wav")
        result = types.SimpleNamespace(content=[block])
        items = conn_mod._persist_media_blocks(result, "probe", "speak")
        assert len(items) == 1 and items[0]["type"] == "audio"
        saved = Path(items[0]["path"])
        assert saved.suffix == ".wav" and saved.read_bytes() == b"RIFFxxxxWAVE"

    async def test_text_only_results_keep_the_old_shape(self, connection):

        assert await connection.call_tool("echo", {"text": "hi"}) == "hi"





class TestHandshake:
    @pytest.mark.asyncio
    async def test_start_completes_the_mcp_handshake(self, connection):
        """A started connection has really shaken hands, not just opened a pipe."""
        assert connection.connected
        assert connection.server_info["name"] == "probe"
        # The point of the migration: a current protocol version, negotiated
        # by the SDK instead of the hard-coded 2024-11-05 of the old client.
        assert connection.protocol_version
        assert connection.protocol_version >= "2025-03-26"

    @pytest.mark.asyncio
    async def test_unreachable_server_fails_at_start_not_later(self):
        """A dead server must raise here, not show up as an empty tool list."""
        conn = ServerConnection(
            "dead", make_config(transport="streaming", url="http://127.0.0.1:9/mcp"), timeout=5.0,
        )
        with pytest.raises(MCPConnectionError):
            await conn.start()
        assert not conn.connected  # and it cleaned up after itself

    @pytest.mark.asyncio
    async def test_connect_errors_name_the_actual_cause(self):
        """An operator must learn WHY, not that a task group was unhappy.

        The SDK runs its transports in anyio task groups, so a refused
        connection, a bad host and a 401 all arrive as a BaseExceptionGroup
        whose str() is "unhandled errors in a TaskGroup (1 sub-exception)".
        Formatting that verbatim threw away the only useful part.
        """
        conn = ServerConnection(
            "dead", make_config(transport="streaming", url="http://127.0.0.1:9/mcp"), timeout=5.0,
        )
        with pytest.raises(MCPConnectionError) as excinfo:
            await conn.start()

        message = str(excinfo.value)
        assert "TaskGroup" not in message, f"error still generic: {message}"
        assert "ConnectError" in message or "connection" in message.lower()
        # The original stays reachable for anyone who wants the traceback.
        assert excinfo.value.__cause__ is not None

    @pytest.mark.asyncio
    async def test_closing_does_not_leave_a_command_task_behind(self):
        """Stopping while a long call is in flight must not orphan its task.

        The drain in _serve used to run with the same budget as stop()'s
        reaper, so the reaper always cancelled the serving task first and the
        drain's cleanup never ran -- leaving a task pinned to a dead session
        for the life of the process.
        """
        import gc

        conn = ServerConnection("probe", make_config(), timeout=3.0)
        await conn.start()
        call = asyncio.create_task(conn.call_tool("sleep", {"seconds": 20}))
        await asyncio.sleep(0.5)  # make sure it is really on the wire

        await conn.stop()
        try:
            await asyncio.wait_for(call, timeout=2)
        except BaseException:  # noqa: BLE001 - the call is expected to fail
            pass
        await asyncio.sleep(0.3)
        gc.collect()

        current = asyncio.current_task()
        leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        assert not leftover, f"tasks left running after stop(): {leftover}"

    @pytest.mark.asyncio
    async def test_ssl_verify_reaches_every_http_transport(self):
        """ssl_verify=False must reach httpx on the SSE path too.

        The streamable transport got an httpx factory, the SSE one did not, so
        verification silently stayed on there -- the setting looked applied and
        was not.
        """
        import httpx

        for transport, url in (("streaming", "http://127.0.0.1:9/mcp"),
                               ("sse", "http://127.0.0.1:9/sse")):
            seen = []
            original = httpx.AsyncClient.__init__

            def spy(self, *args, **kwargs):
                seen.append(kwargs.get("verify", "unset"))
                return original(self, *args, **kwargs)

            httpx.AsyncClient.__init__ = spy
            try:
                conn = ServerConnection(
                    transport, make_config(transport=transport, url=url), timeout=3.0,
                )
                conn.ssl_verify = False
                try:
                    async with conn._open_streams():
                        pass
                except BaseException:  # noqa: BLE001 - connecting is not the point
                    pass
            finally:
                httpx.AsyncClient.__init__ = original

            assert False in seen, f"{transport}: ssl_verify never reached httpx ({seen})"

    @pytest.mark.asyncio
    async def test_unknown_transport_is_rejected_by_name(self):
        conn = ServerConnection("weird", make_config(transport="carrier-pigeon"), timeout=5.0)
        with pytest.raises(MCPConnectionError, match="carrier-pigeon"):
            await conn.start()

    @pytest.mark.asyncio
    async def test_stdio_without_command_is_rejected(self):
        conn = ServerConnection("nocmd", make_config(command=None), timeout=5.0)
        with pytest.raises(MCPConnectionError, match="command"):
            await conn.start()


class TestCalls:
    @pytest.mark.asyncio
    async def test_list_tools_returns_internal_mcp_tool_objects(self, connection):
        tools = await connection.list_tools()
        names = {t.name for t in tools}
        assert {"add", "echo", "boom"} <= names
        add = next(t for t in tools if t.name == "add")
        assert add.description
        assert add.input_schema["type"] == "object"

    @pytest.mark.asyncio
    async def test_call_tool_returns_the_first_text_block(self, connection):
        """The return shape the agent core has always seen: plain text."""
        result = await connection.call_tool("add", {"a": 17, "b": 25})
        assert result == "42"
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_failing_tool_raises_instead_of_returning_the_error(self, connection):
        with pytest.raises(RuntimeError):
            await connection.call_tool("boom", {})

    @pytest.mark.asyncio
    async def test_concurrent_calls_over_one_connection(self, connection):
        """Several agents share one connection; the queue must keep them apart."""
        results = await asyncio.gather(
            *[connection.call_tool("add", {"a": i, "b": i}) for i in range(6)]
        )
        assert results == [str(i * 2) for i in range(6)]

    @pytest.mark.asyncio
    async def test_a_slow_call_does_not_block_the_others(self):
        """One slow tool must not drag every other call down with it.

        The commands used to be awaited one after another inside the serving
        task, so a slow call blocked the queue and everything behind it failed
        with "server did not answer" -- naming a server that was perfectly
        healthy. A single agent hits this, because a turn's tool calls all run
        in parallel.
        """
        conn = ServerConnection("probe", make_config(), timeout=6.0)
        await conn.start()
        try:
            slow = asyncio.create_task(conn.call_tool("sleep", {"seconds": 3.0}))
            await asyncio.sleep(0.3)  # make sure the slow one is on the wire
            quick = await asyncio.wait_for(
                conn.call_tool("add", {"a": 1, "b": 1}), timeout=2.0
            )
            assert quick == "2"
            assert await slow == "slept"
        finally:
            await conn.stop()

    @pytest.mark.asyncio
    async def test_first_text_block_wins(self):
        """The contract is the FIRST text block, not just "some" text.

        Consumers read this as the tool's answer. A server that prepends a
        note and appends the payload would otherwise change meaning depending
        on which block happened to be picked.
        """
        from types import SimpleNamespace

        from plugins.mcp_client.connection import _first_text

        result = SimpleNamespace(content=[
            SimpleNamespace(type="image", data="..."),
            SimpleNamespace(type="text", text="first"),
            SimpleNamespace(type="text", text="second"),
        ])
        assert _first_text(result) == "first"
        assert _first_text(SimpleNamespace(content=[])) is None
        assert _first_text(SimpleNamespace(content=None)) is None

    @pytest.mark.asyncio
    async def test_a_result_without_text_survives_json_dumps(self):
        """A resource block is not text, so the result goes out as the server's
        structure. A plain pydantic dump kept its URL as an AnyUrl object, the
        tool message's json.dumps raised, and the model got "invocation failed"
        instead of the result."""
        conn = ServerConnection("probe", make_config(), timeout=6.0)
        await conn.start()
        try:
            payload = await conn.call_tool("resource_only", {})
        finally:
            await conn.stop()

        assert "file:///probe/notes.txt" in json.dumps(payload)


class TestPerServerTimeout:
    async def test_a_server_specific_timeout_overrides_the_pool_default(self):
        """A Blender render needs minutes; the global 5s default killed it.
        The per-server value must reach the connection -- pydantic silently
        dropping unknown fields is exactly how this breaks unnoticed."""
        pool = ExternalServerPool(timeout=30.0)
        pool.configure({"probe": make_config(timeout=0.2)})
        connection = await pool.connect("probe")
        try:
            assert connection.timeout == 0.2
            with pytest.raises(MCPConnectionError, match="did not answer within 0.2s"):
                await connection.call_tool("sleep", {"seconds": 5})
        finally:
            await pool.close_all()

    async def test_without_a_server_timeout_the_pool_default_holds(self):
        pool = ExternalServerPool(timeout=12.0)
        pool.configure({"probe": make_config()})
        connection = await pool.connect("probe")
        try:
            assert connection.timeout == 12.0
        finally:
            await pool.close_all()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_from_a_foreign_task(self):
        """The reason this class exists.

        The SDK's session is an anyio cancel scope, and anyio refuses to let a
        scope be exited by a task other than the one that entered it. Closing a
        session straight from a request handler therefore raised
        "Attempted to exit cancel scope in a different task...". Here the owning
        task closes itself on a sentinel, so stopping from anywhere works.
        """
        conn = ServerConnection("probe", make_config(), timeout=30.0)
        await conn.start()
        await asyncio.create_task(conn.stop())  # different task on purpose
        assert not conn.connected

    @pytest.mark.asyncio
    async def test_calls_after_stop_fail_clearly(self):
        conn = ServerConnection("probe", make_config(), timeout=30.0)
        await conn.start()
        await conn.stop()
        with pytest.raises(MCPConnectionError, match="not connected"):
            await conn.call_tool("add", {"a": 1, "b": 1})

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self):
        conn = ServerConnection("probe", make_config(), timeout=30.0)
        await conn.start()
        await conn.stop()
        await conn.stop()  # must not raise
        assert not conn.connected


class TestPool:
    @pytest.mark.asyncio
    async def test_configure_keeps_only_enabled_servers(self):
        pool = ExternalServerPool()
        pool.configure({
            "on": make_config(enabled=True),
            "off": make_config(enabled=False),
        })
        assert set(pool.configured_servers) == {"on"}

    @pytest.mark.asyncio
    async def test_connect_list_and_call(self):
        pool = ExternalServerPool(timeout=30.0)
        pool.configure({"probe": make_config()})
        await pool.connect("probe")
        try:
            assert pool.list_connected() == ["probe"]
            by_server = await pool.list_tools_by_server()
            assert {t["name"] for t in by_server["probe"]} >= {"add", "echo"}
            assert await pool.call_tool("probe", "add", {"a": 2, "b": 3}) == "5"
        finally:
            await pool.close_all()
        assert pool.list_connected() == []

    @pytest.mark.asyncio
    async def test_blocked_tools_are_marked_not_removed(self):
        """The permission layer above needs to SEE a blocked tool.

        Dropping it here would look identical to the server not offering it,
        and the UI could no longer show what is blocked.
        """
        pool = ExternalServerPool(timeout=30.0)
        pool.configure({"probe": make_config(tools=tool_config(blocked=["echo"]))})
        await pool.connect("probe")
        try:
            tools = (await pool.list_tools_by_server())["probe"]
            by_name = {t["name"]: t for t in tools}
            assert "echo" in by_name, "blocked tool disappeared from the listing"
            assert by_name["echo"]["blocked"] is True
            assert by_name["add"]["blocked"] is False
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_blocked_tool_cannot_be_called(self):
        pool = ExternalServerPool(timeout=30.0)
        pool.configure({"probe": make_config(tools=tool_config(blocked=["echo"]))})
        await pool.connect("probe")
        try:
            with pytest.raises(PermissionError):
                await pool.call_tool("probe", "echo", {"text": "hi"})
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_a_real_config_object_can_express_stdio(self):
        """The documented stdio setup must survive the config parser.

        These tests build their configs as SimpleNamespace, which cannot show
        that RemoteMCPConfig used to drop command/args/env (pydantic ignores
        extras) -- so the documented transport was unreachable from any real
        config, however correctly it was written.
        """
        from agent_system.config.models import ExternalServersConfig

        parsed = ExternalServersConfig.model_validate({
            "remote_servers": {
                "local": {
                    "enabled": True, "transport": "stdio",
                    "command": sys.executable, "args": [PROBE],
                }
            }
        })
        config = parsed.remote_servers["local"]
        assert config.command == sys.executable
        assert config.args == [PROBE]

        pool = ExternalServerPool(timeout=30.0)
        pool.configure(parsed.remote_servers)
        await pool.connect("local")
        try:
            assert await pool.call_tool("local", "add", {"a": 1, "b": 2}) == "3"
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_a_partial_tool_listing_is_not_cached(self):
        """A server that times out must not be frozen out of the catalogue.

        The listing skipped a failing server and cached the result, so one slow
        answer removed a healthy server from every agent's tool list for a
        whole TTL -- an hour with the shipped config.
        """
        pool = ExternalServerPool(timeout=30.0, cache_ttl=3600.0)
        pool.configure({"probe": make_config()})
        await pool.connect("probe")
        try:
            good = await pool.list_tools_by_server()
            assert good["probe"]

            connection = pool.get("probe")
            original = connection.list_tools

            async def failing():
                raise RuntimeError("timed out")

            connection.list_tools = failing
            pool.invalidate_cache()
            partial = await pool.list_tools_by_server()
            # The previous knowledge is kept ...
            assert partial["probe"] == good["probe"]

            # ... and the gap is NOT cached: once the server answers again the
            # very next call sees it, rather than waiting out the TTL.
            connection.list_tools = original
            assert (await pool.list_tools_by_server())["probe"] == good["probe"]
            assert pool._tools_cache is not None
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_unknown_or_disabled_server_is_refused(self):
        pool = ExternalServerPool(timeout=5.0)
        pool.configure({"off": make_config(enabled=False)})
        with pytest.raises(MCPConnectionError):
            await pool.connect("off")
        with pytest.raises(MCPConnectionError):
            await pool.connect("never-heard-of-it")

    @pytest.mark.asyncio
    async def test_one_dead_server_does_not_stop_the_others(self):
        """Startup must survive an unreachable server."""
        pool = ExternalServerPool(timeout=5.0)
        pool.configure({
            "probe": make_config(),
            "dead": make_config(transport="streaming", url="http://127.0.0.1:9/mcp"),
        })
        try:
            results = await pool.connect_all()
            assert results["probe"] is None
            assert results["dead"]  # an error string, not a raise
            assert pool.list_connected() == ["probe"]
        finally:
            await pool.close_all()

    @pytest.mark.asyncio
    async def test_tool_list_is_cached_until_invalidated(self):
        pool = ExternalServerPool(timeout=30.0, cache_ttl=300.0)
        pool.configure({"probe": make_config()})
        await pool.connect("probe")
        try:
            first = await pool.list_tools_by_server()
            assert await pool.list_tools_by_server() is first  # served from cache
            pool.invalidate_cache()
            assert await pool.list_tools_by_server() is not first
        finally:
            await pool.close_all()


class TestPluginRole:
    """The plugin's second role: supplying external tools to the agent core."""

    @pytest.mark.asyncio
    async def test_start_registers_the_capability_even_with_no_servers(self):
        """An outage must not look like "nothing is configured".

        If registration depended on a successful connection, the core would
        find no provider and silently behave as if no external servers existed.
        """
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig
        from agent_system.plugins import capabilities
        from plugins.mcp_client.server import MCPClientServer

        capabilities.reset()
        try:
            plugin = MCPClientServer("mcp_client", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
            assert plugin.pool.configured_servers == {}
            await plugin.start_plugin()
            assert capabilities.get_provider(capabilities.EXTERNAL_TOOLS) is plugin
            await plugin.stop_plugin()
            assert capabilities.get_provider(capabilities.EXTERNAL_TOOLS) is None
        finally:
            capabilities.reset()

    @pytest.mark.asyncio
    async def test_management_tools_report_state(self):
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig
        from agent_system.plugins import capabilities
        from plugins.mcp_client.server import MCPClientServer

        capabilities.reset()
        plugin = MCPClientServer("mcp_client", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
        plugin.pool.configure({"probe": make_config()})
        try:
            listed = await plugin.list_servers({})
            assert listed["total"] == 1
            assert listed["connected"] == []

            connected = await plugin.connect({"server": "probe"})
            assert connected["success"] is True
            assert connected["protocol_version"]

            tools = await plugin.tools({})
            assert tools["total"] >= 3

            gone = await plugin.disconnect({"server": "probe"})
            assert gone["success"] is True
        finally:
            await plugin.stop_plugin()
            capabilities.reset()

    @pytest.mark.asyncio
    async def test_connect_reports_failure_instead_of_raising(self):
        from agent_system.config.models import AgentSystemConfig, ToolServerConfig
        from plugins.mcp_client.server import MCPClientServer

        plugin = MCPClientServer("mcp_client", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
        result = await plugin.connect({"server": "nope"})
        assert result["success"] is False and result["error"]
        assert (await plugin.connect({}))["success"] is False
