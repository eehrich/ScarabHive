"""mcp_gateway end to end: the mcp SDK's own client talks to the plugin's route in a FastAPI app, the calls go
through a real runner Agent to a real tool server. Nothing of the protocol is hand-written here -- what the SDK
client accepts, an outside CLI's client accepts.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from agent_system import app_state
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.hooks import HookResult, HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServer, ToolServerRegistry
from plugins.mcp_gateway import grants
from plugins.mcp_gateway.plugin import McpGatewayPlugin

URL = "http://gw/plugins/mcp_gateway/mcp"
OPS = {"type": "array", "items": {"type": "object", "properties": {
    "op": {"type": "string", "enum": ["get", "edit"]}, "id": {"type": "integer"}}}}


class _Store(ToolServer):
    """A stand-in for a store a run reads and writes; records what each call received."""

    def __init__(self):
        super().__init__("store", AgentSystemConfig(), ToolServerConfig(type="store", enabled=True))
        self.received: list[tuple[str, dict[str, Any]]] = []
        self.started = asyncio.Event()
        self.stopped: list[str] = []

    def get_tools(self):
        def tool(name, properties, required=()):
            return {"type": "function", "function": {"name": name, "description": f"{name} of the store",
                    "parameters": {"type": "object", "properties": properties, "required": list(required)}}}

        return [tool("store_batch", {"project_id": {"type": "integer"}, "operations": OPS}, ["project_id", "operations"]),
                tool("store_echo", {"project_id": {"type": "integer"}, "text": {"type": "string"}}, ["project_id"]),
                tool("store_global", {"text": {"type": "string"}}),
                tool("store_secret", {"project_id": {"type": "integer"}}),
                {"type": "function", "function": {"name": "store_any", "description": "takes anything",
                                                  "parameters": {"type": "object"}}}]

    async def _record(self, name, params):
        from agent_system.core.request_context import current_run_user
        from agent_system.tools.status import current_request_id

        self.received.append((name, {**params, "ctx": (current_request_id.get(), current_run_user.get())}))
        return {"status": "ok", "project_id": params.get("project_id"), "text": params.get("text")}

    async def store_batch(self, params):
        return await self._record("store_batch", params)

    async def store_echo(self, params):
        if params.get("text") == "big":
            return {"status": "ok", "text": "x" * 5000}
        if params.get("text") == "fail":
            return {"status": "error", "error": "the store says no"}
        if params.get("text") == "slow":
            self.started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.stopped.append("slow")
                raise
            return await self._record("store_echo", {**params, "text": "slow-end"})
        return await self._record("store_echo", params)

    async def store_global(self, params):
        return await self._record("store_global", params)

    async def store_secret(self, params):
        return await self._record("store_secret", params)

    async def store_any(self, params):
        return await self._record("store_any", params)


@pytest.fixture
async def gateway(monkeypatch):
    """(app, store): the gateway's route in an app, its runner allowed every store tool but store_secret."""
    store = _Store()
    tools_of_runner = ToolServerRegistry()
    tools_of_runner.register("store", store)
    llm = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                          profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    runner = Agent("mcp_gateway_runner", AgentSystemConfig(llm_system=llm), ToolServerConfig(
        type="agent", enabled=True, agent_config=AgentConfig(
            max_steps=1, llm_profile="normal",
            tools=ToolConfig(allowed=["store/*"], blocked=["store/store_secret"]))), tools_of_runner)
    registry = ToolServerRegistry()
    registry.register("mcp_gateway_runner", runner)
    monkeypatch.setattr(app_state, "app_registry", registry)
    plugin = McpGatewayPlugin("mcp_gateway", AgentSystemConfig(),
                              ToolServerConfig(type="mcp_gateway", enabled=True, max_result_chars=2000))
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    plugin.router_mounted()
    app.state.plugin = plugin
    yield app, store
    await plugin.stop_plugin()
    grants._grants.clear()


def issue(**kwargs):
    return grants.issue(**{"user_id": "ada", "tools": ["store_*"], "request_id": "run7", **kwargs})


