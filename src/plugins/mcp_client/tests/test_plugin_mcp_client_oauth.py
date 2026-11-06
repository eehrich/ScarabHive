"""OAuth for a remote MCP server, against a real server behind the SDK's real authorization routes.

The browser is the only stand-in: the test fetches the authorization URL and carries the code off the
redirect to the callback, as a person's browser would.
"""
import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import MCPAuthConfig
from agent_system.config.models.external_servers import RemoteMCPConfig
from plugins.mcp_client.connection import MCPConnectionError
from plugins.mcp_client.elicitation import ElicitBroker
from plugins.mcp_client.manager import ExternalServerPool
from plugins.mcp_client.oauth import OAuthBroker
from plugins.mcp_client.web import build_router

PROBE = Path(__file__).with_name("oauth_probe_server.py")
REDIRECT = "http://127.0.0.1:9/plugins/mcp_client/oauth/callback"   # never dialled: the test carries the code


@contextlib.contextmanager
def oauth_probe():
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    server = subprocess.Popen([sys.executable, str(PROBE), str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(150):
            if server.poll() is not None:
                raise RuntimeError(f"the OAuth probe ended with {server.returncode}")
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"the OAuth probe does not listen on {port}")
        yield port
    finally:
        server.kill()
        server.wait()


def _pool(port, tmp_path, redirect=REDIRECT, **server):
    pool = ExternalServerPool(timeout=10, oauth=OAuthBroker(tmp_path), redirect_uri=redirect)
    pool.configure({"probe": RemoteMCPConfig(url=f"http://127.0.0.1:{port}/mcp", enabled=True,
                                             auth=MCPAuthConfig(type="oauth"), **server)})
    return pool


async def _browser(url: str, callback: str = REDIRECT) -> tuple:
    """What a person's browser does: open the page, get sent back to the callback with code and state."""
    async with httpx.AsyncClient() as client:
        answer = await client.get(url, follow_redirects=False)
    assert answer.status_code in (302, 303, 307), answer.text
    query = parse_qs(urlsplit(answer.headers["location"]).query)
    assert answer.headers["location"].startswith(callback), answer.headers["location"]
    return query["state"][0], query["code"][0]


async def _authorize(pool, broker, callback=REDIRECT):
    broker.url_event("probe").clear()
    task = asyncio.create_task(pool.connect("probe", interactive=True))
    await asyncio.wait_for(broker.url_event("probe").wait(), 20)
    state, code = await _browser(broker.urls["probe"], callback)
    assert broker.complete(state, code)
    return await asyncio.wait_for(task, 20)


@pytest.mark.asyncio
async def test_a_server_behind_oauth_is_authorized_once_then_connects_and_refreshes_alone(tmp_path):
    with oauth_probe() as port:
        pool = _pool(port, tmp_path)
        try:
            # Nobody authorized yet: a connect that may not wait for a person says what to do, at once
            with pytest.raises(MCPConnectionError, match="mcp_client_authorize"):
                await asyncio.wait_for(pool.connect("probe"), 20)

            connection = await _authorize(pool, pool.oauth)
            assert await pool.call_tool("probe", "add", {"a": 2, "b": 3}) == "5"
            stored = tmp_path / "probe.json"
            assert stored.exists(), "the tokens were not kept"
            if os.name == "posix":
                assert stored.stat().st_mode & 0o077 == 0, oct(stored.stat().st_mode)

            # A new connection (a restart, another process) needs no person: the stored tokens --
            # expired by now (2 s), so the refresh grant has to bring new ones
            await pool.disconnect("probe")
            await asyncio.sleep(2.5)
            connection = await asyncio.wait_for(pool.connect("probe"), 20)
            assert connection.connected
            assert int(await pool.call_tool("probe", "refreshes", {})) >= 1, "no refresh grant was asked"
        finally:
            await pool.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, {"tokens": {"access_token": "dead", "token_type": "Bearer"}}],
                         ids=["no tokens", "dead tokens, no client"])
async def test_a_connect_nobody_can_authorize_registers_nothing_at_the_server(tmp_path, stored):
    """Nobody to open a link: refused at the server's 401. Left to the SDK, it registered this app at the
    authorization server first -- and a server refusing the registration showed that error instead of the
    way to authorize."""
    import json

    if stored is not None:
        (tmp_path / "probe.json").write_text(json.dumps(stored), encoding="utf-8")
    with oauth_probe() as port:
        pool = _pool(port, tmp_path)
        try:
            with pytest.raises(MCPConnectionError, match="needs authorization: call mcp_client_authorize"):
                await asyncio.wait_for(pool.connect("probe"), 20)
        finally:
            await pool.close_all()
    file = tmp_path / "probe.json"
    kept = json.loads(file.read_text(encoding="utf-8")) if file.exists() else None
    assert kept == stored, f"the connect registered a client at the server: {kept}"


def _unattended(tmp_path, answer):
    """A provider nobody can authorize with, in front of a server that answers with ``answer(request)``."""
    import types

    import httpx2

    seen = []

    def handler(request):
        seen.append(f"{request.method} {request.url.path}")
        return answer(request)

    config = types.SimpleNamespace(url="http://srv.test/mcp", auth=types.SimpleNamespace(client_id=None))
    provider = OAuthBroker(tmp_path).provider("srv", config, REDIRECT, interactive=False)
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=provider), seen


