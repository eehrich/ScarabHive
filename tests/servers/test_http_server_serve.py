"""serve_tool_server and its guards (agent_system.servers.http_server).

The plugins' __main__ serve themselves through it, and the http_server plugin
builds on the same functions: /call must refuse a foreign Host header when no
key is set (DNS rebinding), drop the runtime's `_` parameters, and refuse a
non-loopback bind without a key.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.servers import http_server as hs
from agent_system.tools.base import ToolServer

LOCAL = "http://127.0.0.1:9000"


class Echo(ToolServer):
    """A real tool server: reads `_status` like the plugins do."""

    async def echo(self, params):
        return {"status_seen": params["_status"] is not None,
                "params": {k: v for k, v in params.items() if k != "_status"}}


@pytest.fixture
def echo():
    return Echo("echo", AgentSystemConfig(), ToolServerConfig(type="echo"))


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
    def test_loopback(self, host):
        assert hs.is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "1.2.3.4", "10.0.0.5", "example.com", "::"])
    def test_non_loopback(self, host):
        assert hs.is_loopback_host(host) is False


class TestCall:
    def test_normal_call_works(self, echo):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "echo", "params": {"q": 1}})
        assert r.status_code == 200
        assert r.json() == {"status_seen": True, "params": {"q": 1}}

    def test_underscore_params_are_stripped(self, echo):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "echo",
                                       "params": {"q": 1, "_user_id": "other", "_session_id": "s"}})
        assert r.json()["params"] == {"q": 1}

    def test_server_without_call_with_status_gets_call(self):
        class CallOnly:
            name = "call_only"

            async def call(self, tool, params):
                return {"tool": tool}

        client = TestClient(hs.create_app(CallOnly(), None), base_url=LOCAL)
        assert client.post("/call", json={"tool": "t"}).json() == {"tool": "t"}

    def test_tool_error_is_an_answer(self, echo):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "nope"})
        assert r.status_code == 200
        assert r.json()["error"].startswith("Call failed: Tool 'nope' not found")


class TestHostHeaderWithoutKey:
    @pytest.mark.parametrize("host", ["evil.example:9000", "10.0.0.5", "[::1"])
    def test_foreign_or_malformed_host_gets_403(self, echo, host):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "echo", "params": {}}, headers={"Host": host})
        assert r.status_code == 403

    @pytest.mark.parametrize("host", ["localhost:9000", "[::1]:9000", "127.0.0.1"])
    def test_loopback_host_accepted(self, echo, host):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "echo", "params": {}}, headers={"Host": host})
        assert r.status_code == 200


class TestBrowserRequests:
    """A cross-site page can POST without a preflight only with a "simple"
    content type (none, text/plain, a form); FastAPI parsed such a body as JSON."""

    @pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded"])
    @pytest.mark.parametrize("key", [None, "k"])
    def test_non_json_content_type_gets_415(self, echo, ctype, key):
        client = TestClient(hs.create_app(echo, key), base_url=LOCAL)
        headers = {"X-API-Key": "k"} if key else {}
        if ctype:
            headers["Content-Type"] = ctype
        r = client.post("/call", content=b'{"tool": "echo", "params": {}}', headers=headers)
        assert r.status_code == 415

    def test_json_with_charset_accepted(self, echo):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", content=b'{"tool": "echo", "params": {}}',
                        headers={"Content-Type": "application/json; charset=utf-8"})
        assert r.status_code == 200

    @pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://10.0.0.5:8000"])
    def test_foreign_origin_without_key_gets_403(self, echo, origin):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "echo", "params": {}}, headers={"Origin": origin})
        assert r.status_code == 403

    @pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:9000", "http://[::1]:8080"])
    def test_loopback_origin_without_key_accepted(self, echo, origin):
        client = TestClient(hs.create_app(echo, None), base_url=LOCAL)
        r = client.post("/call", json={"tool": "echo", "params": {}}, headers={"Origin": origin})
        assert r.status_code == 200


class TestKey:
    def test_missing_or_wrong_key_gets_401(self, echo):
        client = TestClient(hs.create_app(echo, "k"), base_url=LOCAL)
        assert client.post("/call", json={"tool": "echo"}).status_code == 401
        assert client.post("/call", json={"tool": "echo"}, headers={"X-API-Key": "x"}).status_code == 401

    @pytest.mark.parametrize("headers", [{"X-API-Key": "k"}, {"Authorization": "Bearer k"}])
    def test_key_accepted_from_any_host(self, echo, headers):
        client = TestClient(hs.create_app(echo, "k"), base_url="http://agents.example:9000")
        assert client.post("/call", json={"tool": "echo", "params": {}}, headers=headers).status_code == 200

    def test_health_stays_open(self, echo):
        client = TestClient(hs.create_app(echo, "k"), base_url="http://agents.example:9000")
        assert client.get("/health").json() == {"status": "ok", "server": "echo"}


class TestServe:
    @pytest.mark.asyncio
    async def test_refuses_non_loopback_bind_without_key(self, echo, monkeypatch):
        monkeypatch.delenv(hs.AUTH_KEY_ENV, raising=False)
        with patch("uvicorn.Server.serve", new=AsyncMock()) as served:
            with pytest.raises(RuntimeError, match="non-loopback"):
                await hs.serve_tool_server(echo, host="0.0.0.0", port=9000)
        served.assert_not_called()

    @pytest.mark.asyncio
    async def test_binds_loopback_by_default_whatever_HOST_says(self, echo, monkeypatch):
        monkeypatch.setenv("HOST", "0.0.0.0")
        monkeypatch.delenv(hs.AUTH_KEY_ENV, raising=False)
        seen = {}
        real_config = hs.uvicorn.Config

        def capture(app, **kw):
            seen.update(kw)
            return real_config(app, **kw)

        with patch.object(hs.uvicorn, "Config", side_effect=capture), \
                patch("uvicorn.Server.serve", new=AsyncMock()):
            await hs.serve_tool_server(echo, port=9000)
        assert seen["host"] == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_runs_the_plugins_start_and_stop_hooks(self, echo):
        order = []
        echo.start_plugin = AsyncMock(side_effect=lambda: order.append("start"))
        echo.stop_plugin = AsyncMock(side_effect=lambda: order.append("stop"))

        async def fake_serve(self_):
            order.append("serve")

        with patch("uvicorn.Server.serve", fake_serve):
            await hs.serve_tool_server(echo, port=9000)
        assert order == ["start", "serve", "stop"]
