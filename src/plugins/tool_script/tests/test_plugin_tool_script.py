"""Tests for the tool_script plugin — scripted tool chains.

The fake agent implements the same contract as Agent.dispatch_tool_call /
_resolve_flat_tool_name (the real dispatch semantics are covered by
tests/agent/test_dispatch_tool_call.py — here we test the plugin layer:
sandbox bridging, error contract, caps, trace, resumability report).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict

import pytest
from unittest.mock import MagicMock

from agent_system.config.models import ToolServerConfig
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from plugins.tool_script.server import (
    ToolScriptServer, ToolCallError, _ScriptAbort, _ScriptContext)


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
        self.hook_sources = []        # (hook_source, cancellation_token) of each dispatch
        self.hook_arguments = []      # the params the tool hooks would see
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
                                 user_id=None, request_id=None, hook_source=None,
                                 cancellation_token=None, injected_params=None):
        # What the tool receives: the injected params go over the script's.
        self.dispatched.append((tool_name, {**params, **(injected_params or {})}, request_id))
        self.hook_sources.append((hook_source, cancellation_token))
        self.hook_arguments.append(params)
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
        ToolServerConfig(type="tool_script", enabled=True, config=config))


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

    @pytest.mark.asyncio
    async def test_script_calls_pass_the_tool_hooks(self, agent):
        """The model wrote the script: a call the pre_tool_call hooks would
        stop must not get past them inside one. They get the script's token,
        so a hook that waits for a person stops when the run is cancelled."""
        server = make_server()
        token = MagicMock(is_cancelled=False)
        await run(server, agent,
                  'call_tool("forum_post", content="a")\n'
                  'call_tool("forum_post", content="b")\n'
                  'result = 1',
                  _cancellation_token=token)
        assert agent.hook_sources == [("tool_script", token), ("tool_script", token)]


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
    async def test_failure_report_survives_the_tool_message(self, agent):
        # Live run 2026-09-11: a small set passed the snapshot's
        # json.dumps(default=str) check but was stored raw, so tool_execution's
        # json.dumps of the whole result raised, and the model saw "Object of
        # type set is not JSON serializable" instead of its own NameError.
        server = make_server()
        res = await run(server, agent, 'seen = {"B01", "B02"}\nkeynum')
        assert res["status"] == "error"
        assert "seen" in res["variables"], "no set in the snapshot -- nothing measured"
        message = json.dumps(res, ensure_ascii=False)  # what tool_execution sends
        assert "keynum" in json.loads(message)["error"]

    @pytest.mark.asyncio
    async def test_plain_json_values_reach_the_report_untouched(self, agent):
        # Only a value that needs default=str is round-tripped: a round-trip of
        # plain JSON merges an int and a str key into one entry.
        server = make_server()
        res = await run(server, agent, 'd = {1: "a", "1": "b"}\nkeynum')
        assert res["status"] == "error"
        message = json.dumps(res, ensure_ascii=False)
        assert '"1": "a"' in message and '"1": "b"' in message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("last_line,named", [
        ("keynum", "keynum"),
        ("result = x", "result"),
    ], ids=["script-error", "result-value"])
    async def test_a_deeply_nested_value_does_not_take_the_report_with_it(
            self, agent, last_line, named):
        # json.dumps raises RecursionError on it, which neither check caught:
        # it escaped run_script and the model got the recursion message
        # instead of its own error.
        #
        # 5000 levels no longer did that: Python 3.14 guards the C stack
        # instead of counting against the recursion limit, and json.dumps
        # serializes 100_000 levels (measured; 120_000 raise). 200_000 is
        # built ten levels per iteration: well inside the sandbox's per-loop
        # caps on iterations and on time (2 s, which a loop of 100_000 plain
        # iterations came within 2x of under a tracer). The fixture is checked
        # first -- on a Python that serializes this depth, the test says so
        # instead of measuring nothing.
        deep: list = []
        for _ in range(200_000):
            deep = [deep]
        with pytest.raises(RecursionError):
            json.dumps(deep)
        server = make_server()
        res = await run(server, agent, (
            "x = []\n"
            "for i in range(20000):\n"
            "    x = [[[[[[[[[[x]]]]]]]]]]\n" + last_line))
        assert res["status"] == "error"
        assert named in res["error"]
        # Estimated before any dump, the deep value is only named.
        assert "omitted" in res["variables"]["x"]
        json.dumps(res, ensure_ascii=False)

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
    async def test_a_cancel_while_a_call_waited_cannot_be_caught(self, agent):
        """A tool hook asked a person and the run was cancelled meanwhile:
        the script stops, as it does on a cancel between hops."""
        token = MagicMock(is_cancelled=False)

        async def cancelled_while_waiting(params):
            token.is_cancelled = True
            raise ToolDispatchError("Request cancelled — the call did not run.")

        agent.add_tool("slow_tool", cancelled_while_waiting,
                       {"type": "object", "properties": {}})
        server = make_server()
        res = await run(server, agent,
                        'try:\n'
                        '    call_tool("slow_tool")\n'
                        'except ToolCallError:\n'
                        '    pass\n'
                        'result = {"went_on": True}',
                        _cancellation_token=token)
        assert res["status"] == "error", res
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", ["except Exception:", "except:"],
                             ids=["except-Exception", "bare-except"])
    async def test_a_broad_except_cannot_swallow_the_call_cap(self, agent, handler):
        # The sandbox runs a script's `except Exception:` and bare `except:` as
        # a Python `except Exception`: an abort that is an Exception was
        # swallowed, and the script finished "ok".
        server = make_server(max_tool_calls=2)
        res = await run(server, agent, (
            'for i in [1, 2, 3, 4]:\n'
            '    try:\n'
            '        call_tool("forum_post", content="spam")\n'
            f'    {handler}\n'
            '        pass\n'
            'print("went on")\n'
            'result = "done"'
        ))
        assert res["status"] == "error", res
        assert "max_tool_calls" in res["error"]
        # The abort ends the script there; it does not run on to its end.
        assert "went on" not in res.get("output", "")

    @pytest.mark.asyncio
    async def test_a_broad_except_cannot_swallow_a_cancel(self, agent):
        token = MagicMock(is_cancelled=True)
        server = make_server()
        res = await run(server, agent, (
            'for i in [1, 2]:\n'
            '    try:\n'
            '        call_tool("forum_post", content="x")\n'
            '    except Exception:\n'
            '        pass\n'
            'result = "went on"'
        ), _cancellation_token=token)
        assert res["status"] == "error", res
        assert "cancel" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_a_loop_over_tool_calls_may_take_longer_than_two_seconds(self, agent):
        # The sandbox's own per-loop limit (2 s, for pure computation) cut a
        # loop over tool calls although the script's timeout was far away.
        async def slow(params):
            await asyncio.sleep(0.7)
            return {"status": "ok"}

        agent.add_tool("slow_tool", lambda p: slow(p), {"type": "object", "properties": {}})
        server = make_server()
        res = await run(server, agent,
                        'for i in range(4):\n    call_tool("slow_tool")\nresult = "done"')
        assert res["status"] == "ok", res
        assert len(res["calls"]) == 4

    @pytest.mark.asyncio
    async def test_log_is_capped_like_print(self, agent):
        # log() lines go back to the model in full; uncapped, a log() in a
        # loop returned 160,000 characters.
        server = make_server(max_output_length=1000)
        res = await run(server, agent,
                        'for i in range(100):\n    log("x" * 50)\nresult = 1')
        assert res["status"] == "error"
        assert "max_output_length" in res["error"]
        assert sum(len(line) + 1 for line in res["log"]) <= 1000

    @pytest.mark.asyncio
    async def test_error_texts_are_capped(self, agent):
        agent.add_tool("loud_tool", lambda p: {"status": "error", "error": "E" * 300_000})
        server = make_server()
        res = await run(server, agent, 'call_tool("loud_tool")')
        assert res["status"] == "error"
        assert len(res["error"]) < 21_000
        assert len(res["calls"][0]["error"]) < 600
        res = await run(server, agent, 'raise ValueError("x" * 100000)')
        assert len(res["error"]) < 21_000

    @pytest.mark.asyncio
    async def test_what_the_script_printed_is_in_the_failure_report(self, agent):
        server = make_server(max_tool_calls=1)
        res = await run(server, agent, 'print("before")\nd = {}\nd["missing"]')
        assert res["status"] == "error"
        assert res["output"] == "before"
        # An abort (here: the call cap) takes the same way out.
        res = await run(server, agent, (
            'print("step one")\n'
            'call_tool("forum_post", content="a")\n'
            'call_tool("forum_post", content="b")'))
        assert "max_tool_calls" in res["error"]
        assert res["output"] == "step one"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["soon", -5, "nan", "inf"])
    async def test_an_invalid_timeout_is_refused(self, agent, value):
        server = make_server()
        res = await run(server, agent, 'result = 1', timeout=value)
        assert res["status"] == "error"
        assert "'timeout'" in res["error"]

    @pytest.mark.asyncio
    async def test_a_finally_that_raises_cannot_replace_the_abort(self, agent):
        # Python lets a raise in `finally:` replace the exception in flight --
        # the abort went, the outer except caught the ValueError, "ok".
        server = make_server(max_tool_calls=1)
        res = await run(server, agent, (
            'try:\n'
            '    try:\n'
            '        call_tool("forum_post", content="a")\n'
            '        call_tool("forum_post", content="b")\n'
            '    finally:\n'
            '        raise ValueError("swap")\n'
            'except Exception:\n'
            '    pass\n'
            'result = "went on"'))
        assert res["status"] == "error", res
        assert "max_tool_calls" in res["error"]

    @pytest.mark.asyncio
    async def test_a_cancel_stops_a_script_that_only_computes(self, agent):
        # The cancel came while the call ran; the call returned normally and
        # the script computed on, with no further call to notice it.
        token = MagicMock(is_cancelled=False)

        def cancelling(params):
            token.is_cancelled = True
            return {"status": "ok"}

        agent.add_tool("cancelling", cancelling, {"type": "object", "properties": {}})
        server = make_server()
        res = await run(server, agent, (
            'call_tool("cancelling")\n'
            'n = 0\n'
            'for i in range(1000):\n'
            '    n += 1\n'
            'result = n'), _cancellation_token=token)
        assert res["status"] == "error", res
        assert "cancel" in res["error"].lower()

    @pytest.mark.asyncio
    async def test_a_cancelled_task_stops_its_worker_thread(self, agent):
        # The worker thread cannot be killed; cancelled, the task used to
        # leave it calling tools with nobody waiting for the answers.
        async def slow(params):
            await asyncio.sleep(0.2)
            return {"status": "ok"}

        agent.add_tool("slow_tool", lambda p: slow(p), {"type": "object", "properties": {}})
        server = make_server()
        task = asyncio.create_task(run(server, agent,
                                       'for i in range(8):\n    call_tool("slow_tool")\nresult = 1'))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.8)
        assert len(agent.dispatched) <= 3, agent.dispatched

    @pytest.mark.asyncio
    async def test_the_log_cap_cannot_be_caught(self, agent):
        server = make_server(max_output_length=1000)
        res = await run(server, agent, (
            'for i in range(100):\n'
            '    try:\n'
            '        log("x" * 50)\n'
            '    except Exception:\n'
            '        pass\n'
            'result = "went on"'))
        assert res["status"] == "error", res
        assert "max_output_length" in res["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("last_line,cap,message", [
        ('result = "y" * 500', 100, "too large"),            # caught by the estimate
        ("result = chr(0) * 50", 100, "too large"),          # JSON escapes it past the cap
        ("result = 10 ** 5000", 20000, "not JSON-serializable"),  # over 4300 digits
    ], ids=["too-large-estimated", "too-large-escaped", "not-serializable"])
    async def test_output_is_reported_when_the_result_fails(self, agent, last_line, cap, message):
        server = make_server(max_result_chars=cap)
        res = await run(server, agent, 'print("step one")\n' + last_line)
        assert res["status"] == "error"
        assert message in res["error"]
        assert res["output"] == "step one"

    @pytest.mark.asyncio
    async def test_a_dispatch_error_text_is_capped_in_the_trace(self, agent):
        def refuse(params):
            raise ToolDispatchError("D" * 300_000)

        agent.add_tool("refusing", refuse)
        server = make_server()
        res = await run(server, agent, 'call_tool("refusing")')
        assert res["status"] == "error"
        assert len(res["calls"][0]["error"]) < 600

    @pytest.mark.asyncio
    async def test_a_tool_the_agent_may_not_call_is_refused_before_its_schema(self, agent):
        # The schema lookup falls back to every registered server: a tool the
        # agent may not call answered with its parameter list.
        agent.tool_dispatch_denial = lambda tool, server: (
            f"Tool '{tool}' is not in this agent's allowed tools." if tool == "forum_post" else None)
        server = make_server()
        res = await run(server, agent, 'call_tool("forum_post", bogus=1)')
        assert res["status"] == "error"
        assert "not in this agent's allowed tools" in res["error"]
        assert "Valid parameters" not in res["error"]
        assert agent.dispatched == []

    @pytest.mark.asyncio
    async def test_an_external_mcp_tool_is_named_as_such(self, agent):
        # The model's list shows an MCP tool with "_" for the dot; there is
        # no plugin server behind the name.
        agent._current_tools_schema = [
            {"type": "function", "function": {"name": "web_search", "parameters": {}}}]
        server = make_server()
        res = await run(server, agent, 'call_tool("web_search")')
        assert res["status"] == "error"
        assert "external MCP tool" in res["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("script", [
        "b = 10 ** 4000\nresult = [b] * 20000",
        "b = 10 ** 4000\nbig = [b] * 20000\nkeynum",
        "b = 10 ** 4000\nlog([b] * 20000)",
    ], ids=["result", "snapshot", "log"])
    async def test_a_big_value_is_estimated_not_dumped(self, agent, script):
        # The result and the variables snapshot are dumped on the event loop:
        # dumping ~80 MB first stood it still for about a second. str() in
        # log() is the same long call in the worker.
        server = make_server()
        t = time.perf_counter()
        res = await run(server, agent, script)
        assert res["status"] == "error"
        assert time.perf_counter() - t < 1.0, f"took {time.perf_counter() - t:.2f}s"

    @pytest.mark.asyncio
    async def test_the_exact_length_decides_near_the_limit(self, agent):
        # [999] * 4000 is exactly 20,000 JSON characters; the estimate says
        # more. An estimate alone refused realistic results (900 prices).
        server = make_server()
        res = await run(server, agent, "result = [999] * 4000")
        assert res["status"] == "ok", res.get("error")
        # [999] * 40 is exactly 200 characters: shown in the snapshot, not hidden.
        res = await run(server, agent, "v = [999] * 40\nkeynum")
        assert res["variables"]["v"] == [999] * 40

    @pytest.mark.asyncio
    async def test_no_call_goes_out_after_the_task_was_cancelled(self, agent, monkeypatch):
        # The worker checks the stop flag, then validates, then dispatches: a
        # cancel between the check and the dispatch let the call out.
        server = make_server()
        slow = server._validate_against_tool_schema

        def slow_validate(*args, **kwargs):
            time.sleep(0.3)
            return slow(*args, **kwargs)

        monkeypatch.setattr(server, "_validate_against_tool_schema", slow_validate)
        task = asyncio.create_task(run(server, agent, 'call_tool("forum_post", content="a")\nresult = 1'))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)
        assert agent.dispatched == []

    def test_the_deadline_message_names_the_timeout_of_the_run(self, agent):
        server = make_server()  # config timeout 120
        call_tool = server._make_call_tool(
            ctx=_ScriptContext(), agent=agent, loop=MagicMock(), deadline=0.0,
            timeout=5.0, session_id=None, user_id=None, request_id=None,
            cancellation_token=None, status=None)
        with pytest.raises(_ScriptAbort, match=r"\(5s\)"):
            call_tool("forum_post", content="x")

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
        # A secret is merged after the tool hooks: none of them logs it or
        # shows it to a person.
        assert "write_key" not in agent.hook_arguments[0]

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
