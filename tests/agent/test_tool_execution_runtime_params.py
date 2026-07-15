"""Model-supplied runtime params must never reach a tool.

Runtime params (`_session_id`, `_agent`, `_request_id`, ...) are injected by the
framework and identify the CALLER. json_store's write protection binds document
ownership to `_session_id`; if an LLM could pass that key in its tool arguments,
it could impersonate another agent and modify a document it may only read.

`inject_runtime_params` overwrites those keys only when the corresponding value
is truthy — a forged `_session_id` would survive whenever no session id is set.
So the arguments are stripped of `_`-prefixed keys before dispatch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_system.servers.agent.components.tool_execution import (
    ToolExecutionManager,
    inject_runtime_params,
)
from tool_execution_test_helpers import execute_tools_collect


class CapturingServer:
    """Records the params a tool actually received."""

    def __init__(self):
        self.received = None

    async def call_with_status(self, tool_name, params):
        self.received = params
        return {"status": "ok"}


def _tool_call(name: str, arguments: dict) -> dict:
    return {"id": "call-1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _manager(server: CapturingServer) -> ToolExecutionManager:
    agent = MagicMock()
    agent.name = "test_agent"
    agent._get_server_from_any_registry.return_value = server
    agent.agent_config = SimpleNamespace(timeouts=None)
    registry = MagicMock()
    registry.list.return_value = []
    return ToolExecutionManager(registry, agent=agent)


class TestForgedRuntimeParamsAreStripped:
    @pytest.mark.asyncio
    async def test_forged_session_id_does_not_reach_the_tool(self):
        # No session_id passed -> injection would NOT overwrite a forged value.
        server = CapturingServer()
        manager = _manager(server)
        await execute_tools_collect(manager,
            [_tool_call("store_manage_json",
                        {"operation": "merge", "doc": "synopsis",
                         "_session_id": "coordinator"})],
            tool_name_mapping={"store_manage_json": "store_manage_json"},
            available_tools=["store_manage_json"],
            step=0,
        )
        assert server.received is not None
        assert server.received.get("_session_id") != "coordinator"
        assert server.received["operation"] == "merge"   # real args survive

    @pytest.mark.asyncio
    async def test_real_session_id_wins_over_forged_one(self):
        server = CapturingServer()
        manager = _manager(server)
        await execute_tools_collect(manager,
            [_tool_call("store_manage_json", {"_session_id": "victim", "a": 1})],
            tool_name_mapping={"store_manage_json": "store_manage_json"},
            available_tools=["store_manage_json"],
            step=0,
        )
        # execute_tools does not forward a session id; the forged one is gone
        # and nothing impersonates 'victim'.
        assert server.received.get("_session_id") != "victim"

    @pytest.mark.asyncio
    async def test_other_underscore_params_stripped_too(self):
        server = CapturingServer()
        manager = _manager(server)
        await execute_tools_collect(manager,
            [_tool_call("store_manage_json",
                        {"_agent": "fake", "_user_id": "root",
                         "_request_id": "spoof", "doc": "d"})],
            tool_name_mapping={"store_manage_json": "store_manage_json"},
            available_tools=["store_manage_json"],
            step=0,
        )
        assert server.received.get("_agent") != "fake"
        assert "_user_id" not in server.received or server.received["_user_id"] != "root"
        assert server.received.get("_request_id") != "spoof"
        assert server.received["doc"] == "d"


class TestInjectionStillOverwrites:
    def test_injection_overwrites_supplied_values(self):
        params = inject_runtime_params(
            {"_session_id": "forged"}, session_id="real", agent=None)
        assert params["_session_id"] == "real"

    def test_injection_without_session_id_leaves_key_absent(self):
        params = inject_runtime_params({}, session_id=None, agent=None)
        assert "_session_id" not in params

    def test_injection_alone_would_not_stop_a_forged_value(self):
        # Proves the stripping above is load-bearing: with no session id to
        # inject, a forged _session_id passes straight through injection.
        params = inject_runtime_params({"_session_id": "forged"}, session_id=None)
        assert params["_session_id"] == "forged"
