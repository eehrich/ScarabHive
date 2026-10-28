"""Tool bindings: a run's tool calls held to values its caller fixed (servers/agent/components/tool_bindings.py).

Through the real path -- Agent.run_events, the tool execution manager, dispatch_tool_call -- against a real tool
server; only the LLM is scripted. What is pinned:

* a bound parameter is put in where the model left it out, and a call that asks for another value does not run;
* a tool a pattern names that does not take the parameter does not run (the binding would hold nothing);
* a tool no pattern names is not touched;
* the bindings are the run's: none in a run without, its caller's back after it;
* dispatch_tool_call (scripts, hooks, the MCP gateway) is held to them as well;
* AgentCaller hands them to the sub-agent manager on create, follow-up and the schema feedback.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.core.request_context import current_tool_bindings
from agent_system.servers.agent.components.tool_bindings import form_problem, hold
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServer, ToolServerRegistry


class _Files(ToolServer):
    """A real tool server: write takes a path, note takes none."""

    def __init__(self):
        super().__init__("files", AgentSystemConfig(), ToolServerConfig(type="files", enabled=True))
        self.received: list[tuple[str, dict[str, Any]]] = []

    def get_tools(self):
        return [
            {"type": "function", "function": {"name": "files_write", "description": "Write a file.", "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "files_note", "description": "A note.", "parameters": {
                "type": "object", "properties": {"text": {"type": "string"}}}}},
        ]

    async def files_write(self, params):
        self.received.append(("write", dict(params)))
        return {"status": "ok"}

    async def files_note(self, params):
        self.received.append(("note", dict(params)))
        return {"status": "ok"}


def _call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class _Model:
    """One round of tool calls, then an answer; records the bindings it saw and the tool results."""

    model = "test/model"

    def __init__(self, *calls: dict[str, Any]):
        self._calls = list(calls)
        self.requests: list[list[Any]] = []
        self.bindings: list[Any] = []

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.requests.append(list(messages))
        self.bindings.append(current_tool_bindings.get())
        if len(self.requests) == 1 and self._calls:
            yield {"type": "final", "assistant": {"role": "assistant", "content": None, "tool_calls": self._calls}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}

    def tool_results(self) -> dict[str, dict[str, Any]]:
        return {m.tool_call_id: json.loads(m.content) for m in self.requests[1] if m.role == "tool"}


def _agent(files: _Files) -> Agent:
    llm_system = LLMSystemConfig(
        models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
        profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    registry = ToolServerRegistry()
    registry.register("files", files)
    agent_config = AgentConfig(max_steps=4, llm_profile="normal", tools=ToolConfig(allowed=["files/*"]))
    return Agent("bound_agent", AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), registry)


BINDINGS = {"files/files_write": {"path": "out.md"}}


async def _run(*calls, bindings=BINDINGS):
    files = _Files()
    agent = _agent(files)
    model = _Model(*calls)
    agent.llm = model
    [event async for event in agent.run_events("go", session_id="bound-1", tool_bindings=bindings)]
    return files, model


class TestTheModelsCalls:
    async def test_a_bound_value_goes_in_and_another_one_does_not_run(self):
        files, model = await _run(_call("c1", "files_write", text="a"),
                                  _call("c2", "files_write", text="b", path="out.md"),
                                  _call("c3", "files_write", text="c", path="../other.md"))
        assert [(n, p["path"], p["text"]) for n, p in files.received] == [("write", "out.md", "a"),
                                                                          ("write", "out.md", "b")]
        refused = model.tool_results()["c3"]
        assert refused["status"] == "error" and "path is fixed to 'out.md'" in refused["error"]

    async def test_a_named_tool_without_the_parameter_does_not_run_and_others_are_free(self):
        files, model = await _run(_call("c1", "files_note", text="named"), _call("c2", "files_note", text="free"),
                                  bindings={"files/*": {"path": "out.md"}})
        assert files.received == []
        assert "does not take path" in model.tool_results()["c1"]["error"]
        files, _ = await _run(_call("c1", "files_note", text="free"))
        assert [n for n, _ in files.received] == ["note"], "a tool no pattern names was held"

    async def test_the_bare_tool_name_binds_as_the_full_path_does(self):
        """A gateway's `tools` name tools bare; a binding written alike must hold, not pass as a server name."""
        files, model = await _run(_call("c1", "files_write", text="a"),
                                  _call("c2", "files_write", text="b", path="../other.md"),
                                  bindings={"files_write": {"path": "out.md"}})
        assert [(n, p["path"]) for n, p in files.received] == [("write", "out.md")]
        assert "path is fixed to 'out.md'" in model.tool_results()["c2"]["error"]

    async def test_two_patterns_that_disagree_hold_nothing(self):
        files, model = await _run(_call("c1", "files_write", text="a"),
                                  bindings={"files/*": {"path": "a.md"}, "*_write": {"path": "b.md"}})
        assert files.received == [] and "both 'a.md' and 'b.md'" in model.tool_results()["c1"]["error"]


