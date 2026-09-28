"""otel: the spans a run leaves, driven through the real path.

Agent.run_events, the hook integration manager, the global hook registry and
the plugin's hooks registered by ``register_plugin_hooks`` from its own
schema.yaml -- only the LLM is scripted (a real ``LLMClient`` subclass that
reports its calls through ``_notify_post_response`` as the provider clients
do; the conftest's fake client reports none and makes no tool calls) and the
exporter is the SDK's InMemorySpanExporter behind the plugin's real
BatchSpanProcessor. Spans are read after ``stop_plugin``, which must flush.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.core.cancellation import get_cancellation_manager
from agent_system.hooks import HookContext, HookResult, HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.llm.models import LLMClient
from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServer, ToolServerRegistry
from agent_system.tools.status import current_request_id
from plugins.otel import hooks as otel_hooks
from plugins.otel import telemetry
from plugins.otel.plugin import PLUGIN_FACTORY

LLM_URL = "https://llm.example.test:8443/v1/chat/completions?key=URL-SECRET"
SECRET_TASK = "TASK-SECRET please echo"
SECRET_ARG = "ARG-SECRET"
SECRET_ANSWER = "ANSWER-SECRET done"
SECRET_RESULT = "RESULT-SECRET"


class _Probe(ToolServer):
    """A real tool server with one tool."""

    def __init__(self):
        super().__init__("probe", AgentSystemConfig(), ToolServerConfig(type="probe", enabled=True))

    def get_tools(self):
        return [{"type": "function", "function": {
            "name": "probe_echo", "description": "Echo the text.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}]

    async def probe_echo(self, params):
        text = params.get("text") or ""
        if text.startswith("slow"):
            await asyncio.sleep(0.4)
        if text == "fail":
            return {"status": "error", "error": "probe says no: " + SECRET_RESULT, "type": "ProbeRefused"}
        if text == "cancel":
            get_cancellation_manager().cancel_request(current_request_id.get())
        if text == "long":
            return {"status": "ok", "echo": SECRET_RESULT + "x" * 5000}
        return {"status": "ok", "echo": f"{text} {SECRET_RESULT}"}


def _call(call_id: str, text: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function",
            "function": {"name": "probe_echo", "arguments": json.dumps({"text": text})}}


class _Model(LLMClient):
    """Asks for one round of tool calls per entry of ``rounds``, then answers.
    Reports every call the way the provider clients do."""

    model = "test-model"

    def __init__(self, *rounds: list[dict[str, Any]], retry_first: bool = False, crash_on: int = 0):
        self._rounds = rounds
        self._retry_first = retry_first
        self._crash_on = crash_on
        self.calls = 0

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.calls += 1
        if self.calls == self._crash_on:
            raise ValueError("the model broke")
        if self._retry_first and self.calls == 1:
            await self._notify_retry("fake", self.model, LLM_URL, True, "HTTP 503 upstream down",
                                     attempt=0, max_attempts=3, duration_ms=5.0)
        asks_tools = self.calls <= len(self._rounds)
        await self._notify_post_response({
            "provider": "fake", "model": self.model, "url": LLM_URL, "is_streaming": True,
            "duration_ms": 25.0, "finish_reason": "tool_calls" if asks_tools else "stop",
            "usage": {"prompt_tokens": 100 + self.calls, "completion_tokens": 7,
                      "prompt_tokens_details": {"cached_tokens": 40}},
        })
        if asks_tools:
            yield {"type": "final", "assistant": {"role": "assistant", "content": None,
                                                  "tool_calls": self._rounds[self.calls - 1]}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": SECRET_ANSWER}}


def _agent() -> Agent:
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
    registry = ToolServerRegistry()
    registry.register("probe", _Probe())
    agent_config = AgentConfig(max_steps=5, llm_profile="normal", tools=ToolConfig(allowed=["probe/*"]))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), registry)


async def _run(model: _Model, agent: Agent | None = None, session_id: str = "otel-1"):
    agent = agent or _agent()
    return [event async for event in agent.run_events(SECRET_TASK, session_id=session_id,
                                                      llm_override=model)]


@pytest.fixture
def exported(monkeypatch):
    """The in-memory exporter the plugin's pipeline exports to."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(telemetry, "build_span_exporter", lambda settings: exporter)
    return exporter


@pytest.fixture
async def otel(exported):
    """start(**config) -> the plugin, built by its factory and registered from
    its schema.yaml; stopped and unregistered after the test."""
    registry = get_hook_registry()
    started: list[Any] = []

    async def start(**config):
        plugin = PLUGIN_FACTORY("otel", None,
                                ToolServerConfig(type="otel", enabled=True, config=config))
        names = await register_plugin_hooks("otel", plugin, metadata=plugin.get_schema_data(),
                                            registry=registry)
        assert len(names) == 4, f"not every hook registered: {names}"
        await plugin.start_plugin()
        started.append((plugin, names))
        return plugin

    yield start
    for plugin, names in started:
        await plugin.stop_plugin()
        for name in names:
            for hook_type in HookType:
                await registry.unregister_hook(hook_type, name)
    # A hook that raised is swallowed (telemetry never fails a run), and
    # stop_plugin ends what it left open -- a test would not see it otherwise.
    for plugin, _ in started:
        assert not plugin._failures_reported, f"a hook raised: {plugin._failures_reported}"


