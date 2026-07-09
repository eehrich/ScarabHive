"""Tests for Agent.dispatch_tool_call (programmatic tool dispatch) and its
parity with schema-build tool filtering.

The security invariant of scripted tool chains (tool_script plugin): a tool the
LLM cannot see (allowed/blocked filtering at schema build) must not be
dispatchable programmatically — and a tool the LLM CAN see must be. Both paths
share ONE matcher (tool_matches_patterns); these tests pin that parity and the
dispatch behavior itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from agent_system.servers.agent.server import Agent
from agent_system.servers.agent.tool_schema_builder import (
    ToolSchemaBuilder,
    tool_matches_patterns,
)
from agent_system.servers.agent.components.tool_execution import (
    ToolDispatchError,
    inject_runtime_params,
)


# ---------------------------------------------------------------------------
# Parity: schema-build filtering <-> dispatch filtering
# ---------------------------------------------------------------------------

MATCH_MATRIX = [
    # (tool_name, server_name, patterns, expected)
    ("v6_json_manage_json", "v6_json", ["v6_json/*"], True),
    ("v6_json_manage_json", "v6_json", ["v6_json"], True),
    ("v6_json_manage_json", "v6_json", ["v6_json/v6_json_manage_json"], True),
    ("v6_json_manage_json", "v6_json", ["other/*"], False),
    ("v6_json_manage_json", "v6_json", ["v6_json_manage_json"], False),  # no slash = server-level
    ("debate_forum_post_message", "debate_forum", ["debate_forum/*"], True),
    ("writer_graph_batch_link", "writer_graph", ["*batch*"], True),
    ("writer_graph_batch_link", "writer_graph", ["writer_graph/writer_graph_*"], True),
    ("some_tool", "some", [], False),
    ("v6_workflow_set_context", "v6_workflow", ["v6_workflow/v6_workflow_set_context"], True),
]


class TestMatcherParity:
    @pytest.fixture
    def schema_builder(self):
        class MockMCPIntegrationManager:
            async def build_tool_schemas(self, tools):
                return [], {}

        return ToolSchemaBuilder(
            agent_name="test_agent",
            mcp_integration_manager=MockMCPIntegrationManager(),
            server_getter_func=lambda name: None,
        )

    @pytest.mark.parametrize("tool,server,patterns,expected", MATCH_MATRIX)
    def test_shared_matcher_matrix(self, tool, server, patterns, expected):
        assert tool_matches_patterns(tool, server, patterns) is expected

    @pytest.mark.parametrize("tool,server,patterns,expected", MATCH_MATRIX)
    def test_schema_build_allowed_delegates_to_shared_matcher(
            self, schema_builder, tool, server, patterns, expected):
        # Schema build (what the LLM sees) must agree with the shared matcher
        assert schema_builder._is_tool_allowed(tool, server, patterns) is expected

    @pytest.mark.parametrize("tool,server,patterns,expected", MATCH_MATRIX)
    def test_schema_build_blocked_delegates_to_shared_matcher(
            self, schema_builder, tool, server, patterns, expected):
        assert schema_builder._is_tool_blocked(tool, server, patterns) is expected


# ---------------------------------------------------------------------------
# dispatch_tool_call behavior (on a minimal fake agent using the REAL methods)
# ---------------------------------------------------------------------------

class FakeServer:
    """Server double capturing the params it was called with."""

    def __init__(self, result: Any = None):
        self.result = result if result is not None else {"status": "ok"}
        self.calls = []

    async def call_with_status(self, tool_name: str, params: Dict[str, Any]):
        self.calls.append(("call_with_status", tool_name, params))
        return self.result

    async def call(self, tool_name: str, params: Dict[str, Any]):
        self.calls.append(("call", tool_name, params))
        return self.result


class PlainServer:
    """Server double WITHOUT call_with_status (fallback path)."""

    def __init__(self, result: Any = None):
        self.result = result if result is not None else {"status": "ok"}
        self.calls = []

    async def call(self, tool_name: str, params: Dict[str, Any]):
        self.calls.append(("call", tool_name, params))
        return self.result


def make_agent(servers: Dict[str, Any], allowed=None, blocked=None):
    """Minimal agent double running the REAL dispatch/resolution methods."""

    class FakeAgent:
        name = "test_agent"
        dispatch_tool_call = Agent.dispatch_tool_call
        _resolve_flat_tool_name = Agent._resolve_flat_tool_name

        def __init__(self):
            self._servers = servers
            self.agent_config = SimpleNamespace(
                tools=SimpleNamespace(allowed=allowed or [], blocked=blocked or []))

        def _get_server_from_any_registry(self, server_name):
            return self._servers.get(server_name)

    return FakeAgent()


class TestDispatchToolCall:
    @pytest.mark.asyncio
    async def test_allowed_tool_dispatches(self):
        srv = FakeServer(result={"status": "ok", "doc": "x"})
        agent = make_agent({"v6_json": srv}, allowed=["v6_json/*"])
        result = await agent.dispatch_tool_call(
            "v6_json_manage_json", {"operation": "list"}, session_id="s1")
        assert result == {"status": "ok", "doc": "x"}
        kind, tool, params = srv.calls[0]
        assert kind == "call_with_status"          # status wrapper preferred
        assert tool == "v6_json_manage_json"       # flat name passed through
        assert params["_session_id"] == "s1"       # runtime injection
        assert params["_agent"] is agent
        assert params["_agent_name"] == "test_agent"

    @pytest.mark.asyncio
    async def test_not_allowed_tool_rejected(self):
        agent = make_agent({"v6_json": FakeServer()}, allowed=["other/*"])
        with pytest.raises(ToolDispatchError, match="not in this agent's allowed"):
            await agent.dispatch_tool_call("v6_json_manage_json", {})

    @pytest.mark.asyncio
    async def test_deny_all_when_no_allowlist(self):
        # Schema build shows zero tools without an allowlist; dispatch must too.
        agent = make_agent({"v6_json": FakeServer()}, allowed=[])
        with pytest.raises(ToolDispatchError, match="not in this agent's allowed"):
            await agent.dispatch_tool_call("v6_json_manage_json", {})

    @pytest.mark.asyncio
    async def test_blocked_tool_rejected_even_if_allowed(self):
        # The v1-design bug class: allowed by pattern but agent-level blocked.
        agent = make_agent(
            {"v6_json": FakeServer()},
            allowed=["v6_json/*"],
            blocked=["v6_json/v6_json_manage_json"])
        with pytest.raises(ToolDispatchError, match="blocked"):
            await agent.dispatch_tool_call("v6_json_manage_json", {})

    @pytest.mark.asyncio
    async def test_unknown_tool_rejected(self):
        agent = make_agent({}, allowed=["*"])
        with pytest.raises(ToolDispatchError, match="Unknown tool"):
            await agent.dispatch_tool_call("nope_not_here", {})

    @pytest.mark.asyncio
    async def test_external_mcp_tool_rejected(self):
        agent = make_agent({}, allowed=["*"])
        with pytest.raises(ToolDispatchError, match="external MCP"):
            await agent.dispatch_tool_call("context7.resolve-library-id", {})

    @pytest.mark.asyncio
    async def test_longest_prefix_resolution(self):
        # Two servers sharing a prefix: the longer one must win.
        short = FakeServer()
        long_ = FakeServer()
        agent = make_agent(
            {"writer": short, "writer_graph": long_}, allowed=["writer_graph/*"])
        await agent.dispatch_tool_call("writer_graph_batch_link", {})
        assert long_.calls and not short.calls

    @pytest.mark.asyncio
    async def test_plain_call_fallback(self):
        srv = PlainServer(result={"ok": True})
        agent = make_agent({"plain": srv}, allowed=["plain/*"])
        result = await agent.dispatch_tool_call("plain_do_it", {})
        assert result == {"ok": True}
        assert srv.calls[0][0] == "call"

    @pytest.mark.asyncio
    async def test_caller_cannot_forge_runtime_params(self):
        # Script-supplied _session_id/_agent must be overwritten by injection.
        srv = FakeServer()
        agent = make_agent({"v6_json": srv}, allowed=["v6_json/*"])
        await agent.dispatch_tool_call(
            "v6_json_manage_json",
            {"_session_id": "forged", "_agent": "forged", "operation": "list"},
            session_id="real")
        _, _, params = srv.calls[0]
        assert params["_session_id"] == "real"
        assert params["_agent"] is agent

    @pytest.mark.asyncio
    async def test_request_id_forwarded(self):
        srv = FakeServer()
        agent = make_agent({"v6_json": srv}, allowed=["v6_json/*"])
        await agent.dispatch_tool_call(
            "v6_json_manage_json", {}, request_id="rid_ts01")
        _, _, params = srv.calls[0]
        assert params["_request_id"] == "rid_ts01"
        assert params["request_id"] == "rid_ts01"

    @pytest.mark.asyncio
    async def test_caller_supplied_runtime_params_stripped_without_session(self):
        # Finding C: inject_runtime_params overwrites _session_id only when one
        # is set, so WITHOUT a session a forged _session_id would survive and
        # impersonate another agent (defeating json_store write protection).
        # dispatch_tool_call must strip caller-supplied _* keys first.
        srv = FakeServer()
        agent = make_agent({"v6_json": srv}, allowed=["v6_json/*"])
        await agent.dispatch_tool_call(
            "v6_json_manage_json",
            {"operation": "merge", "_session_id": "victim", "_agent_name": "boss"},
            session_id=None)
        _, _, params = srv.calls[0]
        assert params.get("_session_id") != "victim"
        assert params.get("_agent_name") != "boss"   # real agent re-injected
        assert params["_agent"] is agent
        assert params["operation"] == "merge"        # real args survive


class TestInjectRuntimeParams:
    def test_injects_and_overwrites(self):
        params = inject_runtime_params(
            {"a": 1, "_session_id": "forged"},
            session_id="s", request_id="r", agent=None)
        assert params["a"] == 1
        assert params["_session_id"] == "s"
        assert params["_request_id"] == "r"

    def test_original_not_mutated(self):
        original = {"a": 1}
        inject_runtime_params(original, session_id="s")
        assert original == {"a": 1}