@asynccontextmanager
async def session_of(app, token):
    """An initialized ClientSession of the mcp SDK on the gateway, sending ``token``."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"})
    async with client, streamable_http_client(URL, http_client=client) as streams:
        async with ClientSession(dispatcher=JSONRPCDispatcher(streams[0], streams[1])) as session:
            await session.initialize()
            yield session


async def call(app, token, name, arguments):
    async with session_of(app, token) as session:
        result = await session.call_tool(name, arguments)
    return result.is_error, result.content[0].text


async def post(app, token):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        headers = {"Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return await client.post(URL, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})


class TestListing:
    async def test_the_client_sees_what_grant_and_runner_both_allow(self, gateway):
        app, _ = gateway
        async with session_of(app, issue()) as session:
            names = [tool.name for tool in (await session.list_tools()).tools]
        assert names == ["store_any", "store_batch", "store_echo", "store_global"]  # store_secret: runner-blocked

    async def test_a_grant_narrows_the_runner(self, gateway):
        app, _ = gateway
        async with session_of(app, issue(tools=["store_echo"])) as session:
            assert [tool.name for tool in (await session.list_tools()).tools] == ["store_echo"]
        assert await call(app, issue(tools=["store_echo"]), "store_global", {}) == (
            True, "Unknown tool 'store_global': call one from tools/list.")

    async def test_pinned_parameters_are_not_the_models_and_a_tool_without_them_is_gone(self, gateway):
        app, _ = gateway
        async with session_of(app, issue(pinned={"store_*": {"project_id": 12}})) as session:
            tools = {tool.name: tool.input_schema for tool in (await session.list_tools()).tools}
        assert sorted(tools) == ["store_batch", "store_echo"]   # store_global, store_any: no project_id to hold
        assert "project_id" not in tools["store_echo"]["properties"]
        assert tools["store_batch"]["required"] == ["operations"]

    async def test_bindings_hold_only_the_tools_their_pattern_names(self, gateway):
        """Per tool: one tool's parameter is fixed, the others are not touched -- not hidden for lacking it."""
        app, store = gateway
        token = issue(pinned={"store/store_echo": {"text": "fixed"}})
        async with session_of(app, token) as session:
            tools = {tool.name: tool.input_schema for tool in (await session.list_tools()).tools}
        assert sorted(tools) == ["store_any", "store_batch", "store_echo", "store_global"]
        assert "text" not in tools["store_echo"]["properties"] and "project_id" in tools["store_echo"]["properties"]
        assert (await call(app, token, "store_echo", {"project_id": 4}))[0] is False
        assert [(p["project_id"], p["text"]) for _, p in store.received] == [(4, "fixed")]

    async def test_a_tool_held_to_two_values_is_gone(self, gateway):
        app, _ = gateway
        token = issue(pinned={"store_*": {"project_id": 1}, "store/store_echo": {"project_id": 2}})
        async with session_of(app, token) as session:
            assert sorted(tool.name for tool in (await session.list_tools()).tools) == ["store_batch"]