async def _spans(plugin, exported) -> list[Any]:
    await plugin.stop_plugin()
    return list(exported.get_finished_spans())


def _named(spans, prefix: str) -> list[Any]:
    return [span for span in spans if span.name.startswith(prefix)]


def _all_attribute_text(spans) -> str:
    return json.dumps([{**dict(s.attributes), "__status": s.status.description} for s in spans],
                      default=str)


# --- the span tree --------------------------------------------------------------

class TestSpanTree:

    async def test_a_run_with_an_llm_call_and_a_tool_call_is_one_trace(self, otel, exported):
        plugin = await otel()

        events = await _run(_Model([_call("call_1", "one")]))
        spans = await _spans(plugin, exported)

        assert any(e.get("type") == "final" for e in events), "fixture: the run did not finish"
        [run] = _named(spans, "invoke_agent")
        chats = _named(spans, "chat")
        [tool] = _named(spans, "execute_tool")
        assert len(chats) == 2 and len(spans) == 4, [s.name for s in spans]
        assert run.parent is None
        for child in (*chats, tool):
            assert child.context.trace_id == run.context.trace_id
            assert child.parent.span_id == run.context.span_id
        assert run.name == "invoke_agent test_agent" and run.kind == SpanKind.INTERNAL
        assert tool.name == "execute_tool probe_echo" and tool.kind == SpanKind.INTERNAL
        assert all(c.name == "chat test-model" and c.kind == SpanKind.CLIENT for c in chats)
        # The run holds its children in time.
        assert run.start_time <= min(s.start_time for s in (*chats, tool))
        assert run.end_time >= max(s.end_time for s in (*chats, tool))

    async def test_the_spans_carry_the_genai_attributes(self, otel, exported):
        plugin = await otel()

        await _run(_Model([_call("call_1", "one")]), session_id="otel-attrs")
        spans = await _spans(plugin, exported)

        [run] = _named(spans, "invoke_agent")
        first_chat = min(_named(spans, "chat"), key=lambda s: s.start_time)
        [tool] = _named(spans, "execute_tool")
        request_id = run.attributes["scarabhive.request_id"]
        assert request_id
        assert dict(run.attributes) == {
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "test_agent",
            "gen_ai.conversation.id": "otel-attrs", "session.id": "otel-attrs",
            "scarabhive.request_id": request_id, "scarabhive.run.outcome": "completed",
            # This agent has no session service: the conversation reached no disk.
            "scarabhive.run.persisted": False}
        assert run.status.status_code == StatusCode.UNSET
        chat = dict(first_chat.attributes)
        assert chat["gen_ai.operation.name"] == "chat"
        assert chat["gen_ai.provider.name"] == "fake"
        assert chat["gen_ai.request.model"] == "test-model"
        assert chat["gen_ai.request.stream"] is True
        assert chat["gen_ai.usage.input_tokens"] == 101
        assert chat["gen_ai.usage.output_tokens"] == 7
        assert chat["gen_ai.usage.cache_read.input_tokens"] == 40
        assert chat["gen_ai.response.finish_reasons"] == ("tool_calls",)
        assert chat["server.address"] == "llm.example.test" and chat["server.port"] == 8443
        assert chat["gen_ai.conversation.id"] == "otel-attrs"
        assert chat["scarabhive.request_id"] == request_id
        assert (first_chat.end_time - first_chat.start_time) == 25_000_000, \
            "the LLM span is not placed by the duration the client reported"
        assert dict(tool.attributes) == {
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "probe_echo",
            "gen_ai.tool.type": "function", "gen_ai.tool.call.id": "call_1",
            "scarabhive.tool.server": "probe", "scarabhive.tool.source": "model",
            "scarabhive.tool.outcome": "ok", "gen_ai.agent.name": "test_agent",
            "gen_ai.conversation.id": "otel-attrs", "session.id": "otel-attrs",
            "scarabhive.request_id": request_id}
        assert "URL-SECRET" not in _all_attribute_text(spans), "the request URL's query was exported"

    async def test_an_agent_called_as_a_tool_is_in_its_callers_conversation(self, otel, exported):
        """It runs on a session of its own below its caller's (Agent.tool_session_id). Named by that session
        alone, its spans left the conversation they belong to: a backend grouping by conversation split one
        into pieces. The session it ran on stays its own."""
        from agent_system.servers.agent.server import tool_session_id

        caller = _agent()
        helper = Agent("helper", caller.system_config, ToolServerConfig(
            type="agent", enabled=True, agent_config=AgentConfig(max_steps=3, llm_profile="normal")),
            caller.registry)
        helper.llm = _Model()
        helper._tool_visible = True  # metadata.visibility: tool
        caller.registry.register("helper", helper)
        caller.agent_config.tools.allowed.append("helper")
        plugin = await otel()

        await _run(_Model([{"id": "call_h", "type": "function",
                            "function": {"name": "helper", "arguments": json.dumps({"task": "look it up"})}}]),
                   agent=caller, session_id="otel-caller")
        spans = await _spans(plugin, exported)

        runs = {span.attributes["gen_ai.agent.name"]: dict(span.attributes) for span in _named(spans, "invoke_agent")}
        assert set(runs) == {"test_agent", "helper"}, f"fixture: the helper did not run: {sorted(runs)}"
        assert (runs["helper"]["gen_ai.conversation.id"], runs["helper"]["session.id"]) == (
            "otel-caller", tool_session_id("otel-caller", "helper"))
        assert (runs["test_agent"]["gen_ai.conversation.id"], runs["test_agent"]["session.id"]) == (
            "otel-caller", "otel-caller")
        [call] = [dict(tool.attributes) for tool in _named(spans, "execute_tool")]
        assert (call["gen_ai.conversation.id"], call["session.id"]) == ("otel-caller", "otel-caller"), call

    async def test_the_user_id_is_exported_when_configured(self, otel, exported):
        agent = _agent()
        # As the API and agent-cli record whose run it is, before the first step.
        agent._session_tracker.set_session_metadata("otel-user", {"user_id": "user-42"})
        plugin = await otel(capture_user_id=True)

        await _run(_Model(), agent=agent, session_id="otel-user")
        spans = await _spans(plugin, exported)

        assert spans and all(s.attributes.get("user.id") == "user-42" for s in spans)

    async def test_the_user_id_stays_out_by_default(self, otel, exported):
        agent = _agent()
        agent._session_tracker.set_session_metadata("otel-user", {"user_id": "user-42"})
        plugin = await otel()

        await _run(_Model(), agent=agent, session_id="otel-user")
        spans = await _spans(plugin, exported)

        assert spans and not any("user.id" in s.attributes for s in spans)
        assert "user-42" not in _all_attribute_text(spans)

    async def test_parallel_tool_calls_are_timed_by_the_call_itself(self, otel, exported):
        """The post hooks run once every call of the step is done; timed by
        the hooks' own clock, the fast call would last as long as the slow one."""
        plugin = await otel()

        await _run(_Model([_call("call_fast", "fast"), _call("call_slow", "slow")]))
        spans = await _spans(plugin, exported)

        tools = {s.attributes["gen_ai.tool.call.id"]: s for s in _named(spans, "execute_tool")}
        fast, slow = tools["call_fast"], tools["call_slow"]
        assert (slow.end_time - slow.start_time) >= 350_000_000, "fixture: the slow call was not slow"
        assert (fast.end_time - fast.start_time) < 200_000_000, \
            "the fast call's span lasted until the slow call ended"

    async def test_request_ids_that_extend_a_run_nest_under_it(self, otel, exported):
        """Sub-agent runs (<id>_007_sub_x) and tool_script calls (<id>_007_ts01)
        carry their run's request id as a prefix."""
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()

        def llm_event(request_id):
            return HookContext(hook_type=HookType.POST_LLM_RESPONSE, request_id=request_id,
                               session_id="s", agent=agent, agent_name="parent",
                               llm_provider="fake", llm_model="m", llm_duration_ms=1.0)

        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, llm_event("r1"))
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, llm_event("r1_007_sub_ab12"))
        script_call = {"id": None, "name": "probe_echo", "server": "probe",
                       "arguments": {}, "source": "tool_script"}
        await registry.execute_hooks(HookType.POST_TOOL_CALL, HookContext(
            hook_type=HookType.POST_TOOL_CALL, request_id="r1_008_ts01", session_id="s",
            agent=agent, agent_name="parent", tool_call=script_call,
            tool_result={"result": {"status": "ok"}, "is_error": False,
                         "started_at": time.time() - 0.01, "finished_at": time.time()}))
        for request_id in ("r1_007_sub_ab12", "r1"):
            await registry.execute_hooks(HookType.SESSION_END, HookContext(
                hook_type=HookType.SESSION_END, request_id=request_id, session_id="s",
                agent=agent, agent_name="parent", metadata={"completed": True}))
        spans = await _spans(plugin, exported)

        runs = {s.attributes["scarabhive.request_id"]: s for s in _named(spans, "invoke_agent")}
        assert set(runs) == {"r1", "r1_007_sub_ab12"}, "a tool_script call opened a run of its own"
        assert {r.attributes["scarabhive.run.outcome"] for r in runs.values()} == {"completed"}
        top, sub = runs["r1"], runs["r1_007_sub_ab12"]
        assert top.parent is None
        assert sub.parent.span_id == top.context.span_id
        [script] = _named(spans, "execute_tool")
        assert script.parent.span_id == top.context.span_id
        assert script.attributes["scarabhive.tool.source"] == "tool_script"

    async def test_an_llm_call_after_its_run_ended_opens_no_new_run(self, otel, exported):
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        event = HookContext(hook_type=HookType.POST_LLM_RESPONSE, request_id="r2", session_id="s",
                            agent=agent, agent_name="a", llm_provider="fake", llm_model="m")

        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, event)
        await registry.execute_hooks(HookType.SESSION_END, HookContext(
            hook_type=HookType.SESSION_END, request_id="r2", session_id="s", agent=agent,
            agent_name="a", metadata={"completed": True}))
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, event)
        spans = await _spans(plugin, exported)

        [run] = _named(spans, "invoke_agent")
        chats = _named(spans, "chat")
        assert len(chats) == 2 and all(c.parent.span_id == run.context.span_id for c in chats)


