"""pre_tool_call / post_tool_call: hooks around every tool call the model makes.

Everything runs through the real path -- Agent.run_events, the tool execution
manager, the hook manager, the registry -- against a real tool server; only the
LLM is scripted. What is pinned:

* pre may change the arguments the tool runs with, or block the call: a blocked
  call does not run, the model reads why as an error result, the run goes on;
* the first hook that blocks ends the chain;
* post may change the result before it joins the history;
* the history keeps what the model sent, and every request is a prefix of the
  next;
* an agent no hook runs for pays nothing;
* the runtime params stay the framework's;
* tool_script's calls (Agent.dispatch_tool_call with a hook_source) pass the
  same hooks, other in-process callers do not.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable
from types import SimpleNamespace
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
from agent_system.hooks import HookResult, HooksConfig, HookType, PluginHook, SchemaBasedPluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.servers.agent.components.tool_execution import ToolDispatchError
from agent_system.servers.agent.escalation import StuckEscalator
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServer, ToolServerRegistry
from agent_system.tools.status import get_status_bus, status_scope


class _Probe(ToolServer):
    """A real tool server with one tool; records what each call received."""

    def __init__(self):
        super().__init__("probe", AgentSystemConfig(), ToolServerConfig(type="probe", enabled=True))
        self.received: list[dict[str, Any]] = []

    def get_tools(self):
        return [{"type": "function", "function": {
            "name": "probe_echo", "description": "Echo the text.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}]

    async def probe_echo(self, params):
        self.received.append(dict(params))
        if params.get("text") == "boom":
            raise RuntimeError("probe broke: secret-123")
        if params.get("text") == "fail":
            return {"status": "error", "error": "probe says no"}
        if params.get("text") == "slow":
            await asyncio.sleep(0.3)
        return {"status": "ok", "echo": params.get("text")}


def _call(call_id: str, text: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function",
            "function": {"name": "probe_echo", "arguments": json.dumps({"text": text})}}


class _Model:
    """Asks for one round of tool calls per entry of ``rounds``, then answers;
    records every request."""

    model = "test/model"

    def __init__(self, *rounds: list[dict[str, Any]]):
        self._rounds = rounds
        self.requests: list[list[dict[str, Any]]] = []

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.requests.append([{"role": m.role, "content": m.content, "tool_call_id": m.tool_call_id,
                               "tool_calls": m.tool_calls} for m in messages])
        if len(self.requests) <= len(self._rounds):
            yield {"type": "final", "assistant": {"role": "assistant", "content": None,
                                                  "tool_calls": self._rounds[len(self.requests) - 1]}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}

    def tool_results(self, request: int = 1) -> list[dict[str, Any]]:
        return [{"id": m["tool_call_id"], **json.loads(m["content"])}
                for m in self.requests[request] if m["role"] == "tool"]


def _agent(probe: _Probe) -> Agent:
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
    registry = ToolServerRegistry()
    registry.register("probe", probe)
    agent_config = AgentConfig(max_steps=5, llm_profile="normal", tools=ToolConfig(allowed=["probe/*"]))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), registry)


async def _run(*rounds: list[dict[str, Any]]):
    probe = _Probe()
    agent = _agent(probe)
    model = _Model(*rounds)
    agent.llm = model
    events = [event async for event in agent.run_events("go", session_id="hooks-1")]
    return probe, model, events


class _Hook(PluginHook):
    """Runs the given functions as its tool hooks; records every context, and
    the call and result as they came in (the functions may change them)."""

    def __init__(self, pre: Callable | None = None, post: Callable | None = None):
        super().__init__("probe_hook")
        self._pre, self._post = pre, post
        self.pre_contexts: list[Any] = []
        self.pre_calls: list[dict[str, Any]] = []
        self.post_contexts: list[Any] = []
        self.post_results: list[dict[str, Any]] = []

    async def on_pre_tool_call(self, context):
        self.pre_contexts.append(context)
        self.pre_calls.append(copy.deepcopy(context.tool_call))
        return self._pre(context) if self._pre else HookResult(success=True, context=context)

    async def on_post_tool_call(self, context):
        self.post_contexts.append(context)
        self.post_results.append(copy.deepcopy(context.tool_result))
        return self._post(context) if self._post else HookResult(success=True, context=context)


@pytest.fixture
async def hooks():
    """register(name, hook, types=..., **kwargs); everything is unregistered after the test."""
    registry = get_hook_registry()
    registered = []

    async def register(name, hook, types=(HookType.PRE_TOOL_CALL, HookType.POST_TOOL_CALL), **kwargs):
        for hook_type in types:
            await registry.register_hook(hook_type, name, hook, **kwargs)
            registered.append((hook_type, name))

    yield register
    for hook_type, name in registered:
        await registry.unregister_hook(hook_type, name)


def _block_second(context):
    if context.tool_call["arguments"].get("text") == "two":
        return HookResult(success=True, metadata={"block": "No second echo. Answer with what you have."})
    return HookResult(success=True, context=context)


def _shout(context):
    context.tool_call["arguments"] = {**context.tool_call["arguments"],
                                      "text": context.tool_call["arguments"]["text"].upper()}
    return HookResult(success=True, modified=True, context=context)


def _stamp(context):
    context.tool_result["result"] = {**context.tool_result["result"], "checked": True}
    return HookResult(success=True, modified=True, context=context)


def _received_texts(probe: _Probe) -> list[str]:
    return [params.get("text") for params in probe.received]


class TestPreToolCall:

    async def test_a_pre_hook_changes_the_arguments_the_tool_runs_with(self, hooks):
        await hooks("tch.shout", _Hook(pre=_shout))

        probe, model, _ = await _run([_call("call_1", "one")])

        assert _received_texts(probe) == ["ONE"]
        assert model.tool_results() == [{"id": "call_1", "status": "ok", "echo": "ONE"}]
        # The history keeps what the model sent: rewriting its call would
        # change a message the next request repeats.
        assistant = [m for m in model.requests[1] if m["role"] == "assistant"][-1]
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"text": "one"}

    async def test_a_blocked_call_does_not_run_and_the_model_reads_why(self, hooks):
        await hooks("tch.guard", _Hook(pre=_block_second))

        probe, model, events = await _run([_call("call_1", "one"), _call("call_2", "two"),
                                           _call("call_3", "three")])

        assert _received_texts(probe) == ["one", "three"], "the blocked call ran, or blocked the others"
        assert model.tool_results() == [
            {"id": "call_1", "status": "ok", "echo": "one"},
            {"id": "call_2", "status": "error", "error": "No second echo. Answer with what you have.",
             "type": "ToolCallBlocked"},
            {"id": "call_3", "status": "ok", "echo": "three"},
        ], "a result is missing or out of call order"
        assert [e for e in events if e.get("blocked")] == [
            {"type": "tool_error", "tool": "probe", "error": "No second echo. Answer with what you have.",
             "blocked": True}]
        assert events[-1]["type"] == "end" and any(e.get("type") == "final" for e in events), \
            "a blocked call ended the run"

    async def test_a_block_without_a_text_gets_one(self, hooks):
        await hooks("tch.guard", _Hook(pre=lambda ctx: HookResult(success=True, metadata={"block": True})))

        probe, model, _ = await _run([_call("call_1", "one")])

        assert probe.received == []
        [result] = model.tool_results()
        assert result["status"] == "error" and "'probe_echo' was blocked before it ran" in result["error"]

    async def test_the_first_hook_that_blocks_ends_the_chain(self, hooks):
        later = _Hook()
        await hooks("tch.a_guard", _Hook(pre=_block_second), types=(HookType.PRE_TOOL_CALL,))
        await hooks("tch.b_later", later, types=(HookType.PRE_TOOL_CALL,),
                    order_spec={"after": ["tch.a_guard"]})

        probe, _, _ = await _run([_call("call_1", "one"), _call("call_2", "two")])

        assert [c["arguments"]["text"] for c in later.pre_calls] == ["one"], \
            "a hook after the block ran for the blocked call"
        assert _received_texts(probe) == ["one"]

    async def test_the_hook_sees_the_call(self, hooks):
        seen = _Hook()
        await hooks("tch.seen", seen)

        await _run([_call("call_1", "one")])

        [pre] = seen.pre_contexts
        assert seen.pre_calls == [{"id": "call_1", "name": "probe_echo", "server": "probe",
                                   "arguments": {"text": "one"}, "source": "model"}]
        assert pre.session_id == "hooks-1" and pre.request_id and pre.agent_name == "test_agent"
        assert pre.cancellation_token is not None, "a hook that waits could not stop on a cancel"
        [post] = seen.post_contexts
        assert post.tool_call == seen.pre_calls[0]
        [result] = seen.post_results
        assert {k: result[k] for k in ("result", "is_error")} == \
            {"result": {"status": "ok", "echo": "one"}, "is_error": False}
        assert set(result) == {"result", "is_error", "started_at", "finished_at"}

    async def test_a_hook_cannot_hand_the_tool_runtime_params(self, hooks):
        """Keys the framework sets anyway it overwrites; one it leaves unset
        (here: a key it never sets) would reach the tool as the framework's."""
        def forge(context):
            context.tool_call["arguments"] = {"text": "one", "_on_behalf_of": "mallory",
                                              "request_id": "forged"}
            return HookResult(success=True, modified=True, context=context)

        await hooks("tch.forge", _Hook(pre=forge))

        probe, _, _ = await _run([_call("call_1", "one")])

        [params] = probe.received
        assert params["text"] == "one"
        assert "_on_behalf_of" not in params, "a hook handed the tool a runtime param"

    async def test_a_failing_hook_is_skipped_and_the_call_runs(self, hooks):
        def broken(context):
            raise RuntimeError("hook broke")

        await hooks("tch.broken", _Hook(pre=broken))

        probe, model, _ = await _run([_call("call_1", "one")])

        assert _received_texts(probe) == ["one"]
        assert model.tool_results()[0]["status"] == "ok"

    async def test_when_the_hooks_cannot_be_asked_the_call_does_not_run(self, hooks, monkeypatch):
        await hooks("tch.seen", _Hook(), types=(HookType.PRE_TOOL_CALL,))
        probe = _Probe()
        agent = _agent(probe)
        agent.llm = model = _Model([_call("call_1", "one")])

        async def fails(*args, **kwargs):
            raise RuntimeError("machinery broke")

        monkeypatch.setattr(agent._hook_manager, "execute_pre_tool_hooks", fails)
        events = [event async for event in agent.run_events("go", session_id="hooks-1")]

        assert probe.received == []
        [result] = model.tool_results()
        assert result["status"] == "error" and "did not run" in result["error"]
        assert any(e.get("type") == "final" for e in events), "the failure ended the run"


