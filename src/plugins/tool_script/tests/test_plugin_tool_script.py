"""Tests for the tool_script plugin — scripted tool chains.

The fake agent implements the same contract as Agent.dispatch_tool_call /
_resolve_flat_tool_name (the real dispatch semantics are covered by
tests/agent/test_dispatch_tool_call.py — here we test the plugin layer:
sandbox bridging, error contract, caps, trace, resumability report).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import pytest
from unittest.mock import MagicMock

from agent_system.config.models import MCPConfig
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from plugins.tool_script.server import ToolScriptServer, ToolCallError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

JSON_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["read", "write", "merge_doc"]},
        "doc": {"type": "string"},
        "namespace": {"type": "string"},
        "data": {"type": "object"},
    },
    "required": ["operation"],
}


class FakeToolServer:
    """Target server double with a schema (for param validation)."""

    def __init__(self, name: str, tools: Dict[str, Dict[str, Any]]):
        self.name = name
        self._tools = tools

    def get_tools(self):
        return [{"type": "function",
                 "function": {"name": n, "parameters": s}}
                for n, s in self._tools.items()]


class FakeAgent:
    """Implements the dispatch contract the plugin relies on."""

    name = "test_agent"

    def __init__(self):
        self.dispatched = []          # (tool_name, params, request_id)
        self.handlers: Dict[str, Any] = {}
        self.schemas: Dict[str, Dict[str, Any]] = {}
        self._server = FakeToolServer("store", self.schemas)

    def add_tool(self, name: str, handler, schema: Dict[str, Any] | None = None):
        self.handlers[name] = handler
        if schema is not None:
            self.schemas[name] = schema

    def _resolve_flat_tool_name(self, tool_name: str):
        if tool_name in self.handlers:
            return self._server, "store"
        return None, None

    async def dispatch_tool_call(self, tool_name, params, *, session_id=None,
                                 user_id=None, request_id=None):
        self.dispatched.append((tool_name, params, request_id))
        handler = self.handlers.get(tool_name)
        if handler is None:
            raise ToolDispatchError(f"Unknown tool: '{tool_name}'.")
        result = handler(params)
        if asyncio.iscoroutine(result):
            result = await result
        return result


@pytest.fixture
def agent():
    a = FakeAgent()
    docs = {"d1": {"x": 1}}

    def json_tool(params):
        op = params.get("operation")
        if op == "read":
            doc = params.get("doc")
            if doc not in docs:
                return {"status": "error",
                        "error": f"Document '{doc}' not found."}
            return {"status": "ok", "doc": doc,
                    "json": json.dumps(docs[doc])}
        if op == "write":
            docs[params.get("doc") or "auto"] = params.get("data") or {}
            return {"status": "ok", "doc": params.get("doc") or "auto"}
        return {"status": "ok"}

    a.add_tool("store_manage_json", json_tool, JSON_TOOL_SCHEMA)
    a.add_tool("forum_post", lambda p: {"status": "ok", "message_id": 42},
               {"type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"]})
    return a


def make_server(**config) -> ToolScriptServer:
    return ToolScriptServer(
        "pipe", MagicMock(),
        MCPConfig(type="tool_script", enabled=True, config=config))


async def run(server, agent, script, **extra):
    return await server.run_script({
        "script": script, "_agent": agent, "_session_id": "sess-1", **extra})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestRunScript:
    @pytest.mark.asyncio
    async def test_simple_chain(self, agent):
        server = make_server()
        res = await run(server, agent, (
            'r = call_tool("store_manage_json", operation="read", doc="d1")\n'
            'call_tool("forum_post", content="data: " + r["json"])\n'
            'result = {"chars": len(r["json"])}'
        ))
        assert res["status"] == "ok"
        assert res["result"] == {"chars": len(json.dumps({"x": 1}))}
        assert [c["tool"] for c in res["calls"]] == ["store_manage_json", "forum_post"]
        assert all(c["ok"] for c in res["calls"])
        # intermediate payload flowed server-side, not through the result
        assert "x" not in json.dumps(res["result"])

    @pytest.mark.asyncio
    async def test_result_defaults_to_null(self, agent):
        server = make_server()
        res = await run(server, agent, 'x = 1')
        assert res["status"] == "ok" and res["result"] is None

    @pytest.mark.asyncio
    async def test_loop_fanout(self, agent):
        server = make_server()
        res = await run(server, agent, (
            'out = []\n'
            'for d in ["a", "b"]:\n'
            '    call_tool("store_manage_json", operation="write", doc=d, data={"n": d})\n'
            '    out.append(d)\n'
            'result = out'
        ))
        assert res["status"] == "ok" and res["result"] == ["a", "b"]
        assert len(res["calls"]) == 2

    @pytest.mark.asyncio
    async def test_log_collected(self, agent):
        server = make_server()
        res = await run(server, agent, 'log("step 1")\nresult = 1')
        assert res["log"] == ["step 1"]

    @pytest.mark.asyncio
    async def test_slicing_works_in_scripts(self, agent):
        server = make_server()
        res = await run(server, agent,
                        'r = call_tool("store_manage_json", operation="read", doc="d1")\n'
                        'result = r["json"][:5]')
        assert res["status"] == "ok"
        assert res["result"] == json.dumps({"x": 1})[:5]

    @pytest.mark.asyncio
    async def test_request_id_lineage(self, agent):
        server = make_server()
        await run(server, agent,
                  'call_tool("forum_post", content="a")\n'
                  'call_tool("forum_post", content="b")\n'
                  'result = 1',
                  _request_id="rid1")
        assert [rid for _, _, rid in agent.dispatched] == ["rid1_ts01", "rid1_ts02"]


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------

class TestErrorContract:
    @pytest.mark.asyncio
    async def test_status_error_raises_and_reports(self, agent):
        server = make_server()
        res = await run(server, agent,
                        'r = call_tool("store_manage_json", operation="read", doc="nope")\n'
                        'result = r')
        assert res["status"] == "error"
        assert "not found" in res["error"]
        assert res["calls"][0]["ok"] is False
        assert "committed" in res["committed_side_effects"]

    @pytest.mark.asyncio
    async def test_bare_error_key_raises(self, agent):
        # debate_forum-style failure: {"error": ...} with NO status key.
        # Must not look like success to the script.
        agent.add_tool("forum_strict", lambda p: {"error": "Channel 5 not found"})
        server = make_server()
        res = await run(server, agent, 'call_tool("forum_strict")\nresult = "reached"')
        assert res["status"] == "error"
        assert "Channel 5 not found" in res["error"]

    @pytest.mark.asyncio
    async def test_non_error_status_with_error_none_is_ok(self, agent):
        # Success shapes must not be misread as failures.
        agent.add_tool("forum_ok", lambda p: {"status": "posted", "message_id": 7})
        agent.add_tool("nullerr", lambda p: {"error": None, "value": 3})
        server = make_server()
        res = await run(server, agent, (
            'a = call_tool("forum_ok")\n'
            'b = call_tool("nullerr")\n'
            'result = [a["message_id"], b["value"]]'
        ))
        assert res["status"] == "ok" and res["result"] == [7, 3]

    @pytest.mark.asyncio
    async def test_tool_error_catchable_in_script(self, agent):
        server = make_server()
        res = await run(server, agent, (
            'try:\n'
            '    call_tool("store_manage_json", operation="read", doc="nope")\n'
            '    result = "no-error"\n'
            'except ToolCallError:\n'
            '    result = "caught"'
        ))
        assert res["status"] == "ok" and res["result"] == "caught"

    @pytest.mark.asyncio
    async def test_unknown_param_fails_at_call_site(self, agent):
        server = make_server()
        res = await run(server, agent,
                        'call_tool("store_manage_json", operation="read", dok="d1")\n'
                        'result = 1')
        assert res["status"] == "error"
        assert "dok" in res["error"]
        assert "operation" in res["error"]  # valid params echoed

    @pytest.mark.asyncio
    async def test_missing_required_param(self, agent):
        server = make_server()
        res = await run(server, agent,
                        'call_tool("store_manage_json", doc="d1")\nresult = 1')
        assert res["status"] == "error"
        assert "operation" in res["error"]

    @pytest.mark.asyncio
    async def test_enum_violation_reported(self, agent):
        server = make_server()
        res = await run(server, agent,
                        'call_tool("store_manage_json", operation="explode")\n'
                        'result = 1')
        assert res["status"] == "error"
        assert "explode" in res["error"] or "enum" in res["error"]

    @pytest.mark.asyncio
    async def test_unknown_tool_dispatch_error(self, agent):
        server = make_server()
        res = await run(server, agent, 'call_tool("no_such_tool")\nresult = 1')
        assert res["status"] == "error"
        assert "Unknown tool" in res["error"]

    @pytest.mark.asyncio
    async def test_failure_report_variables_capped(self, agent):
        server = make_server()
        res = await run(server, agent, (
            'small = "ok"\n'
            'big = "x" * 5000\n'
            'call_tool("store_manage_json", operation="read", doc="nope")'
        ))
        assert res["status"] == "error"
        assert res["variables"]["small"] == "ok"
        assert "omitted" in res["variables"]["big"]
        assert "call_tool" not in res["variables"]  # seeded names excluded

    @pytest.mark.asyncio
    async def test_syntax_error_reported_with_line(self, agent):
        server = make_server()
        res = await run(server, agent, 'x = (1\nresult = 2')
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_keyerror_gets_type_prefix(self, agent):
        # First live run: bare "'teile'" was undiagnosable — the type prefix
        # is what tells the model it was a missing dict key.
        server = make_server()
        res = await run(server, agent, 'd = {"a": 1}\nresult = d["teile"]')
        assert res["status"] == "error"
        assert "teile" in res["error"]
        assert "error" in res["error"].lower() or ":" in res["error"]

    @pytest.mark.asyncio
    async def test_parse_json_seeded(self, agent):
        # Tools return JSON as text (json_store read); parse_json bridges the
        # missing json module — the first live run reached for json.loads.
        server = make_server()
        res = await run(server, agent, (
            'r = call_tool("store_manage_json", operation="read", doc="d1")\n'
            'data = parse_json(r["json"])\n'
            'result = data["x"] + 1'
        ))
        assert res["status"] == "ok" and res["result"] == 2

    @pytest.mark.asyncio
    async def test_parse_json_invalid_input(self, agent):
        server = make_server()
        res = await run(server, agent, (
            'try:\n'
            '    parse_json("not json {")\n'
            '    result = "no-error"\n'
            'except ToolCallError:\n'
            '    result = "caught"'
        ))
        assert res["status"] == "ok" and res["result"] == "caught"


# ---------------------------------------------------------------------------
# Security / caps
# ---------------------------------------------------------------------------

class TestSecurityAndCaps:
    @pytest.mark.asyncio
    async def test_no_recursion(self, agent):
        agent.add_tool("pipe2_run_script", lambda p: {"status": "ok"})
        server = make_server()
        res = await run(server, agent,
                        'call_tool("pipe2_run_script", script="result=1")\nresult = 1')
        assert res["status"] == "error"
        assert "recursion" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_max_tool_calls_not_catchable(self, agent):
        # The abort must NOT be swallowed by `except ToolCallError` — a script
        # cannot loop forever by catching its own budget errors.
        server = make_server(max_tool_calls=2)
        res = await run(server, agent, (
            'for i in [1, 2, 3, 4]:\n'
            '    try:\n'
            '        call_tool("forum_post", content="spam")\n'
            '    except ToolCallError:\n'
            '        pass\n'
            'result = "done"'
        ))
        assert res["status"] == "error"
        assert "max_tool_calls" in res["error"]
        assert len([c for c in res["calls"] if c.get("ok")]) == 2

    @pytest.mark.asyncio
    async def test_allowed_tools_narrowing(self, agent):
        server = make_server(allowed_tools=["store_*"])
        res = await run(server, agent,
                        'call_tool("forum_post", content="x")\nresult = 1')
        assert res["status"] == "error"
        assert "allowed_tools" in res["error"]

    @pytest.mark.asyncio
    async def test_blocked_tools(self, agent):
        server = make_server(blocked_tools=["forum_*"])
        res = await run(server, agent,
                        'call_tool("forum_post", content="x")\nresult = 1')
        assert res["status"] == "error"
        assert "blocked" in res["error"]

    @pytest.mark.asyncio
    async def test_oversized_call_result_rejected(self, agent):
        agent.add_tool("big_tool", lambda p: {"status": "ok", "blob": "x" * 2000})
        server = make_server(max_call_result_bytes=500)
        res = await run(server, agent, 'call_tool("big_tool")\nresult = 1')
        assert res["status"] == "error"
        assert "too large" in res["error"]

    @pytest.mark.asyncio
    async def test_oversized_script_result_rejected(self, agent):
        server = make_server(max_result_chars=100)
        res = await run(server, agent, 'result = "x" * 500')
        assert res["status"] == "error"
        assert "too large" in res["error"]
        assert "reference" in res["error"]

    @pytest.mark.asyncio
    async def test_one_script_per_session(self, agent):
        server = make_server()

        async def slow_tool(params):
            await asyncio.sleep(0.3)
            return {"status": "ok"}

        agent.add_tool("slow_tool", lambda p: slow_tool(p))
        t1 = asyncio.create_task(
            run(server, agent, 'call_tool("slow_tool")\nresult = 1'))
        await asyncio.sleep(0.05)
        res2 = await run(server, agent, 'result = 2')
        res1 = await t1
        assert res1["status"] == "ok"
        assert res2["status"] == "error"
        assert "Another script" in res2["error"]

    @pytest.mark.asyncio
    async def test_different_sessions_run_independently(self, agent):
        server = make_server()
        r1, r2 = await asyncio.gather(
            server.run_script({"script": "result = 1", "_agent": agent,
                               "_session_id": "s1"}),
            server.run_script({"script": "result = 2", "_agent": agent,
                               "_session_id": "s2"}),
        )
        assert r1["result"] == 1 and r2["result"] == 2

    @pytest.mark.asyncio
    async def test_cancellation_between_hops(self, agent):
        token = MagicMock()
        token.is_cancelled = True
        server = make_server()
        res = await run(server, agent,
                        'call_tool("forum_post", content="x")\nresult = 1',
                        _cancellation_token=token)
        assert res["status"] == "error"
        assert "cancel" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_per_call_timeout(self, agent):
        async def hang(params):
            await asyncio.sleep(5)
            return {"status": "ok"}

        agent.add_tool("hang_tool", lambda p: hang(p))
        server = make_server(per_call_timeout=0.2)
        res = await run(server, agent, 'call_tool("hang_tool")\nresult = 1')
        assert res["status"] == "error"
        assert "timed out" in res["error"]

    def test_plain_data_validation(self):
        ToolScriptServer._ensure_plain_data({"a": [1, "x", None, {"b": True}]})
        with pytest.raises(ToolCallError, match="non-JSON"):
            ToolScriptServer._ensure_plain_data({"a": object()})
        with pytest.raises(ToolCallError, match="non-string key"):
            ToolScriptServer._ensure_plain_data({1: "x"})

    @pytest.mark.asyncio
    async def test_missing_agent_context(self):
        server = make_server()
        res = await server.run_script({"script": "result = 1"})
        assert res["status"] == "error"
        assert "_agent" in res["error"]


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------

class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_lists_targets(self, agent):
        server = make_server()
        res = await run(server, agent, (
            'call_tool("store_manage_json", operation="read", doc="d1")\n'
            'call_tool("forum_post", content="x")\n'
            'result = 1'
        ), dry_run=True)
        assert res["status"] == "ok" and res["dry_run"] is True
        assert res["call_tool_targets"] == ["store_manage_json", "forum_post"]
        assert agent.dispatched == []  # nothing executed

    @pytest.mark.asyncio
    async def test_dry_run_syntax_error(self, agent):
        server = make_server()
        res = await run(server, agent, 'x = (1', dry_run=True)
        assert res["status"] == "error"
        assert "line" in res["error"].lower()


# ---------------------------------------------------------------------------
# inject_params: server-seitige Secrets (write_key), nie LLM-typed
# ---------------------------------------------------------------------------


class TestInjectParams:
    KEY_SCHEMA = {
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "write_key": {"type": "string"},
        },
        "required": ["operation", "write_key"],
    }

    def _agent_with_keyed_tool(self):
        a = FakeAgent()
        a.add_tool(
            "writer_issues_op",
            lambda p: {"status": "ok", "seen_key": p.get("write_key")},
            self.KEY_SCHEMA,
        )
        a.add_tool(
            "other_tool", lambda p: {"status": "ok", "seen_key": p.get("write_key")},
            {"type": "object", "properties": {"write_key": {"type": "string"}}},
        )
        return a

    @pytest.mark.asyncio
    async def test_injects_omitted_required_param(self):
        # Script laesst write_key weg — Injection VOR Schema-Validierung
        agent = self._agent_with_keyed_tool()
        server = make_server(
            inject_params={"writer_issues_op": {"write_key": "SECRET_OK"}})
        res = await run(server, agent,
                        'result = call_tool("writer_issues_op", operation="x")')
        assert res["status"] == "ok"
        assert agent.dispatched[0][1]["write_key"] == "SECRET_OK"

    @pytest.mark.asyncio
    async def test_config_overrides_garbled_script_value(self):
        # Der v6-Befund: LLM tippt den Key transponiert — Config gewinnt
        agent = self._agent_with_keyed_tool()
        server = make_server(
            inject_params={"writer_issues_op": {"write_key": "SECRET_OK"}})
        res = await run(
            server, agent,
            'result = call_tool("writer_issues_op", operation="x", '
            'write_key="WC_x9K_mP_falsch")')
        assert res["status"] == "ok"
        assert agent.dispatched[0][1]["write_key"] == "SECRET_OK"

    @pytest.mark.asyncio
    async def test_non_matching_tool_not_injected(self):
        agent = self._agent_with_keyed_tool()
        server = make_server(
            inject_params={"writer_issues_op": {"write_key": "SECRET_OK"}})
        res = await run(server, agent, 'result = call_tool("other_tool")')
        assert res["status"] == "ok"
        assert "write_key" not in agent.dispatched[0][1]

    @pytest.mark.asyncio
    async def test_fnmatch_pattern(self):
        agent = self._agent_with_keyed_tool()
        server = make_server(
            inject_params={"writer_*": {"write_key": "SECRET_OK"}})
        res = await run(server, agent,
                        'result = call_tool("writer_issues_op", operation="x")')
        assert res["status"] == "ok"
        assert agent.dispatched[0][1]["write_key"] == "SECRET_OK"