# --- content ----------------------------------------------------------------------

class TestContent:

    async def test_no_content_is_exported_by_default(self, otel, exported):
        plugin = await otel()

        await _run(_Model([_call("call_1", SECRET_ARG), _call("call_2", "fail")]))
        spans = await _spans(plugin, exported)

        assert len(_named(spans, "execute_tool")) == 2, "fixture: the tool calls left no spans"
        text = _all_attribute_text(spans)
        for secret in ("TASK-SECRET", SECRET_ARG, "ANSWER-SECRET", SECRET_RESULT):
            assert secret not in text, f"{secret} left the process without capture_content"
        for span in spans:
            assert not set(span.attributes) & set(otel_hooks.CONTENT_ATTRIBUTES), span.name

    async def test_content_is_exported_capped_when_enabled(self, otel, exported):
        plugin = await otel(capture_content=True, content_max_chars=200)

        await _run(_Model([_call("call_1", "long")]))
        spans = await _spans(plugin, exported)

        [tool] = _named(spans, "execute_tool")
        [run] = _named(spans, "invoke_agent")
        result = tool.attributes["gen_ai.tool.call.result"]
        assert SECRET_RESULT in result, "the result was not captured"
        assert len(result) <= 200 and result.endswith(otel_hooks.TRUNCATION_MARK)
        assert json.loads(tool.attributes["gen_ai.tool.call.arguments"]) == {"text": "long"}
        [opening] = json.loads(run.attributes["gen_ai.input.messages"])
        assert opening["role"] == "user" and opening["parts"][0]["content"] == SECRET_TASK
        [answer] = json.loads(run.attributes["gen_ai.output.messages"])
        assert answer["parts"][0]["content"] == SECRET_ANSWER
        for span in spans:
            for key in otel_hooks.CONTENT_ATTRIBUTES:
                assert len(span.attributes.get(key, "")) <= 200