class TestPostToolCall:

    async def test_a_post_hook_changes_the_result_the_model_reads(self, hooks):
        await hooks("tch.stamp", _Hook(post=_stamp))

        probe, model, _ = await _run([_call("call_1", "one"), _call("call_2", "two")])

        assert _received_texts(probe) == ["one", "two"]
        assert model.tool_results() == [
            {"id": "call_1", "status": "ok", "echo": "one", "checked": True},
            {"id": "call_2", "status": "ok", "echo": "two", "checked": True},
        ]

    async def test_a_blocked_call_reaches_no_post_hook(self, hooks):
        seen = _Hook()
        await hooks("tch.guard", _Hook(pre=_block_second), types=(HookType.PRE_TOOL_CALL,))
        await hooks("tch.seen", seen, types=(HookType.POST_TOOL_CALL,))

        await _run([_call("call_1", "one"), _call("call_2", "two")])

        assert [c.tool_call["id"] for c in seen.post_contexts] == ["call_1"]

    async def test_every_request_is_a_prefix_of_the_next(self, hooks):
        def shout_or_block(context):
            if context.tool_call["arguments"].get("text") == "two":
                return _block_second(context)
            return _shout(context)

        await hooks("tch.all", _Hook(pre=shout_or_block, post=_stamp))

        _, model, _ = await _run([_call("call_1", "one"), _call("call_2", "two")],
                                 [_call("call_3", "three"), _call("call_4", "two")])

        assert len(model.requests) == 3, "fixture: two rounds of tool calls, then the answer"
        for i, (request, following) in enumerate(zip(model.requests, model.requests[1:])):
            assert following[:len(request)] == request, f"request {i + 2} does not start with request {i + 1}"
        assert [r["id"] for r in model.tool_results(2)] == ["call_1", "call_2", "call_3", "call_4"]