class TestCalls:
    async def test_a_call_runs_for_the_grants_user_session_and_request_id(self, gateway):
        app, store = gateway
        token = issue(session_id="sess-1")
        assert (await call(app, token, "store_echo", {"project_id": 3, "text": "hi"}))[0] is False
        await call(app, token, "store_echo", {"project_id": 3, "text": "again"})
        (_, first), (_, second) = store.received
        assert (first["_user_id"], first["_session_id"], first["request_id"]) == ("ada", "sess-1", "run7_mcp001")
        assert second["request_id"] == "run7_mcp002"
        assert (first["ctx"], second["ctx"]) == (("run7_mcp001", "ada"), ("run7_mcp002", "ada")),             "the run's context variables: what a tool reports without an agent is the run's"

    async def test_a_pinned_value_is_set_and_another_one_refused(self, gateway):
        app, store = gateway
        token = issue(pinned={"store_*": {"project_id": 12}})
        assert (await call(app, token, "store_echo", {"text": "a"}))[0] is False
        assert (await call(app, token, "store_echo", {"text": "b", "project_id": 12}))[0] is False
        failed, text = await call(app, token, "store_echo", {"text": "c", "project_id": 5})
        assert failed and "project_id is fixed to 12" in text
        assert [(p["project_id"], p["text"]) for _, p in store.received] == [(12, "a"), (12, "b")]

    async def test_allowed_values_reach_into_lists(self, gateway):
        app, store = gateway
        token = issue(pinned={"store_*": {"project_id": 1}}, allowed={"store_batch": {"operations[].op": ["get"]}})
        assert (await call(app, token, "store_batch", {"operations": [{"op": "get", "id": 1}]}))[0] is False
        failed, text = await call(app, token, "store_batch", {"operations": [{"op": "get"}, {"op": "edit"}]})
        assert failed and text == "operations[1].op must be one of ['get'] for this run, not 'edit'"
        failed, text = await call(app, token, "store_batch", {"operations": [{"id": 4}]})
        assert failed and text == "operations[0].op is required for this run, one of ['get']"
        assert len(store.received) == 1

    async def test_arguments_are_checked_against_the_tools_schema(self, gateway):
        app, store = gateway
        token = issue()
        failed, text = await call(app, token, "store_echo", {"project_id": "three"})
        assert failed and text.startswith("store_echo: project_id: 'three' is not of type 'integer'")
        failed, text = await call(app, token, "store_echo", {"project_id": 1, "colour": "red"})
        assert failed and "unknown parameter(s) ['colour']" in text
        assert (await call(app, token, "store_echo", {"text": "x"}))[1].startswith(
            "store_echo: the arguments: 'project_id' is a required property")
        assert store.received == []

    async def test_a_runtime_parameter_from_the_client_does_not_reach_the_tool(self, gateway):
        app, store = gateway
        token = issue(session_id="sess-1")
        failed, text = await call(app, token, "store_global", {"text": "x", "_session_id": "someone-else"})
        assert failed and "unknown parameter(s) ['_session_id']" in text
        assert store.received == []
        # A tool without declared parameters takes anything: the framework's own values must win there.
        forged = {"_session_id": "someone-else", "_user_id": "eve", "request_id": "theirs", "requestId": "theirs"}
        assert (await call(app, token, "store_any", forged))[0] is False
        (_, got), = store.received
        assert (got["_session_id"], got["_user_id"], got["request_id"], got["requestId"]) == (
            "sess-1", "ada", "run7_mcp001", "run7_mcp001")
        # Without a session of its own nothing overwrites a forged one: it must be dropped, not passed on.
        assert (await call(app, issue(), "store_any", forged))[0] is False
        assert "_session_id" not in store.received[1][1]

    async def test_a_tool_error_and_an_oversized_result_are_errors(self, gateway):
        app, _ = gateway
        token = issue()
        failed, text = await call(app, token, "store_echo", {"project_id": 1, "text": "fail"})
        assert failed and "the store says no" in text
        failed, text = await call(app, token, "store_echo", {"project_id": 1, "text": "big"})
        assert failed and "more than the 2000 this gateway hands on" in text


class TestHooks:
    async def test_the_runners_hooks_see_the_call_without_the_pinned_values_and_can_block_it(self, gateway):
        app, store = gateway
        seen: list[dict[str, Any]] = []

        class Hook(PluginHook):
            async def on_pre_tool_call(self, context):
                seen.append(copy.deepcopy(context.tool_call))
                if context.tool_call["arguments"].get("text") == "stop":
                    return HookResult(success=True, metadata={"block": "not this one"})
                return HookResult(success=True, context=context)

        hooks = get_hook_registry()
        await hooks.register_hook(HookType.PRE_TOOL_CALL, "gw.probe", Hook("gw.probe"))
        try:
            token = issue(pinned={"store_*": {"project_id": 9}})
            assert (await call(app, token, "store_echo", {"text": "go", "project_id": 9}))[0] is False
            assert await call(app, token, "store_echo", {"text": "stop"}) == (True, "not this one")
        finally:
            await hooks.unregister_hook(HookType.PRE_TOOL_CALL, "gw.probe")
        assert [(c["source"], c["arguments"]) for c in seen] == [("mcp_gateway", {"text": "go"}),
                                                                 ("mcp_gateway", {"text": "stop"})]
        assert [p["text"] for _, p in store.received] == ["go"]