# --- errors, cancellation, calls without a post hook ----------------------------

class _Block(PluginHook):
    def __init__(self):
        super().__init__("block")

    async def on_pre_tool_call(self, context):
        if context.tool_call["arguments"].get("text") == "two":
            return HookResult(success=True, metadata={"block": "Not this one."})
        return HookResult(success=True, context=context)


class TestErrors:

    async def test_an_llm_error_ends_its_span_with_an_error_status(self, otel, exported):
        plugin = await otel()

        await _run(_Model(retry_first=True))
        spans = await _spans(plugin, exported)

        failed = [s for s in _named(spans, "chat") if s.status.status_code == StatusCode.ERROR]
        [ok] = [s for s in _named(spans, "chat") if s.status.status_code != StatusCode.ERROR]
        [retry] = failed
        assert retry.attributes["error.type"] == "503"
        assert retry.attributes["scarabhive.llm.retry"] is True
        assert retry.status.description == "LLM request failed (HTTP 503)", \
            "the error text left the process without capture_content"
        assert "gen_ai.response.finish_reasons" not in retry.attributes
        assert "error.type" not in ok.attributes

    async def test_the_llm_error_text_is_exported_with_capture_content(self, otel, exported):
        plugin = await otel(capture_content=True)

        await _run(_Model(retry_first=True))
        spans = await _spans(plugin, exported)

        [retry] = [s for s in _named(spans, "chat") if s.status.status_code == StatusCode.ERROR]
        assert "upstream down" in retry.status.description

    async def test_a_failing_tool_ends_its_span_with_an_error_status(self, otel, exported):
        plugin = await otel()

        await _run(_Model([_call("call_1", "fail")]))
        spans = await _spans(plugin, exported)

        [tool] = _named(spans, "execute_tool")
        assert tool.status.status_code == StatusCode.ERROR
        assert tool.attributes["scarabhive.tool.outcome"] == "error"
        assert tool.attributes["error.type"] == "ProbeRefused"
        assert SECRET_RESULT not in (tool.status.description or ""), \
            "the tool's error text (its result) left the process without capture_content"

    async def test_a_crashed_run_ends_its_span_with_an_error_status(self, otel, exported):
        plugin = await otel()
        agent = _agent()

        async def broken_setup():
            raise ValueError("tool setup broke")

        agent._tool_integration_manager.setup_tool_integration = broken_setup
        events = await _run(_Model(), agent=agent)
        spans = await _spans(plugin, exported)

        assert any("tool setup broke" in str(e.get("message")) for e in events), "fixture: no crash"
        [run] = _named(spans, "invoke_agent")
        assert run.status.status_code == StatusCode.ERROR
        assert run.attributes["scarabhive.run.outcome"] == "error"
        # A crash text may quote content: it stays in without capture_content.
        assert run.status.description == "run reported 1 error(s)"

    async def test_the_run_error_text_is_exported_with_capture_content(self, otel, exported):
        plugin = await otel(capture_content=True)
        agent = _agent()

        async def broken_setup():
            raise ValueError("tool setup broke")

        agent._tool_integration_manager.setup_tool_integration = broken_setup
        await _run(_Model(), agent=agent)
        spans = await _spans(plugin, exported)

        [run] = _named(spans, "invoke_agent")
        assert "tool setup broke" in run.status.description

    async def test_a_cancelled_run_ends_its_span_as_cancelled(self, otel, exported):
        plugin = await otel()

        events = await _run(_Model([_call("call_1", "cancel")], [_call("call_2", "one")]))
        spans = await _spans(plugin, exported)

        assert any(e.get("type") == "cancelled" for e in events), "fixture: the run was not cancelled"
        [run] = _named(spans, "invoke_agent")
        assert run.status.status_code == StatusCode.ERROR
        assert run.attributes["scarabhive.run.outcome"] == "cancelled"

    @pytest.mark.parametrize("blocker", ["aa_policy.guard", "zz_policy.guard"],
                             ids=["blocker-runs-before-otel", "blocker-runs-after-otel"])
    async def test_a_blocked_call_gets_a_span_though_no_post_hook_comes(self, otel, exported, blocker):
        """Blocked calls reach no post hook. Ours sees the pre hook when it runs
        first (the pending record is resolved at session_end); when the blocker
        runs first, only the blocked result in the run's messages tells."""
        plugin = await otel()
        registry = get_hook_registry()
        await registry.register_hook(HookType.PRE_TOOL_CALL, blocker, _Block())
        try:
            await _run(_Model([_call("call_1", "one"), _call("call_2", "two")]))
        finally:
            await registry.unregister_hook(HookType.PRE_TOOL_CALL, blocker)
        spans = await _spans(plugin, exported)

        ids = sorted(s.attributes["gen_ai.tool.call.id"] for s in _named(spans, "execute_tool"))
        assert ids == ["call_1", "call_2"], "a call got no span, or one got two"
        tools = {s.attributes["gen_ai.tool.call.id"]: s for s in _named(spans, "execute_tool")}
        blocked = tools["call_2"]
        assert blocked.attributes["scarabhive.tool.outcome"] == "blocked"
        assert blocked.attributes["gen_ai.tool.name"] == "probe_echo"
        assert blocked.status.status_code == StatusCode.ERROR
        assert blocked.end_time == blocked.start_time, "a call that never ran has no duration"
        assert tools["call_1"].attributes["scarabhive.tool.outcome"] == "ok"
        [run] = _named(spans, "invoke_agent")
        assert blocked.parent.span_id == run.context.span_id
        assert run.attributes["scarabhive.run.outcome"] == "completed"