class TestCost:

    async def test_an_agent_no_tool_hook_runs_for_builds_no_context(self, hooks, monkeypatch):
        await hooks("tch.off", _Hook(pre=_shout, post=_stamp), enabled=False)
        probe = _Probe()
        agent = _agent(probe)
        agent.llm = model = _Model([_call("call_1", "one")])

        async def must_not_run(*args, **kwargs):
            raise AssertionError("a tool hook context was built for an agent no hook runs for")

        built = []

        async def records(*args, **kwargs):
            built.append(args)
            raise RuntimeError("a context was built")

        monkeypatch.setattr(agent._hook_manager, "execute_pre_tool_hooks", records)
        monkeypatch.setattr(agent._hook_manager, "execute_post_tool_hooks", records)
        [event async for event in agent.run_events("go", session_id="hooks-1")]

        assert built == [], "a tool hook context was built for an agent no hook runs for"
        assert _received_texts(probe) == ["one"]
        assert model.tool_results() == [{"id": "call_1", "status": "ok", "echo": "one"}]


class TestScriptedCalls:
    """Agent.dispatch_tool_call: tool_script passes hook_source, the others do not."""

    async def test_a_script_call_passes_the_hooks(self, hooks):
        seen = _Hook(pre=_shout, post=_stamp)
        await hooks("tch.seen", seen)
        probe = _Probe()
        agent = _agent(probe)

        token = object()
        result = await agent.dispatch_tool_call("probe_echo", {"text": "one"}, session_id="hooks-1",
                                                hook_source="tool_script", cancellation_token=token)

        assert _received_texts(probe) == ["ONE"]
        assert result == {"status": "ok", "echo": "ONE", "checked": True}
        assert seen.pre_calls == [{"id": None, "name": "probe_echo", "server": "probe",
                                   "arguments": {"text": "one"}, "source": "tool_script"}]
        assert seen.pre_contexts[0].cancellation_token is token
        assert seen.post_contexts[0].cancellation_token is token
        assert seen.post_contexts[0].tool_call["arguments"] == {"text": "ONE"}, \
            "the post hook saw the call as the model sent it, not as it ran"

    async def test_a_hook_cannot_forge_whose_script_call_it_is(self, hooks):
        """A script call may run without a session -- nothing then overwrites a
        forged _session_id, and a tool would take the call for another agent's."""
        def forge(context):
            context.tool_call["arguments"] = {**context.tool_call["arguments"], "_session_id": "someone-else"}
            return HookResult(success=True, modified=True, context=context)

        await hooks("tch.forge", _Hook(pre=forge))
        probe = _Probe()
        agent = _agent(probe)

        await agent.dispatch_tool_call("probe_echo", {"text": "one"}, hook_source="tool_script")

        [params] = probe.received
        assert params["text"] == "one" and "_session_id" not in params

    async def test_a_blocked_script_call_raises_the_reason(self, hooks):
        await hooks("tch.guard", _Hook(pre=_block_second))
        probe = _Probe()
        agent = _agent(probe)

        with pytest.raises(ToolDispatchError, match="No second echo"):
            await agent.dispatch_tool_call("probe_echo", {"text": "two"}, hook_source="tool_script")
        assert probe.received == []

    async def test_a_call_without_a_hook_source_passes_no_hook(self, hooks):
        seen = _Hook(pre=_block_second)
        await hooks("tch.seen", seen)
        probe = _Probe()
        agent = _agent(probe)

        result = await agent.dispatch_tool_call("probe_echo", {"text": "two"})

        assert result == {"status": "ok", "echo": "two"}
        assert seen.pre_contexts == [] and seen.post_contexts == []