class TestTokens:
    async def test_no_wrong_revoked_or_expired_token_reaches_the_server(self, gateway):
        app, _ = gateway
        assert (await post(app, issue())).status_code == 200
        assert (await post(app, None)).status_code == 401
        assert (await post(app, "not-a-token")).status_code == 401
        token = issue()
        assert grants.revoke(token) is True and grants.revoke(token) is False
        assert (await post(app, token)).status_code == 401
        expired = issue(ttl_s=0.001)
        grants._grants[grants._digest(expired)].expires_at = 0
        assert (await post(app, expired)).status_code == 401

    async def test_a_token_of_another_gateway_is_not_taken(self, gateway):
        app, _ = gateway
        grants.serve("other_gateway", True)
        try:
            assert (await post(app, issue(gateway="other_gateway"))).status_code == 401
        finally:
            grants.serve("other_gateway", False)

    async def test_revoking_stops_a_call_of_the_grant_that_still_runs(self, gateway):
        app, store = gateway
        token = issue()
        running = asyncio.ensure_future(call(app, token, "store_echo", {"project_id": 1, "text": "slow"}))
        await asyncio.wait_for(store.started.wait(), 10)
        assert grants.revoke(token)
        failed, text = await asyncio.wait_for(running, 10)
        assert failed and text == "store_echo: the run's token was revoked or expired; the call was stopped."
        assert store.stopped == ["slow"], "the tool's task went on running"

    async def test_a_grant_whose_time_runs_out_stops_its_running_call(self, gateway):
        app, store = gateway
        token = issue(ttl_s=0.5)
        failed, text = await asyncio.wait_for(call(app, token, "store_echo", {"project_id": 1, "text": "slow"}), 10)
        assert failed and "revoked or expired" in text
        assert store.stopped == ["slow"]

    async def test_a_revoke_from_another_thread_stops_the_call(self, gateway):
        app, store = gateway
        token = issue()
        running = asyncio.ensure_future(call(app, token, "store_echo", {"project_id": 1, "text": "slow"}))
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(store.started.wait(), 10)
            loop.set_debug(True)  # debug mode raises on a loop call from another thread
            try:
                assert await asyncio.to_thread(grants.revoke, token)
            finally:
                loop.set_debug(False)
            failed, text = await asyncio.wait_for(running, 10)
        finally:
            running.cancel()  # a failed check must not leave the call to hang the teardown
        assert failed and store.stopped == ["slow"]

    async def test_a_stopped_gateway_serves_again_once_started(self, gateway):
        app, _ = gateway
        instance = app.state.plugin
        assert (await call(app, issue(), "store_echo", {"project_id": 1, "text": "before"}))[0] is False
        old = issue()
        await instance.stop_plugin()
        with pytest.raises(LookupError):
            issue()
        assert (await post(app, old)).status_code == 401, "a stopped gateway still served"
        await instance.start_plugin()
        assert (await post(app, old)).status_code == 401, "a grant of before the stop came back"
        assert (await call(app, issue(), "store_echo", {"project_id": 1, "text": "back"}))[0] is False

    @pytest.mark.parametrize("kwargs,word", [
        ({"user_id": ""}, "user_id"), ({"tools": []}, "tools"), ({"request_id": " "}, "request_id"),
        ({"ttl_s": 0}, "ttl_s"), ({"pinned": {"store_*": {"_session_id": "x"}}}, "cannot be bound"),
        ({"pinned": {"store_*": {"request_id": "x"}}}, "cannot be bound"),
        ({"pinned": {"project_id": 1}}, "must map parameters"),
        ({"pinned": {"store_*": {"project_id": [1]}}}, "string, number or boolean"),
        ({"allowed": {"store_*": {"ops[0].op": ["get"]}}}, "no path"),
        ({"allowed": {"store_*": {"operations[].op": []}}}, "non-empty")])
    async def test_a_grant_that_cannot_hold_is_refused(self, gateway, kwargs, word):
        with pytest.raises(ValueError, match=word):
            issue(**kwargs)

    async def test_no_grant_for_a_gateway_that_does_not_serve(self, gateway):
        with pytest.raises(LookupError):
            issue(gateway="nowhere")

    async def test_a_gateway_serves_only_once_the_app_has_mounted_its_route(self):
        """Every process builds the plugin; only the app's mounts its route. Before that, no grant."""
        from agent_system.plugins.web_adapter import PluginWebRegistry

        plugin = McpGatewayPlugin("mcp_gateway_mount", AgentSystemConfig(),
                                  ToolServerConfig(type="mcp_gateway", enabled=True))
        try:
            await plugin.start_plugin()
            with pytest.raises(LookupError):
                issue(gateway="mcp_gateway_mount")
            web = PluginWebRegistry()
            web.register_web_plugin("mcp_gateway_mount", plugin)
            with pytest.raises(LookupError):
                issue(gateway="mcp_gateway_mount")   # asked for its router, as every process does
            web.apply_to_app(FastAPI())
            assert issue(gateway="mcp_gateway_mount")
        finally:
            await plugin.stop_plugin()


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_the_shipped_security_config_lets_a_cli_without_a_login_reach_the_route(method):
    """Both auth layers would answer the CLI 401 before the route could check its token."""
    from agent_system.auth.enforcement import EndpointSecurityEnforcer
    from agent_system.config.settings import load_settings
    from agent_system.paths import PROJECT_ROOT
    from agent_system.plugins.web_adapter import PluginEndpointSecurityEnforcer

    auth = load_settings(str(PROJECT_ROOT / "config" / "config.yaml")).auth
    assert auth.enabled, "the shipped config has auth off: this test would prove nothing"
    path = "/plugins/mcp_gateway/mcp"
    assert EndpointSecurityEnforcer(auth).get_endpoint_policy(method, path).requires_auth is False
    assert PluginEndpointSecurityEnforcer(auth).get_plugin_policy("mcp_gateway", path, method)["requires_auth"] is False
    assert PluginEndpointSecurityEnforcer(auth).get_plugin_policy(
        "mcp_gateway", "/plugins/mcp_gateway/other", method)["requires_auth"] is True, "only the one route is open"