def _post_event(request_id: str, call_id: Any, agent: Any, result: Any, *, name: str = "probe_echo",
                is_error: bool = False) -> HookContext:
    now = time.time()
    return HookContext(hook_type=HookType.POST_TOOL_CALL, request_id=request_id, session_id="s",
                       agent=agent, agent_name="a", step=1,
                       tool_call={"id": call_id, "name": name, "server": "probe",
                                  "arguments": {}, "source": "model"},
                       tool_result={"result": result, "is_error": is_error,
                                    "started_at": now - 0.01, "finished_at": now})


def _end_event(request_id: str, agent: Any, **metadata) -> HookContext:
    return HookContext(hook_type=HookType.SESSION_END, request_id=request_id, session_id="s",
                       agent=agent, agent_name="a", metadata=metadata)


class TestCallOutcomes:

    async def test_a_call_cancelled_while_it_ran(self, otel, exported):
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "c1", agent))
        await registry.execute_hooks(HookType.POST_TOOL_CALL, _post_event(
            "r1", "c1", agent, {"error": "Tool 'probe' was cancelled.", "cancelled": True}, is_error=True))
        spans = await _spans(plugin, exported)

        [call] = _named(spans, "execute_tool")
        assert call.attributes["scarabhive.tool.outcome"] == "cancelled"
        assert call.attributes["error.type"] == "cancelled"
        assert call.status.status_code == StatusCode.ERROR

    @pytest.mark.parametrize("cancelled,outcome", [(True, "cancelled"), (False, "unknown")])
    async def test_a_call_left_waiting_at_session_end_takes_the_runs_outcome(
            self, otel, exported, cancelled, outcome):
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "c1", agent))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, cancelled=cancelled))
        spans = await _spans(plugin, exported)

        [call] = _named(spans, "execute_tool")
        assert call.attributes["scarabhive.tool.outcome"] == outcome
        assert (call.status.status_code == StatusCode.ERROR) is cancelled

    async def test_a_call_left_waiting_by_a_crash_is_unknown_not_cancelled(self, otel, exported):
        """A crash cancels the run's token (the one the call's hooks got); the
        run was not cancelled, and neither was its call."""
        from types import SimpleNamespace
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        pre = _pre_event("r1", "c1", agent)
        pre.cancellation_token = SimpleNamespace(is_cancelled=True)
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, pre)
        await registry.execute_hooks(HookType.SESSION_END, _end_event(
            "r1", agent, cancelled=False, errors=["Agent execution failed: boom"], completed=False))
        spans = await _spans(plugin, exported)

        [call] = _named(spans, "execute_tool")
        assert call.attributes["scarabhive.tool.outcome"] == "unknown"
        [run] = _named(spans, "invoke_agent")
        assert run.attributes["scarabhive.run.outcome"] == "error"

    async def test_a_run_without_a_final_answer_is_incomplete(self, otel, exported):
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("r1", agent))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, completed=False))
        spans = await _spans(plugin, exported)

        [run] = _named(spans, "invoke_agent")
        assert run.attributes["scarabhive.run.outcome"] == "incomplete"
        assert run.status.status_code == StatusCode.ERROR

    async def test_calls_that_share_a_key_are_matched_in_call_order(self, otel, exported):
        """A backend that sends no call ids: two calls of one tool in a step
        share their key; their post hooks come in call order."""
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        for _ in range(2):
            await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "", agent))
        await registry.execute_hooks(HookType.POST_TOOL_CALL, _post_event("r1", "", agent, {"status": "ok"}))
        await registry.execute_hooks(HookType.POST_TOOL_CALL, _post_event(
            "r1", "", agent, {"status": "error", "error": "x"}, is_error=True))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, completed=True))
        spans = await _spans(plugin, exported)

        assert sorted(s.attributes["scarabhive.tool.outcome"] for s in _named(spans, "execute_tool")) \
            == ["error", "ok"]

    async def test_a_post_hook_after_its_call_was_given_up_adds_no_second_span(self, otel, exported):
        plugin = await otel(max_pending_tool_calls=1)
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "c1", agent))
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "c2", agent))
        await registry.execute_hooks(HookType.POST_TOOL_CALL, _post_event("r1", "c1", agent, {"status": "ok"}))
        await registry.execute_hooks(HookType.POST_TOOL_CALL, _post_event("r1", "c2", agent, {"status": "ok"}))
        spans = await _spans(plugin, exported)

        calls = sorted((s.attributes["gen_ai.tool.call.id"], s.attributes["scarabhive.tool.outcome"])
                       for s in _named(spans, "execute_tool"))
        assert calls == [("c1", "unknown"), ("c2", "ok")]

    async def test_a_reused_request_id_gets_a_run_span_of_its_own(self, otel, exported):
        """A job dispatched again under its id: the second run holds a new
        cancellation token, and its events belong to it, not to the first."""
        plugin = await otel()
        registry = get_hook_registry()
        manager = get_cancellation_manager()
        agent = _agent()
        try:
            manager.create_token("job-reused")
            await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("job-reused", agent))
            manager.unregister_request("job-reused")
            await registry.execute_hooks(HookType.SESSION_END, _end_event("job-reused", agent, completed=True))
            manager.create_token("job-reused")
            await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("job-reused", agent))
            await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("job-reused", "c1", agent))
            manager.unregister_request("job-reused")
            await registry.execute_hooks(HookType.SESSION_END, _end_event(
                "job-reused", agent, errors=["the second attempt failed"]))
        finally:
            manager.unregister_request("job-reused")
        spans = await _spans(plugin, exported)

        runs = sorted(_named(spans, "invoke_agent"), key=lambda s: s.start_time)
        assert [r.attributes["scarabhive.run.outcome"] for r in runs] == ["completed", "error"]
        first, second = runs
        chats = sorted(_named(spans, "chat"), key=lambda s: s.start_time)
        assert chats[0].parent.span_id == first.context.span_id
        assert chats[1].parent.span_id == second.context.span_id
        [call] = _named(spans, "execute_tool")
        assert call.parent.span_id == second.context.span_id
        assert call.attributes["scarabhive.tool.outcome"] == "unknown"

    async def test_a_second_run_under_an_ended_id_that_opened_no_span_still_gets_one(self, otel, exported):
        """It failed on its way in: no LLM or tool event, only its session_end."""
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("r1", agent))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, completed=True))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, errors=["no LLM"]))
        spans = await _spans(plugin, exported)

        assert sorted(r.attributes["scarabhive.run.outcome"] for r in _named(spans, "invoke_agent")) \
            == ["completed", "error"]

    async def test_a_blocked_call_sent_without_an_id_is_reported_once(self, otel, exported):
        """Its result carries a made-up id (blocked-call-...), so the scan for
        blocked results cannot match it to the pending call: it must leave it."""
        from agent_system.llm.models import ChatMessage
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "", agent))
        messages = [
            ChatMessage(role="user", content="go", request_id="r1"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "", "type": "function", "function": {"name": "probe_echo", "arguments": "{}"}}]),
            ChatMessage(role="tool", tool_call_id="blocked-call-1", name="probe_echo", content=json.dumps(
                {"status": "error", "error": "no", "type": "ToolCallBlocked"})),
        ]
        end = _end_event("r1", agent, completed=True)
        end.messages = messages
        await registry.execute_hooks(HookType.SESSION_END, end)
        spans = await _spans(plugin, exported)

        [run] = _named(spans, "invoke_agent")
        assert run.attributes["scarabhive.run.outcome"] == "completed", "the run's end was lost"
        assert len(_named(spans, "execute_tool")) == 1, "the call was reported twice"

    async def test_a_script_call_that_ends_after_its_run_adds_no_second_span(self, otel, exported):
        """tool_script runs its script in a thread: cancelled with its run, a
        call may still finish after the run's session_end settled it."""
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("r1", agent))
        script_pre = _pre_event("r1_007_ts01", None, agent)
        script_pre.step = 0
        script_pre.tool_call["source"] = "tool_script"
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, script_pre)
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, cancelled=True))
        script_post = _post_event("r1_007_ts01", None, agent, {"status": "ok"})
        script_post.step = 0
        script_post.tool_call["source"] = "tool_script"
        await registry.execute_hooks(HookType.POST_TOOL_CALL, script_post)
        spans = await _spans(plugin, exported)

        assert [s.attributes["scarabhive.tool.outcome"] for s in _named(spans, "execute_tool")] == ["cancelled"]

    async def test_a_call_given_up_by_the_sweep_adds_no_second_span(self, otel, exported):
        plugin = await otel(idle_timeout_seconds=60)
        now = [1000.0]
        plugin._clock = lambda: now[0]
        registry = get_hook_registry()
        agent = _agent()
        script_pre = _pre_event("lonely_ts01", "c1", agent)
        script_pre.tool_call["source"] = "tool_script"  # no run: nothing keeps it waiting
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, script_pre)
        now[0] += 61
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("other", agent))
        assert not plugin._pending, "fixture: the call was not given up"
        script_post = _post_event("lonely_ts01", "c1", agent, {"status": "ok"})
        script_post.tool_call["source"] = "tool_script"
        await registry.execute_hooks(HookType.POST_TOOL_CALL, script_post)
        spans = await _spans(plugin, exported)

        assert [s.attributes["scarabhive.tool.outcome"] for s in _named(spans, "execute_tool")] == ["unknown"]

    async def test_a_retry_after_an_expired_run_still_gets_a_span(self, otel, exported):
        plugin = await otel(idle_timeout_seconds=60)
        now = [1000.0]
        plugin._clock = lambda: now[0]
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("r1", agent))
        now[0] += 61
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("other", agent))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, completed=True))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, errors=["no LLM"]))
        spans = await _spans(plugin, exported)

        outcomes = sorted(r.attributes["scarabhive.run.outcome"] for r in _named(spans, "invoke_agent")
                          if r.attributes["scarabhive.request_id"] == "r1")
        assert outcomes == ["error", "expired"]

    async def test_a_new_run_takes_over_an_id_whose_end_never_came(self, otel, exported):
        plugin = await otel()
        registry = get_hook_registry()
        manager = get_cancellation_manager()
        agent = _agent()
        try:
            manager.create_token("job-stuck")
            await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("job-stuck", agent))
            manager.unregister_request("job-stuck")  # its end did not reach the plugin
            manager.create_token("job-stuck")
            await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("job-stuck", agent))
            manager.unregister_request("job-stuck")
            await registry.execute_hooks(HookType.SESSION_END, _end_event("job-stuck", agent, completed=True))
        finally:
            manager.unregister_request("job-stuck")
        spans = await _spans(plugin, exported)

        runs = sorted(_named(spans, "invoke_agent"), key=lambda s: s.start_time)
        assert [r.attributes["scarabhive.run.outcome"] for r in runs] == ["evicted", "completed"]
        chats = sorted(_named(spans, "chat"), key=lambda s: s.start_time)
        assert [c.parent.span_id for c in chats] == [r.context.span_id for r in runs]

    async def test_a_parent_waiting_on_its_sub_agent_does_not_expire(self, otel, exported):
        plugin = await otel(idle_timeout_seconds=60)
        now = [1000.0]
        plugin._clock = lambda: now[0]
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "spawn", agent))
        for _ in range(4):  # the sub-agent works for 200 s, the parent waits
            now[0] += 50
            await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("r1_007_sub_ab", agent))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1_007_sub_ab", agent, completed=True))
        now[0] += 50
        await registry.execute_hooks(HookType.POST_TOOL_CALL, _post_event("r1", "spawn", agent, {"status": "ok"}))
        await registry.execute_hooks(HookType.SESSION_END, _end_event("r1", agent, completed=True))
        spans = await _spans(plugin, exported)

        runs = {s.attributes["scarabhive.request_id"]: s for s in _named(spans, "invoke_agent")}
        assert runs["r1"].attributes["scarabhive.run.outcome"] == "completed"
        assert [s.attributes["scarabhive.tool.outcome"] for s in _named(spans, "execute_tool")] == ["ok"]

    async def test_an_llm_span_ends_when_the_client_reported_it(self, otel, exported):
        plugin = await otel()
        registry = get_hook_registry()
        reported = time.time() - 5
        event = _llm_event("r1", _agent())
        event.metadata = {"timestamp_ms": reported * 1000}
        event.llm_duration_ms = 100.0

        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, event)
        spans = await _spans(plugin, exported)

        [chat] = _named(spans, "chat")
        assert abs(chat.end_time - reported * 1e9) < 1e6
        assert chat.end_time - chat.start_time == 100_000_000