class _SchemaGuard(SchemaBasedPluginHook):
    """A hook plugin the way plugins write one: schema.yaml names the hook,
    the method of the same name handles it."""

    async def deny_second(self, context):
        return _block_second(context)


class TestThePluginRoute:

    async def test_a_schema_plugin_registered_by_discovery_blocks_a_call(self, tmp_path):
        (tmp_path / "schema.yaml").write_text(
            "hooks:\n  - name: deny_second\n    type: pre_tool_call\n    timeout: 5\n", encoding="utf-8")
        plugin = _SchemaGuard(tmp_path)
        registry = get_hook_registry()
        names = await register_plugin_hooks("tch_schema", plugin, {"hooks": plugin._hooks},
                                            registry, HooksConfig())
        try:
            probe, model, _ = await _run([_call("call_1", "one"), _call("call_2", "two")])
        finally:
            for name in names:
                await registry.unregister_hook(HookType.PRE_TOOL_CALL, name)

        assert names == ["tch_schema.deny_second"]
        assert _received_texts(probe) == ["one"]
        assert model.tool_results()[1]["error"] == "No second echo. Answer with what you have."


class _RecordingEscalator(StuckEscalator):
    """Records every stuck signal; opens no window."""

    def __init__(self):
        super().__init__(enabled=True, rounds=2, max_calls=6)
        self.reasons: list[str] = []

    def trigger(self, reason: str):
        self.reasons.append(reason)