@pytest.mark.asyncio
async def test_a_server_that_answers_without_a_login_is_reached_without_one(tmp_path):
    import httpx2

    client, seen = _unattended(tmp_path, lambda request: httpx2.Response(200, json={}))
    async with client:
        assert (await client.post("http://srv.test/mcp", json={})).status_code == 200
    assert seen == ["POST /mcp"], seen
    assert not (tmp_path / "srv.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("challenge, stops", [('Bearer error="insufficient_scope", scope="more"', True),
                                              ('Bearer error="invalid_token", error_description="insufficient_scope"',
                                               False)])
async def test_a_403_stops_only_for_a_missing_scope(tmp_path, challenge, stops):
    """A scope step-up needs a person like a 401; any other 403 is the server's answer, passed on."""
    import httpx2

    client, seen = _unattended(tmp_path, lambda request: httpx2.Response(
        403, headers={"WWW-Authenticate": challenge}, json={}))
    async with client:
        if stops:
            with pytest.raises(Exception, match="needs authorization"):
                await client.post("http://srv.test/mcp", json={})
        else:
            assert (await client.post("http://srv.test/mcp", json={})).status_code == 403
    assert seen == ["POST /mcp"], seen


@pytest.mark.asyncio
async def test_a_refused_refresh_still_sends_the_request(tmp_path):
    """The 401 of the token endpoint is the SDK's: it drops the token and sends the request without it."""
    import json

    import httpx2

    (tmp_path / "srv.json").write_text(json.dumps({
        "tokens": {"access_token": "old", "token_type": "Bearer", "refresh_token": "r", "expires_in": 1},
        "tokens_saved_at": 1,     # long ago: the token ran out
        "client_info": {"client_id": "c", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"},
    }), encoding="utf-8")
    client, seen = _unattended(
        tmp_path, lambda request: httpx2.Response(401 if request.url.path == "/token" else 200, json={}))
    async with client:
        assert (await client.post("http://srv.test/mcp", json={})).status_code == 200
    assert seen == ["POST /token", "POST /mcp"], seen


def _callback_client(broker):
    server = type("S", (), {"oauth": broker, "callback_path": "/plugins/mcp_client/oauth/callback", "name": "mcp_client",
                            "questions": ElicitBroker(), "answer_url": "/plugins/mcp_client/answer"})()
    app = FastAPI()
    app.include_router(build_router(server))
    return TestClient(app)


def test_the_callback_hands_the_code_only_to_an_authorization_that_waits(tmp_path):
    broker = OAuthBroker(tmp_path)
    client = _callback_client(broker)
    path = "/plugins/mcp_client/oauth/callback"

    assert client.get(path, params={"state": "made-up", "code": "c"}).status_code == 400
    assert not list((tmp_path / "pending").glob("*.answer")) if (tmp_path / "pending").exists() else True

    wait = broker._file("s1", "wait")
    wait.parent.mkdir(parents=True)
    wait.write_text("probe")
    assert client.get(path, params={"state": "s1", "code": "the-code"}).status_code == 200
    # the first answer counts: a second one for the same state changes nothing
    assert client.get(path, params={"state": "s1", "code": "other"}).status_code == 400
    assert '"the-code"' in broker._file("s1", "answer").read_text()


def test_a_refusal_reaches_the_waiting_authorization(tmp_path):
    broker = OAuthBroker(tmp_path)
    client = _callback_client(broker)
    wait = broker._file("s2", "wait")
    wait.parent.mkdir(parents=True)
    wait.write_text("probe")
    answer = client.get("/plugins/mcp_client/oauth/callback", params={"state": "s2", "error": "access_denied"})
    assert answer.status_code == 400
    assert "access_denied" in broker._file("s2", "answer").read_text()


@pytest.mark.asyncio
async def test_the_authorize_tool_hands_out_the_link_and_the_callback_route_connects_the_server(tmp_path):
    """The way a person goes: /mcp-authorize gives the link, the browser comes back to this app's route."""
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.mcp_client.server import MCPClientServer

    with oauth_probe() as port:
        plugin = MCPClientServer("mcp_client", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
        plugin.oauth._directory = tmp_path
        plugin.pool.configure({"probe": RemoteMCPConfig(url=f"http://127.0.0.1:{port}/mcp", enabled=True,
                                                        auth=MCPAuthConfig(type="oauth", redirect_uri=REDIRECT))})
        try:
            answer = await plugin.authorize({"server": "probe"})
            assert answer["success"], answer
            state, code = await _browser(answer["authorize_url"])
            route = await asyncio.to_thread(_callback_client(plugin.oauth).get, plugin.callback_path,
                                            params={"state": state, "code": code})
            assert route.status_code == 200, route.text
            await asyncio.wait_for(plugin._authorizations["probe"], 20)
            assert plugin.pool.get("probe").connected
            assert (await plugin.authorize({"server": "probe"}))["message"].startswith("connected already")
        finally:
            await plugin.pool.close_all()


@pytest.mark.asyncio
async def test_authorize_refuses_a_server_without_oauth(tmp_path):
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.mcp_client.server import MCPClientServer

    plugin = MCPClientServer("mcp_client", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
    plugin.pool.configure({"plain": RemoteMCPConfig(url="http://127.0.0.1:9/mcp", enabled=True)})
    answer = await plugin.authorize({"server": "plain"})
    assert not answer["success"] and "oauth" in answer["error"], answer


@pytest.mark.asyncio
async def test_a_connect_that_may_not_wait_is_refused_while_an_authorization_waits(tmp_path):
    """The authorization holds the server's connect lock until the person is done; every run that names
    an on_demand server connects first -- it must not hang behind the browser."""
    with oauth_probe() as port:
        pool = _pool(port, tmp_path, connect="on_demand")
        try:
            task = asyncio.create_task(pool.connect("probe", interactive=True))
            await asyncio.wait_for(pool.oauth.url_event("probe").wait(), 20)
            with pytest.raises(MCPConnectionError, match="waits for its authorization"):
                await asyncio.wait_for(pool.connect("probe"), 2)
            started = asyncio.get_running_loop().time()
            assert not await asyncio.wait_for(pool.connect_on_demand(["probe"]), 2), "a run would have waited"
            assert asyncio.get_running_loop().time() - started < 1.5
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            await pool.close_all()


@pytest.mark.asyncio
async def test_a_connection_made_by_an_authorization_does_not_wait_for_a_person_later(tmp_path):
    with oauth_probe() as port:
        pool = _pool(port, tmp_path)
        try:
            connection = await _authorize(pool, pool.oauth)
            assert connection.http_auth.interactive is False
        finally:
            await pool.close_all()


@pytest.mark.asyncio
async def test_a_moved_callback_address_keeps_the_refresh_and_registers_anew_when_authorizing(tmp_path):
    """redirect_uri or the port changed: the refresh needs the client, not the address -- dropping the
    registration at once cost a working authorization. An authorization needs the address: there the old
    registration goes, and the next authorization registers for the new one."""
    import json

    moved_to = "http://127.0.0.1:9/elsewhere/oauth/callback"
    with oauth_probe() as port:
        pool = _pool(port, tmp_path)
        try:
            await _authorize(pool, pool.oauth)
        finally:
            await pool.close_all()
        await asyncio.sleep(2.5)                       # the access token runs out
        moved = _pool(port, tmp_path, redirect=moved_to)
        try:
            connection = await asyncio.wait_for(moved.connect("probe"), 20)
            assert connection.connected, "the refresh was lost with the moved address"
            await moved.disconnect("probe")

            stored = tmp_path / "probe.json"
            data = json.loads(stored.read_text())
            data.pop("tokens")                          # nothing to refresh: an authorization is needed
            stored.write_text(json.dumps(data))
            moved.oauth.url_event("probe").clear()
            with pytest.raises(MCPConnectionError, match="registered there for another callback address"):
                await asyncio.wait_for(moved.connect("probe", interactive=True), 20)
            connection = await _authorize(moved, moved.oauth, callback=moved_to)
            assert connection.connected
        finally:
            await moved.close_all()


@pytest.mark.asyncio
async def test_the_message_names_the_tool_of_this_instance(tmp_path):
    with oauth_probe() as port:
        pool = _pool(port, tmp_path)
        pool.oauth.authorize_tool = "team_mcp_authorize"
        try:
            with pytest.raises(MCPConnectionError, match="call team_mcp_authorize"):
                await asyncio.wait_for(pool.connect("probe"), 20)
        finally:
            await pool.close_all()


def test_the_authorize_tool_is_named_after_the_instance():
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.mcp_client.server import MCPClientServer

    plugin = MCPClientServer("team_mcp", AgentSystemConfig(), ToolServerConfig(type="mcp_client"))
    assert plugin.oauth.authorize_tool == "team_mcp_authorize"


@pytest.mark.parametrize("host, address", [("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("[::]", "[::1]"),
                                           ("::1", "[::1]"), ("hive.lan", "hive.lan"), ("127.0.0.1", "127.0.0.1")])
def test_the_callback_address_is_one_a_browser_can_reach(host, address):
    """network.host 0.0.0.0 (every interface) is no address: Notion refused http://0.0.0.0 as a callback."""
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.mcp_client.server import MCPClientServer

    config = AgentSystemConfig()
    config.network.host, config.network.port = host, 8001
    plugin = MCPClientServer("mcp_client", config, ToolServerConfig(type="mcp_client"))
    assert plugin.default_redirect_uri == f"http://{address}:8001/plugins/mcp_client/oauth/callback"


def test_a_half_written_answer_is_read_again_not_taken(tmp_path):
    from plugins.mcp_client.oauth import _read_answer

    answer = tmp_path / "x.answer"
    assert _read_answer(answer) is None
    answer.write_text('{"code": "ab')
    assert _read_answer(answer) is None
    answer.write_text('{"code": "abc"}')
    assert _read_answer(answer) == {"code": "abc"}