# --- bounded state ----------------------------------------------------------------

def _llm_event(request_id: str, agent: Any) -> HookContext:
    return HookContext(hook_type=HookType.POST_LLM_RESPONSE, request_id=request_id, session_id="s",
                       agent=agent, agent_name="a", llm_provider="fake", llm_model="m")


def _pre_event(request_id: str, call_id: str, agent: Any) -> HookContext:
    return HookContext(hook_type=HookType.PRE_TOOL_CALL, request_id=request_id, session_id="s",
                       agent=agent, agent_name="a", step=1,
                       tool_call={"id": call_id, "name": "probe_echo", "server": "probe",
                                  "arguments": {}, "source": "model"})


class TestBoundedState:

    async def test_open_runs_are_bounded_and_the_evicted_one_ends(self, otel, exported):
        plugin = await otel(max_open_runs=2)
        registry = get_hook_registry()
        agent = _agent()

        for request_id in ("r1", "r2", "r3"):
            await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event(request_id, agent))

        assert list(plugin._runs) == ["r2", "r3"]
        plugin.force_flush()
        [evicted] = _named(exported.get_finished_spans(), "invoke_agent")
        assert evicted.attributes["scarabhive.request_id"] == "r1"
        assert evicted.attributes["scarabhive.run.outcome"] == "evicted"
        assert evicted.status.status_code == StatusCode.ERROR

    async def test_an_idle_run_and_a_call_without_post_expire(self, otel, exported):
        plugin = await otel(idle_timeout_seconds=60)
        now = [1000.0]
        plugin._clock = lambda: now[0]
        registry = get_hook_registry()
        agent = _agent()

        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "call_1", agent))
        assert "r1" in plugin._runs and len(plugin._pending) == 1, "fixture: nothing was opened"
        now[0] += 61
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("other", agent))

        assert "r1" not in plugin._runs and not plugin._pending
        plugin.force_flush()
        finished = exported.get_finished_spans()
        [run] = [s for s in _named(finished, "invoke_agent")
                 if s.attributes["scarabhive.request_id"] == "r1"]
        assert run.attributes["scarabhive.run.outcome"] == "expired"
        [call] = _named(finished, "execute_tool")
        assert call.attributes["scarabhive.tool.outcome"] == "unknown"

    async def test_pending_calls_are_bounded(self, otel, exported):
        plugin = await otel(max_pending_tool_calls=2)
        registry = get_hook_registry()
        agent = _agent()

        for call_id in ("c1", "c2", "c3"):
            await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", call_id, agent))

        assert [call.call_id for call in plugin._pending.values()] == ["c2", "c3"]
        plugin.force_flush()
        [dropped] = _named(exported.get_finished_spans(), "execute_tool")
        assert dropped.attributes["gen_ai.tool.call.id"] == "c1"
        assert dropped.attributes["scarabhive.tool.outcome"] == "unknown"

    async def test_stopping_ends_what_is_open_and_flushes(self, otel, exported):
        plugin = await otel()
        registry = get_hook_registry()
        agent = _agent()
        await registry.execute_hooks(HookType.POST_LLM_RESPONSE, _llm_event("r1", agent))
        await registry.execute_hooks(HookType.PRE_TOOL_CALL, _pre_event("r1", "c1", agent))

        spans = await _spans(plugin, exported)

        [run] = _named(spans, "invoke_agent")
        assert run.attributes["scarabhive.run.outcome"] == "shutdown"
        assert [s.attributes["scarabhive.tool.outcome"] for s in _named(spans, "execute_tool")] == ["unknown"]
        assert not plugin._runs and not plugin._pending