class TestHooksAndExternals:
    async def test_a_pre_tool_call_hook_cannot_rewrite_a_bound_value(self):
        from agent_system.hooks import HookResult, HookType, PluginHook
        from agent_system.hooks.registry import get_hook_registry

        class _Redirect(PluginHook):
            async def on_pre_tool_call(self, context):
                context.tool_call["arguments"] = {**context.tool_call["arguments"], "path": "hooked.md"}
                return HookResult(success=True, modified=True, context=context)

        registry = get_hook_registry()
        await registry.register_hook(HookType.PRE_TOOL_CALL, "bind.redirect", _Redirect("bind.redirect"))
        try:
            files, model = await _run(_call("c1", "files_write", text="a"))
            assert files.received == [] and "path is fixed" in model.tool_results()["c1"]["error"]
            agent = _agent(files)
            token = current_tool_bindings.set(BINDINGS)
            try:
                with pytest.raises(ToolDispatchError, match="path is fixed"):
                    await agent.dispatch_tool_call("files_write", {"text": "b"}, hook_source="tool_script")
            finally:
                current_tool_bindings.reset(token)
        finally:
            await registry.unregister_hook(HookType.PRE_TOOL_CALL, "bind.redirect")
        assert files.received == []

    @pytest.mark.parametrize("pattern", ["ext.*", "ext.write_file", "*write_file"])
    async def test_an_external_tool_a_pattern_names_does_not_run(self, pattern):
        """Matched as the allowlist matches it: by the dotted name as its server, the sanitized one as its name."""
        agent = _agent(_Files())
        invoker = agent._tool_execution_manager._invoker
        token = current_tool_bindings.set({pattern: {"path": "out.md"}})
        try:
            message, events, _ = await invoker.execute_tool(
                {"id": "c1"}, "ext.write_file", "ext_write_file", {"path": "/elsewhere"}, 1, "req-1", "bound-1")
        finally:
            current_tool_bindings.reset(token)
        assert "an external tool cannot be held" in json.loads(message.content)["error"]
        assert events[0]["type"] == "tool_error"