class TestReviewFindings:

    async def test_the_post_hook_sees_the_call_as_it_ran(self, hooks):
        seen = _Hook()
        await hooks("tch.shout", _Hook(pre=_shout), types=(HookType.PRE_TOOL_CALL,))
        await hooks("tch.seen", seen, types=(HookType.POST_TOOL_CALL,))

        await _run([_call("call_1", "one")])

        assert [c.tool_call["arguments"] for c in seen.post_contexts] == [{"text": "ONE"}]

    async def test_a_post_hook_sees_a_failed_call_as_an_error(self, hooks):
        seen = _Hook()
        await hooks("tch.seen", seen, types=(HookType.POST_TOOL_CALL,))

        await _run([_call("call_1", "fail"), _call("call_2", "boom")])

        [failed, raised] = seen.post_results
        assert {k: failed[k] for k in ("result", "is_error")} == \
            {"result": {"status": "error", "error": "probe says no"}, "is_error": True}
        assert raised["is_error"] is True and "probe broke" in json.dumps(raised["result"]), \
            "a call whose tool raised did not reach the post hooks"

    async def test_a_waiting_pre_hook_reaches_the_stream_while_it_waits(self, hooks):
        """A hook that asks a person over the run's own stream: the question
        must reach the viewer before the answer, not with it."""
        released = asyncio.Event()

        class _Asking(PluginHook):
            async def on_pre_tool_call(self, context):
                async with status_scope(get_status_bus(), "approval", request_id=context.request_id) as status:
                    await status.progress("Approve probe_echo?")
                    try:
                        await asyncio.wait_for(released.wait(), timeout=3)
                    except TimeoutError:
                        return HookResult(success=True, metadata={"block": "Nobody answered."})
                return HookResult(success=True, context=context)

        await hooks("tch.ask", _Asking("ask"), types=(HookType.PRE_TOOL_CALL,))
        probe = _Probe()
        agent = _agent(probe)
        agent.llm = _Model([_call("call_1", "one")])

        async for event in agent.run_events("go", session_id="hooks-1"):
            if "Approve probe_echo?" in json.dumps(event, default=str):
                released.set()

        assert _received_texts(probe) == ["one"], "the question reached the stream only after the hook gave up"

    async def test_a_post_result_that_cannot_be_encoded_leaves_the_result(self, hooks):
        def unencodable(context):
            context.tool_result["result"] = {("a", "b"): 1}
            return HookResult(success=True, modified=True, context=context)

        await hooks("tch.bad", _Hook(post=unencodable), types=(HookType.POST_TOOL_CALL,))

        _, model, events = await _run([_call("call_1", "one")])

        assert model.tool_results() == [{"id": "call_1", "status": "ok", "echo": "one"}]
        assert any(e.get("type") == "final" for e in events), "the hook's result ended the run"

    async def test_answers_keep_the_order_of_the_calls(self, hooks):
        """Calls the framework rejects are answered in their place too:
        Gemini wants the answers in the order of the calls."""
        await hooks("tch.guard", _Hook(pre=_block_second), types=(HookType.PRE_TOOL_CALL,))
        unknown = {"id": "call_2", "type": "function",
                   "function": {"name": "no_such_tool", "arguments": "{}"}}

        _, model, _ = await _run([_call("call_1", "one"), unknown, _call("call_3", "two"),
                                  _call("call_4", "four")])

        assert [r["id"] for r in model.tool_results()] == ["call_1", "call_2", "call_3", "call_4"]

    async def test_steps_of_blocked_calls_are_no_stuck_signal(self, hooks, monkeypatch):
        """A policy or a person saying no is no reason to switch to a stronger
        model. Different calls each step: the same call sent again verbatim is a
        loop, and the loop detector rightly says so."""
        deny = _Hook(pre=lambda ctx: HookResult(success=True, metadata={"block": "Not allowed."}))
        await hooks("tch.deny", deny, types=(HookType.PRE_TOOL_CALL,))
        probe = _Probe()
        agent = _agent(probe)
        agent.llm = _Model([_call("call_1", "alpha")], [_call("call_2", "bravo 42")],
                           [_call("call_3", "charlie x-ray")])
        escalator = _RecordingEscalator()
        monkeypatch.setattr(agent, "_create_stuck_escalator", lambda **_: escalator)

        [event async for event in agent.run_events("go", session_id="hooks-1")]

        assert probe.received == [] and len(deny.pre_calls) == 3
        assert escalator.reasons == []

    async def test_an_empty_block_text_blocks(self, hooks):
        """A hook passing on a person's empty comment on a "no"."""
        await hooks("tch.deny", _Hook(pre=lambda ctx: HookResult(success=True, metadata={"block": ""})),
                    types=(HookType.PRE_TOOL_CALL,))

        probe, model, _ = await _run([_call("call_1", "one")])

        assert probe.received == []
        assert model.tool_results()[0]["type"] == "ToolCallBlocked"

    async def test_a_cancel_while_a_pre_hook_waits_reports_the_call_cancelled(self, hooks):
        """The hook stops waiting on the cancel and fails -- under on_error:
        block that is no failed check, the run was cancelled."""
        def stops_on_cancel(context):
            context.cancellation_token.cancel()
            raise RuntimeError("stopped waiting: the run was cancelled")

        await hooks("tch.ask", _Hook(pre=stops_on_cancel), types=(HookType.PRE_TOOL_CALL,), on_error="block")

        probe, _, events = await _run([_call("call_1", "one")])

        assert probe.received == []
        assert not [e for e in events if e.get("blocked")], "the cancelled call was recorded as blocked"
        assert any(e.get("type") == "tool_cancelled" for e in events)

    async def test_a_call_cancelled_while_its_hooks_were_asked_reaches_no_post_hook(self, hooks):
        """It never ran: an audit hook must not log a denied call as one that
        ran and was cancelled."""
        def denies_and_cancels(context):
            context.cancellation_token.cancel()
            return HookResult(success=True, metadata={"block": "A person said no."})

        audit = _Hook(pre=denies_and_cancels)
        await hooks("tch.ask", audit)

        probe, _, events = await _run([_call("call_1", "one"), _call("call_2", "two")])

        assert probe.received == []
        assert len(audit.pre_calls) == 1, "a cancelled run asked about the next call too"
        assert audit.post_contexts == [], "a call that never ran reached a post hook"
        assert [e["type"] for e in events if e.get("type", "").startswith("tool_")].count("tool_cancelled") == 2

    async def test_a_block_of_zero_blocks(self, hooks):
        """Only None and False let the call through."""
        await hooks("tch.deny", _Hook(pre=lambda ctx: HookResult(success=True, metadata={"block": 0})),
                    types=(HookType.PRE_TOOL_CALL,))

        probe, model, _ = await _run([_call("call_1", "one")])

        assert probe.received == []
        assert model.tool_results()[0]["type"] == "ToolCallBlocked"

    async def test_a_failing_policy_hook_with_on_error_block_blocks_the_call(self, hooks):
        def broken(context):
            raise RuntimeError("policy store unreachable")

        await hooks("tch.policy", _Hook(pre=broken), types=(HookType.PRE_TOOL_CALL,), on_error="block")

        probe, model, events = await _run([_call("call_1", "one")])

        assert probe.received == [], "a policy hook that failed let its call through"
        [result] = model.tool_results()
        assert result["type"] == "ToolCallBlocked"
        assert "'tch.policy'" in result["error"] and "raised RuntimeError" in result["error"]
        assert any(e.get("type") == "final" for e in events)

    async def test_a_policy_hook_that_times_out_blocks_the_call(self, hooks):
        class _Slow(PluginHook):
            async def on_pre_tool_call(self, context):
                await asyncio.sleep(5)
                return HookResult(success=True, context=context)

        await hooks("tch.slow", _Slow("slow"), types=(HookType.PRE_TOOL_CALL,), timeout=0.2, on_error="block")

        probe, model, _ = await _run([_call("call_1", "one")])

        assert probe.received == []
        assert "timed out" in model.tool_results()[0]["error"]


