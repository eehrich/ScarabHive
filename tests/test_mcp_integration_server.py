import socket
from contextlib import closing

from aiohttp import web
import aiohttp
import pytest

from agent_system.mcp.client import MCPClientFactory


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
async def test_mcp_client_against_local_server():
    """Start a tiny MCP-compatible HTTP server and ensure client can initialize and list tools."""

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    async def handle_mcp(request: web.Request):
        payload = await request.json()
        method = payload.get("method")
        req_id = payload.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "serverInfo": {"name": "local-mcp-test"},
                    "capabilities": {"tools": True, "resources": False, "prompts": False}
                }
            }
            return web.json_response(resp)

        if method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": [{"name": "echo", "description": "Echo tool", "inputSchema": {}}]}
            }
            return web.json_response(resp)

        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})

    app = web.Application()
    app.router.add_post('/mcp', handle_mcp)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()

    try:
        # Create client and ensure list_tools returns expected tool
        client = await MCPClientFactory.create_http_client(base_url=base_url, client_name="test-client", timeout=5.0, ssl_verify=True)
        tools = await client.list_tools()
        # tools may be dataclass-like objects or dicts depending on implementation
        found = False
        for t in tools:
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if name == "echo":
                found = True
                break
        assert found, f"expected tool 'echo' in tools, got: {tools!r}"
        await client.disconnect()
    finally:
        await runner.cleanup()


@pytest.mark.integration
async def test_mcp_tools_call_returns_text_content():
    """Mock server returns a content array with a text item; client should return the text."""

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    async def handle_mcp(request: web.Request):
        payload = await request.json()
        method = payload.get("method")
        req_id = payload.get("id")

        if method == "initialize":
            return web.json_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"serverInfo": {"name": "local-mcp-test"}, "capabilities": {"tools": True}}
            })

        if method == "tools/list":
            return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{"name": "say", "description": "Say text", "inputSchema": {}}]}})

        if method == "tools/call":
            params = payload.get("params", {})
            name = params.get("name") or params.get("tool")
            if name == "say":
                text = (params.get("arguments") or params.get("input") or {}).get("text", "")
                # Return content array with a text item (client should extract and return the text)
                return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}})

            return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params"}})

        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})

    app = web.Application()
    app.router.add_post('/mcp', handle_mcp)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()

    try:
        client = await MCPClientFactory.create_http_client(base_url=base_url, client_name="test-client", timeout=5.0, ssl_verify=True)
        res = await client.call_tool('say', {'text': 'hello world'})
        # Since server returned content text, client should return that text
        assert res == 'hello world'
        await client.disconnect()
    finally:
        await runner.cleanup()


@pytest.mark.integration
async def test_mcp_tools_call_multiple_content_items():
    """Ensure client returns the first text item even if earlier items are non-text."""

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    async def handle_mcp(request: web.Request):
        payload = await request.json()
        method = payload.get("method")
        req_id = payload.get("id")

        if method == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"serverInfo": {"name": "local-mcp-test"}, "capabilities": {"tools": True}}})

        if method == "tools/list":
            return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{"name": "multi", "description": "Multi content", "inputSchema": {}}]}})

        if method == "tools/call":
            params = payload.get("params", {})
            name = params.get("name") or params.get("tool")
            if name == "multi":
                # first item is non-text (e.g., binary or structured), second is text
                return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "data", "data": "..."}, {"type": "text", "text": "first text"}, {"type": "text", "text": "second text"}]}})

            return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params"}})

        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})

    app = web.Application()
    app.router.add_post('/mcp', handle_mcp)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()

    try:
        client = await MCPClientFactory.create_http_client(base_url=base_url, client_name="test-client", timeout=5.0, ssl_verify=True)
        res = await client.call_tool('multi', {})
        # client should return the first text content which is 'first text'
        assert res == 'first text'
        await client.disconnect()
    finally:
        await runner.cleanup()


@pytest.mark.integration
async def test_mcp_tools_call_and_auth():
    """Start a mock MCP server that supports tools/call and checks Authorization header."""

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    received_headers = []

    async def handle_mcp(request: web.Request):
        payload = await request.json()
        method = payload.get("method")
        req_id = payload.get("id")

        # Capture all headers for debugging/inspection
        received_headers.append(dict(request.headers))

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "serverInfo": {"name": "local-mcp-test"},
                    "capabilities": {"tools": True}
                }
            }
            return web.json_response(resp)

        if method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": [{"name": "add", "description": "Add two numbers", "inputSchema": {}}]}
            }
            return web.json_response(resp)

        if method == "tools/call":
            params = payload.get("params", {})
            # Support both shapes: {"tool":..., "input":...} and {"name":..., "arguments":...}
            tool = params.get("tool") or params.get("name")
            inputs = params.get("input") or params.get("arguments") or {}
            # simple behavior: if tool == 'add' expect {'a': x, 'b': y}
            if tool == "add":
                a = inputs.get("a", 0)
                b = inputs.get("b", 0)
                result = {"result": a + b}
                return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})

            return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params"}})

        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})

    app = web.Application()
    app.router.add_post('/mcp', handle_mcp)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()

    try:
        # create client without auth and call tool
        client = await MCPClientFactory.create_http_client(base_url=base_url, client_name="test-client", timeout=5.0, ssl_verify=True)
        tools = await client.list_tools()
        assert any((getattr(t, 'name', None) or (t.get('name') if isinstance(t, dict) else None)) == 'add' for t in tools)

        # call 'add' tool (use client.call_tool(name, arguments))
        res = await client.call_tool('add', {'a': 2, 'b': 3})
        # client returns the result object from the server; our server returns {'result': sum}
        assert isinstance(res, dict)
        assert res.get('result') == 5
        await client.disconnect()

        # now create a client that should send Authorization header via HTTPTransport
        # Build a pre-configured aiohttp session with Authorization header and attach it to HTTPTransport
        from agent_system.mcp.http_transport import HTTPTransport
        from agent_system.mcp.client import StandardMCPClient

        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True),
            timeout=aiohttp.ClientTimeout(total=5),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"}
        )

        transport2 = HTTPTransport(base_url=base_url, timeout=5.0, ssl_verify=True)
        # attach our prepared session so transport won't create a new one
        transport2.session = session

        client2 = StandardMCPClient(transport2, "auth-client")
        try:
            await client2.connect()
            await client2.initialize()
            _ = await client2.list_tools()
        finally:
            await client2.disconnect()
            await session.close()

        # verify the mock server saw the Authorization header in any request
        found = False
        for h in received_headers:
            if h.get('Authorization') == 'Bearer test-token':
                found = True
                break
        assert found, f"Authorization header not found in requests: {received_headers!r}"

    finally:
        await runner.cleanup()