class TestTheRunsOwn:
    async def test_the_model_call_sees_the_runs_bindings_and_the_caller_has_its_own_back(self):
        token = current_tool_bindings.set({"outer/*": {"x": 1}})
        try:
            _, model = await _run()
            assert model.bindings == [BINDINGS]
            assert current_tool_bindings.get() == {"outer/*": {"x": 1}}
            _, model = await _run(bindings=None)
            assert model.bindings == [None], "a run without bindings took over its caller's"
        finally:
            current_tool_bindings.reset(token)

    async def test_a_stream_closed_early_gives_the_callers_back(self):
        files = _Files()
        agent = _agent(files)
        agent.llm = _Model()
        token = current_tool_bindings.set({"outer/*": {"x": 1}})
        try:
            events = agent.run_events("go", session_id="bound-2", tool_bindings=BINDINGS)
            async for _ in events:
                if agent.llm.bindings:
                    break
            await events.aclose()
            assert agent.llm.bindings == [BINDINGS], "the model was not reached: this test would prove nothing"
            assert current_tool_bindings.get() == {"outer/*": {"x": 1}}
        finally:
            current_tool_bindings.reset(token)

    async def test_a_consumer_that_stops_at_end_without_closing_has_the_callers_back(self):
        files = _Files()
        agent = _agent(files)
        agent.llm = _Model()
        token = current_tool_bindings.set({"outer/*": {"x": 1}})
        events = agent.run_events("go", session_id="bound-3", tool_bindings=BINDINGS)
        try:
            async for event in events:
                if event.get("type") == "end":
                    break
            assert current_tool_bindings.get() == {"outer/*": {"x": 1}}
        finally:
            current_tool_bindings.reset(token)
            await events.aclose()

    async def test_a_broken_form_does_not_start_the_run(self):
        files = _Files()
        agent = _agent(files)
        agent.llm = _Model()
        with pytest.raises(ValueError, match="cannot be bound"):
            [event async for event in agent.run_events("go", tool_bindings={"files/*": {"_session_id": "x"}})]

    @pytest.mark.parametrize("bindings,words", [
        ([], "mapping"), ({"": {"a": 1}}, "no tool pattern"), ({"files/*": {}}, "map parameters"),
        ({"files/*": {"request_id": "r"}}, "cannot be bound"), ({"files/*": {"path": ["a"]}}, "string, number")])
    def test_form(self, bindings, words):
        assert words in form_problem(bindings)

    def test_unbound_and_matching_calls(self):
        assert hold(None, "files_write", "files", {"text": "a"}, {"text"}) == ({"text": "a"}, None)
        assert hold(BINDINGS, "files_write", "files", {}, {"path"}) == ({"path": "out.md"}, None)
        assert "not callable" in hold(BINDINGS, "files_write", "files", {}, None)[1]


class TestDispatch:
    async def test_dispatch_is_held_to_the_bindings_in_the_context(self):
        files = _Files()
        agent = _agent(files)
        token = current_tool_bindings.set(BINDINGS)
        try:
            await agent.dispatch_tool_call("files_write", {"text": "a"}, session_id="bound-1")
            with pytest.raises(ToolDispatchError, match="path is fixed"):
                await agent.dispatch_tool_call("files_write", {"text": "b", "path": "x.md"}, session_id="bound-1")
            # injected values come before the binding, and cannot ask for another value either
            with pytest.raises(ToolDispatchError, match="path is fixed"):
                await agent.dispatch_tool_call("files_write", {"text": "c"}, session_id="bound-1",
                                               injected_params={"path": "y.md"})
        finally:
            current_tool_bindings.reset(token)
        await agent.dispatch_tool_call("files_write", {"text": "d", "path": "free.md"}, session_id="bound-1")
        assert [p["path"] for _, p in files.received] == ["out.md", "free.md"]


class TestAgentCaller:
    async def test_create_follow_up_and_schema_feedback_carry_the_bindings(self):
        from pydantic import BaseModel

        from agent_system.core.agent_caller import AgentCaller

        class Out(BaseModel):
            n: int

        sent: list[dict[str, Any]] = []
        answers = iter(['{"wrong": 1}', '{"n": 2}', '{"n": 3}'])

        class _Sam:
            async def call_tool(self, tool_name, params):
                sent.append(dict(params))
                return {"status": "success", "instance_id": "i1", "result": next(answers)}

        caller = AgentCaller(_Sam(), sam_instance="sam", session_id="s", user_id="u", request_id="r", retries=0)
        assert (await caller.call("worker", "go", schema=Out, tool_bindings=BINDINGS)).n == 2
        assert (await caller.follow_up("i1", "more", schema=Out, tool_bindings=BINDINGS)).n == 3
        assert [(p["operation"], p.get("_tool_bindings")) for p in sent] == [
            ("create", BINDINGS), ("continue", BINDINGS), ("continue", BINDINGS)]
        answers = iter(["free"])
        await caller.call_text("worker", "go")
        assert "_tool_bindings" not in sent[-1], "a call without bindings sent some"