# --- the pipeline ----------------------------------------------------------------

class _SlowExporter(InMemorySpanExporter):
    def export(self, spans):
        time.sleep(1.0)
        return super().export(spans)


class TestPipeline:

    async def test_a_slow_exporter_does_not_hold_up_the_run(self, otel, monkeypatch):
        slow = _SlowExporter()
        monkeypatch.setattr(telemetry, "build_span_exporter", lambda settings: slow)
        plugin = await otel(shutdown_timeout_seconds=0.2)

        started = time.monotonic()
        events = await _run(_Model([_call("call_1", "one")]))
        took = time.monotonic() - started
        await plugin.stop_plugin()

        assert any(e.get("type") == "final" for e in events)
        assert took < 1.0, f"the run waited for the exporter ({took:.2f}s)"

    async def test_a_host_tracer_provider_is_used_not_replaced(self, otel, exported, monkeypatch):
        host_exporter = InMemorySpanExporter()
        host = TracerProvider()
        host.add_span_processor(SimpleSpanProcessor(host_exporter))
        monkeypatch.setattr(trace_api, "get_tracer_provider", lambda: host)
        plugin = await otel()

        await _run(_Model())
        await plugin.stop_plugin()

        assert _named(host_exporter.get_finished_spans(), "invoke_agent"), "the host's pipeline got nothing"
        assert exported.get_finished_spans() == (), "the plugin built its own pipeline beside the host's"
        host.shutdown()

    async def test_metrics_record_tokens_and_duration(self, otel, exported, monkeypatch):
        reader = InMemoryMetricReader()
        monkeypatch.setattr(telemetry, "build_metric_reader", lambda settings: reader)
        plugin = await otel(metrics=True)

        await _run(_Model([_call("call_1", "one")]))
        data = reader.get_metrics_data()
        await plugin.stop_plugin()

        points = {}
        for resource_metrics in data.resource_metrics:
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    points[metric.name] = list(metric.data.data_points)
        tokens = {p.attributes["gen_ai.token.type"]: p for p in points["gen_ai.client.token.usage"]}
        assert tokens["input"].sum == 101 + 102 and tokens["output"].sum == 14
        [duration] = points["gen_ai.client.operation.duration"]
        assert duration.count == 2 and duration.attributes["gen_ai.request.model"] == "test-model"