async def test_every_call_leaves_an_audit_line_naming_the_grant_not_the_token(gateway, caplog):
    app, _ = gateway
    token = issue()
    with caplog.at_level("INFO", logger="plugins.mcp_gateway.gateway"):
        await call(app, token, "store_echo", {"project_id": 1, "text": "a"})
        await call(app, token, "store_echo", {"project_id": 1, "text": "fail"})
    lines = [r.getMessage() for r in caplog.records if r.name == "plugins.mcp_gateway.gateway"]
    key = grants.lookup("mcp_gateway", token).key
    assert [line.split(" in ")[0] for line in lines] == [f"mcp_gateway mcp_gateway: grant {key} store_echo ok",
                                                         f"mcp_gateway mcp_gateway: grant {key} store_echo error"]
    assert not any(token in line for line in lines)


def test_a_user_cannot_run_the_runner_directly():
    """Run as an agent, its model could call every tool of the allowlist without a grant's pinned and allowed
    values -- only an admin may start it."""
    from types import SimpleNamespace

    from agent_system.auth.agent_access import may_run_agent
    from agent_system.config.settings import get_tool_server_config, load_settings
    from agent_system.paths import PROJECT_ROOT

    config = load_settings(str(PROJECT_ROOT / "config" / "config.yaml"))
    min_role = get_tool_server_config("mcp_gateway_runner", config).metadata.min_role
    assert not may_run_agent(min_role, SimpleNamespace(role="user"), config.auth)
    assert may_run_agent(min_role, SimpleNamespace(role="admin"), config.auth)


async def test_a_call_that_raises_leaves_its_audit_line_too(gateway, caplog, monkeypatch):
    app, _ = gateway
    runner = app_state.app_registry.get("mcp_gateway_runner")

    async def broken():
        raise RuntimeError("discovery broke")

    monkeypatch.setattr(runner, "build_llm_tool_schemas", broken)
    with caplog.at_level("INFO", logger="plugins.mcp_gateway.gateway"), pytest.raises(BaseExceptionGroup) as raised:
        await call(app, issue(), "store_echo", {"project_id": 1})  # the SDK answers a JSON-RPC error
    assert raised.group_contains(Exception, match="discovery broke", depth=None)
    assert [r.getMessage().split(" in ")[0] for r in caplog.records if r.name == "plugins.mcp_gateway.gateway"] \
        == [f"mcp_gateway mcp_gateway: grant {next(iter(grants._grants.values())).key} store_echo error"]