class TestScriptedCallsAfterReview:

    async def test_injected_params_reach_the_tool_but_no_hook(self, hooks):
        seen = _Hook()
        await hooks("tch.seen", seen)
        probe = _Probe()
        agent = _agent(probe)

        await agent.dispatch_tool_call("probe_echo", {"text": "one"}, hook_source="tool_script",
                                       injected_params={"write_key": "SECRET", "_session_id": "x"})

        [params] = probe.received
        assert params["write_key"] == "SECRET"
        assert "_session_id" not in params, "an injected runtime param reached the tool"
        assert "write_key" not in json.dumps(seen.pre_calls + [c.tool_call for c in seen.post_contexts])

    async def test_a_script_call_that_raises_passes_the_post_hooks(self, hooks):
        def redact(context):
            context.tool_result["result"] = {**context.tool_result["result"],
                                              "error": context.tool_result["result"]["error"]
                                              .replace("secret-123", "<redacted>")}
            return HookResult(success=True, modified=True, context=context)

        await hooks("tch.redact", _Hook(post=redact), types=(HookType.POST_TOOL_CALL,))
        agent = _agent(_Probe())

        result = await agent.dispatch_tool_call("probe_echo", {"text": "boom"}, hook_source="tool_script")

        assert result["status"] == "error" and "<redacted>" in result["error"]
        assert "secret-123" not in json.dumps(result)

    async def test_a_script_call_cancelled_while_its_hook_waited_does_not_run(self, hooks):
        token = SimpleNamespace(is_cancelled=False)

        def person_cancels(context):
            token.is_cancelled = True
            return HookResult(success=True, context=context)

        await hooks("tch.ask", _Hook(pre=person_cancels), types=(HookType.PRE_TOOL_CALL,))
        probe = _Probe()
        agent = _agent(probe)

        with pytest.raises(ToolDispatchError, match="cancelled"):
            await agent.dispatch_tool_call("probe_echo", {"text": "one"}, hook_source="tool_script",
                                           cancellation_token=token)
        assert probe.received == []

    async def test_a_call_without_a_hook_source_still_raises(self):
        agent = _agent(_Probe())

        with pytest.raises(RuntimeError, match="probe broke"):
            await agent.dispatch_tool_call("probe_echo", {"text": "boom"})


class TestCallTiming:
    """post_tool_call runs once every call of the step is done: a hook's own
    clock says when the step ended. Each call's own start and end come with
    the result (telemetry times its spans by them)."""

    async def test_the_post_hook_is_told_when_each_call_ran(self, hooks):
        seen = _Hook()
        await hooks("tch.seen", seen, types=(HookType.POST_TOOL_CALL,))

        await _run([_call("call_fast", "fast"), _call("call_slow", "slow")])

        fast, slow = seen.post_results
        assert slow["finished_at"] - slow["started_at"] >= 0.25, "fixture: the slow call was not slow"
        assert fast["started_at"] <= fast["finished_at"]
        assert fast["finished_at"] < slow["finished_at"] - 0.2, \
            "the fast call is reported as ending with the slow one"

    async def test_a_script_call_is_timed_too(self, hooks):
        seen = _Hook()
        await hooks("tch.seen", seen, types=(HookType.POST_TOOL_CALL,))
        agent = _agent(_Probe())

        before = time.time()
        await agent.dispatch_tool_call("probe_echo", {"text": "slow"}, hook_source="tool_script")

        [result] = seen.post_results
        assert before <= result["started_at"] <= result["finished_at"] - 0.25

