"""The LLM hooks get the model that answers the step, not the run's base model.

The step loop picked the model — recovery back to the original, persistent
fallback, escalation — only AFTER the pre-LLM hooks had run with the run's base
client, and handed the post-LLM hooks that base client too. Hooks that size the
context by context.llm (context_engineer's arrival cap, context_summarizer's
trigger, context_usage_tracker's percentage) measured a model the call never
went to: a persistent fallback with a smaller window was sized by the original's.
"""

import time

import httpx
import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    MCPConfig,
)
from agent_system.llm.models import LLMServerError
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.escalation import StuckEscalator
from agent_system.servers.agent.server import Agent


@pytest.fixture
def system_config():
    models = {"big": LLMModelConfig(provider="openai", model="big", context_window=1_000_000),
              "small": LLMModelConfig(provider="openai", model="small", context_window=65_000),
              "mid": LLMModelConfig(provider="openai", model="mid", context_window=200_000)}
    profiles = {name: LLMProfile(model_ref=name) for name in models}
    return AgentSystemConfig(llm_system=LLMSystemConfig(models=models, profiles=profiles,
                                                        default_profile="big"))


class _ScriptedLLM:
    """Call 1: a tool call (uses the step). Later: the final answer. Optionally
    answers with a reasoning block and runs a callback before answering."""

    def __init__(self, name, context_window, reasoning=False, before_answer=None):
        self.name = name
        self.context_window = context_window
        self.call_count = 0
        self.saw_artifacts = []
        self._reasoning = reasoning
        self._before_answer = before_answer

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        self.saw_artifacts.append(any(getattr(m, "reasoning_details", None) for m in messages))
        if self._before_answer:
            self._before_answer()
        if self.call_count == 1:
            assistant = {"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c-{self.name}", "function": {
                             "name": "some_tool", "arguments": "{}"}}]}
            if self._reasoning:
                assistant["reasoning_details"] = [{"format": "openai-responses-items-v1",
                                                   "type": "reasoning.responses_items",
                                                   "items": [{"type": "reasoning", "id": "rs_1"}]}]
            return {"assistant": assistant}
        return {"assistant": {"role": "assistant", "content": f"FINAL-{self.name}", "tool_calls": None}}


def _agent(system_config, original, max_steps=1, llm_profile=("big", "small"), **agent_cfg):
    agent_config = AgentConfig(llm_profile=list(llm_profile), max_steps=max_steps, **agent_cfg)
    agent_config.tools.allowed = ["*"]
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", system_config, mcp_config, MCPRegistry(), llm=original)


def _record_hooks(agent, during_pre=None):
    """Replace both hook entry points with recorders that change nothing.

    during_pre(step) runs inside the pre-LLM hooks — standing in for another
    request on the same agent that changes the shared fallback state meanwhile.
    """
    seen = {"pre": [], "post": [], "session_llm": [], "session_llm_pre": []}

    async def pre(messages, step, request_id, session_id, llm=None, cancellation_token=None):
        seen["pre"].append((step, llm, [m.model_copy() for m in messages]))
        # What a tool run inside the hooks (tool_preload) gets from the agent.
        seen["session_llm_pre"].append(agent.llm_for_session(session_id))
        if during_pre:
            during_pre(step)
        return messages

    async def post(messages, llm_response, step, request_id, session_id, llm=None):
        seen["post"].append((step, llm))
        seen["session_llm"].append(agent.llm_for_session(session_id))
        return llm_response, {}

    agent._hook_manager.execute_pre_llm_hooks = pre
    agent._hook_manager.execute_post_llm_hooks = post
    return seen


async def _run(agent, **kwargs):
    events = []
    async for event in agent.run_events("test task", **kwargs):
        events.append(event)
        if event.get("type") == "end":
            break
    return events


def _announced(events):
    """The 'Calling LLM (...)' status lines, in order."""
    return [str(e) for e in events if "Calling LLM" in str(e)]


@pytest.mark.asyncio
async def test_the_hooks_see_the_persistent_fallback(system_config):
    original = _ScriptedLLM("original", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = _agent(system_config, original)
    agent._active_fallback_llm = fallback
    agent._active_fallback_profile = "small"
    agent._fallback_activated_at = time.time()       # recovery not due
    seen = _record_hooks(agent)

    await _run(agent)

    assert fallback.call_count >= 1 and original.call_count == 0, "fixture: step did not run on the fallback"
    assert seen["pre"] and seen["pre"][0][1] is fallback, "pre-LLM hooks sized the original's window"
    assert seen["post"] and seen["post"][0][1] is fallback, "post-LLM hooks got the model that did not answer"


@pytest.mark.asyncio
async def test_a_due_recovery_happens_before_the_hooks(system_config):
    """Step 1 runs on the fallback and leaves a reasoning block, then the
    recovery clock is aged. Step 2's hooks must see the original AND the
    history already stripped for it — not the fallback, and not a history the
    switch rewrites after they worked on it."""
    original = _ScriptedLLM("original", 1_000_000)
    agent = _agent(system_config, original, max_steps=3)

    def age_the_clock():
        agent._fallback_activated_at = time.time() - 99_999

    fallback = _ScriptedLLM("fallback", 65_000, reasoning=True, before_answer=age_the_clock)
    agent._active_fallback_llm = fallback
    agent._active_fallback_profile = "small"
    agent._fallback_activated_at = time.time()
    seen = _record_hooks(agent)

    await _run(agent)

    assert fallback.call_count == 1 and original.call_count >= 1, "fixture: no recovery happened"
    step2 = [entry for entry in seen["pre"] if entry[0] == 1]
    assert step2, "no hooks ran for step 2"
    _, llm, messages = step2[0]
    assert llm is original, "the hooks of the recovered step still saw the fallback"
    assert not any(m.reasoning_details for m in messages), (
        "the hooks got the fallback's reasoning; the switch stripped it only afterwards")


class _ServerErrorLLM(_ScriptedLLM):
    """Answers every call with a 504 after the client's own retries."""

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        raise LLMServerError("gateway timeout", provider="openai", model=self.name, status_code=504)


@pytest.mark.asyncio
async def test_a_5xx_fallback_carries_the_rest_of_the_request(system_config):
    """The post-LLM hooks got the base client after the retry loop switched, and
    the 5xx path - unlike its upstream/connection twins - left every later step
    starting on the failing original, its hooks sized for it."""
    original = _ServerErrorLLM("original", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = _agent(system_config, original, max_steps=3)
    agent._create_fallback_llm = lambda profile: fallback
    seen = _record_hooks(agent)

    await _run(agent)

    assert fallback.call_count >= 2, "fixture: the fallback did not carry two steps"
    assert seen["post"][0][1] is fallback, "post-LLM hooks got the client that failed"
    assert seen["session_llm"][0] is fallback, "tools after the switch were told the failed client answers"
    assert [llm for step, llm, _ in seen["pre"] if step == 1] == [fallback], (
        "step 2 started on the failing original again")
    assert original.call_count == 1


@pytest.mark.asyncio
async def test_a_fallback_switched_on_by_another_request_during_the_hooks_is_used(system_config):
    original = _ScriptedLLM("original", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = _agent(system_config, original)

    def another_request_hits_a_429(step):
        agent._active_fallback_llm = fallback
        agent._active_fallback_profile = "small"
        agent._fallback_activated_at = time.time()

    _record_hooks(agent, during_pre=another_request_hits_a_429)

    await _run(agent)

    assert original.call_count == 0, "called the model another request had just found rate-limited"
    assert fallback.call_count >= 1


@pytest.mark.asyncio
async def test_a_recovery_by_another_request_during_the_hooks_is_used_with_a_clean_history(system_config):
    """Step 1 runs on the fallback and leaves reasoning; during step 2's hooks
    another request resets the fallback. Step 2 must reach the original, with
    the fallback's reasoning stripped from THIS request's history."""
    original = _ScriptedLLM("original", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000, reasoning=True)
    agent = _agent(system_config, original, max_steps=3)
    agent._active_fallback_llm = fallback
    agent._active_fallback_profile = "small"
    agent._fallback_activated_at = time.time()

    def another_request_recovers(step):
        if step == 1:
            agent.reset_fallback()                   # its own history, not ours

    _record_hooks(agent, during_pre=another_request_recovers)

    await _run(agent)

    assert fallback.call_count == 1 and original.call_count >= 1, "step 2 stayed on the fallback"
    assert original.saw_artifacts[0] is False, "the fallback's reasoning reached the original"


@pytest.mark.asyncio
async def test_tools_can_ask_for_the_model_answering_the_step(system_config):
    original = _ScriptedLLM("original", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = _agent(system_config, original)
    agent._active_fallback_llm = fallback
    agent._active_fallback_profile = "small"
    agent._fallback_activated_at = time.time()
    seen = _record_hooks(agent)

    await _run(agent)

    assert seen["session_llm"] and seen["session_llm"][0] is fallback
    assert seen["session_llm_pre"][0] is fallback, "a tool run inside the pre-LLM hooks got agent.llm"
    assert not agent._step_llms, "the request ended but its step model stayed registered"


def _open_escalation(agent, monkeypatch, advanced, rounds=2):
    escalator = StuckEscalator(enabled=True, rounds=rounds, max_calls=6)
    escalator.trigger("stuck")
    monkeypatch.setattr(agent, "_create_stuck_escalator", lambda already_advanced: escalator)
    monkeypatch.setattr(agent, "_get_escalation_llm", lambda: advanced)
    return escalator


@pytest.mark.asyncio
async def test_a_re_pick_onto_a_fallback_spends_no_escalation_round(system_config, monkeypatch):
    original = _ScriptedLLM("original", 65_000)
    advanced = _ScriptedLLM("advanced", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = _agent(system_config, original)
    escalator = _open_escalation(agent, monkeypatch, advanced)

    def another_request_hits_a_429(step):
        agent._active_fallback_llm = fallback
        agent._active_fallback_profile = "small"
        agent._fallback_activated_at = time.time()

    _record_hooks(agent, during_pre=another_request_hits_a_429)

    await _run(agent)

    assert advanced.call_count == 0 and fallback.call_count >= 1, "fixture: no re-pick onto the fallback"
    assert escalator.calls_used == 0, "a round was spent on an advanced call never made"


@pytest.mark.asyncio
async def test_a_re_pick_onto_a_fallback_strips_the_originals_reasoning(system_config):
    """The switch away strips the history of the request that switched; this
    request's history still carried the original's reasoning items."""
    original = _ScriptedLLM("original", 1_000_000, reasoning=True)
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = _agent(system_config, original, max_steps=3)

    def another_request_hits_a_429(step):
        if step == 1:
            agent._active_fallback_llm = fallback
            agent._active_fallback_profile = "small"
            agent._fallback_activated_at = time.time()

    _record_hooks(agent, during_pre=another_request_hits_a_429)

    await _run(agent)

    assert original.call_count == 1 and fallback.call_count >= 1, "fixture: step 2 did not switch"
    assert fallback.saw_artifacts[0] is False, "the original's reasoning reached the fallback"


@pytest.mark.asyncio
async def test_an_escalation_that_did_not_build_is_not_announced(system_config, monkeypatch):
    original = _ScriptedLLM("original", 65_000)
    agent = _agent(system_config, original)
    _open_escalation(agent, monkeypatch, advanced=None)
    _record_hooks(agent)

    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") == "end":
            break

    calling = [str(e) for e in events if "Calling LLM" in str(e)]
    assert calling, "fixture: no 'Calling LLM' status reached the event stream"
    assert not any("escalated" in text for text in calling), calling


@pytest.mark.asyncio
async def test_a_5xx_on_the_escalation_model_does_not_swap_the_base(system_config, monkeypatch):
    """The advanced model failed, not the base: only that step is rescued, the
    run goes on with its base model."""
    original = _ScriptedLLM("original", 1_000_000)
    advanced = _ServerErrorLLM("advanced", 1_000_000)
    fallback = _ScriptedLLM("fallback", 65_000)
    # Two steps: a third would be escalated again by the stuck detector.
    agent = _agent(system_config, original, max_steps=2)
    agent._create_fallback_llm = lambda profile: fallback
    _open_escalation(agent, monkeypatch, advanced, rounds=1)
    seen = _record_hooks(agent)

    events = await _run(agent)

    assert advanced.call_count == 1 and fallback.call_count == 1, "fixture: step 1 was not rescued once"
    assert [llm for step, llm, _ in seen["pre"] if step == 1] == [original], (
        "the base model was replaced although only the escalation model failed")
    assert ":fallback" not in _announced(events)[-1], "step 2 on the base was announced as the fallback"


@pytest.mark.asyncio
async def test_a_swapped_in_fallback_is_not_its_own_fallback(system_config):
    """After the base's 5xx put 'small' in charge, a 5xx on 'small' retried
    'small' — the chain of later steps did not leave the swapped profile out."""
    original = _ServerErrorLLM("original", 1_000_000)
    small = _FlakyLLM("small", 65_000, fail_on_call=2)
    built = []

    def build(profile):
        built.append(profile)
        return small if profile == "small" else _ScriptedLLM(profile, 1_000_000)

    agent = _agent(system_config, original, max_steps=3)
    agent._create_fallback_llm = build
    _record_hooks(agent)

    await _run(agent)

    assert small.call_count >= 2, "fixture: 'small' never failed"
    assert built.count("small") == 1, built


@pytest.mark.asyncio
async def test_reasoning_reaches_the_next_call_when_the_model_stays(system_config):
    """Control for the strip tests: without a switch the reasoning is carried."""
    original = _ScriptedLLM("original", 1_000_000, reasoning=True)
    agent = _agent(system_config, original, max_steps=3)
    _record_hooks(agent)

    await _run(agent)

    assert original.call_count >= 2 and original.saw_artifacts[1] is True


@pytest.mark.asyncio
async def test_a_switch_between_steps_strips_the_previous_models_reasoning(system_config):
    """Another request switches on a fallback while this one runs its tools —
    between two steps, outside the hook window the re-pick watches."""
    fallback = _ScriptedLLM("fallback", 65_000)
    agent = None

    def another_request_hits_a_429():
        agent._active_fallback_llm = fallback
        agent._active_fallback_profile = "small"
        agent._fallback_activated_at = time.time()

    original = _ScriptedLLM("original", 1_000_000, reasoning=True, before_answer=another_request_hits_a_429)
    agent = _agent(system_config, original, max_steps=3)
    _record_hooks(agent)

    await _run(agent)

    assert original.call_count == 1 and fallback.call_count >= 1, "fixture: step 2 did not switch"
    assert fallback.saw_artifacts[0] is False, "the original's reasoning reached the fallback"


@pytest.mark.asyncio
async def test_after_an_override_failed_the_label_names_the_model_answering(system_config):
    failing_override = _ServerErrorLLM("mid", 200_000)
    small = _ScriptedLLM("small", 65_000)
    agent = _agent(system_config, _ScriptedLLM("big", 1_000_000), max_steps=2,
                   llm_profile=("big", "small", "mid"))
    agent._create_fallback_llm = lambda profile: small
    _record_hooks(agent)

    events = await _run(agent, llm_override=failing_override, llm_profile_info_override="mid:openai/mid")

    assert small.call_count == 2, "fixture: the swap did not carry step 2"
    assert "mid:" not in _announced(events)[-1], "step 2 was announced as the override that failed"


@pytest.mark.asyncio
async def test_after_a_swap_the_failed_override_is_no_fallback(system_config):
    failing_override = _ServerErrorLLM("mid", 200_000)
    small = _FlakyLLM("small", 65_000, fail_on_call=2)
    built = []

    def build(profile):
        built.append(profile)
        return small if profile == "small" else _ScriptedLLM(profile, 1_000_000)

    agent = _agent(system_config, _ScriptedLLM("big", 1_000_000), max_steps=3,
                   llm_profile=("big", "small", "mid"))
    agent._create_fallback_llm = build
    _record_hooks(agent)

    await _run(agent, llm_override=failing_override, llm_profile_info_override="mid:openai/mid")

    assert small.call_count >= 2, "fixture: 'small' never failed"
    assert "mid" not in built, built


@pytest.mark.asyncio
async def test_a_recovery_to_the_base_it_already_is_strips_nothing(system_config):
    """A 401 made the fallback persistent AND this run's base. When its recovery
    comes due in the same run, the pick returns that very fallback — stripping
    its own reasoning only cost the cache."""
    agent = None

    def age_the_clock():
        agent._fallback_activated_at = time.time() - 99_999

    denied = _TransportErrorLLM(httpx.HTTPStatusError(
        "Client error '401 Unauthorized'", request=httpx.Request("POST", "https://x/v1/chat"),
        response=httpx.Response(401, text="bad key")))
    fallback = _ScriptedLLM("fallback", 65_000, reasoning=True, before_answer=age_the_clock)
    agent = _agent(system_config, denied, max_steps=3)
    agent._create_fallback_llm = lambda profile: fallback
    _record_hooks(agent)

    await _run(agent)

    assert fallback.call_count >= 2, "fixture: the fallback did not carry two steps"
    assert fallback.saw_artifacts[1] is True, "the recovery stripped the reasoning of the model that stays"


class _TransportErrorLLM(_ScriptedLLM):
    """Raises the given error on every call."""

    def __init__(self, exc):
        super().__init__("denied", 1_000_000)
        self._exc = exc

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.call_count += 1
        raise self._exc


class _FlakyLLM(_ScriptedLLM):
    """Scripted, but answers call number fail_on_call with a 504."""

    def __init__(self, name, context_window, fail_on_call):
        super().__init__(name, context_window)
        self._fail_on_call = fail_on_call

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        if self.call_count + 1 == self._fail_on_call:
            self.call_count += 1
            raise LLMServerError("gateway timeout", provider="openai", model=self.name, status_code=504)
        return await super().chat_tools(messages, tools, cancellation_token, status_scope)


class _NamedModelLLM(_ScriptedLLM):
    """Scripted, with the model name real clients carry. Reasoning it leaves
    is tagged with that name; the other doubles stay untagged so the tests
    above keep measuring the switch the step loop sees itself."""

    def __init__(self, name, context_window, reasoning=False):
        super().__init__(name, context_window, reasoning=reasoning)
        self.model = name


async def _second_request_after(agent, between):
    """Request 1 on the persistent fallback, *between* runs, then request 2 of
    the same session. The step model of request 1 is gone by then."""
    agent._active_fallback_llm = agent_fallback = _NamedModelLLM("small", 65_000, reasoning=True)
    agent._active_fallback_profile = "small"
    agent._fallback_activated_at = time.time()
    _record_hooks(agent)
    await _run(agent, session_id="s-cross")
    assert agent_fallback.call_count >= 1, "fixture: request 1 did not run on the fallback"
    between()
    await _run(agent, session_id="s-cross")
    return agent_fallback


@pytest.mark.asyncio
async def test_a_switch_since_the_previous_request_strips_its_reasoning(system_config):
    """Another request recovered from the fallback while this session was idle:
    its strip went to its own history, and this session's next request had no
    step model left to compare with."""
    original = _NamedModelLLM("big", 1_000_000)
    agent = _agent(system_config, original)

    await _second_request_after(agent, between=agent.reset_fallback)

    assert original.call_count >= 1, "fixture: request 2 did not run on the original"
    assert original.saw_artifacts[0] is False, "the fallback's reasoning from request 1 reached the original"


@pytest.mark.asyncio
async def test_reasoning_reaches_the_next_request_when_the_model_stays(system_config):
    """Control: the session carries reasoning across requests, and the same
    model keeps it."""
    original = _NamedModelLLM("big", 1_000_000)
    agent = _agent(system_config, original)

    fallback = await _second_request_after(agent, between=lambda: None)

    assert original.call_count == 0 and fallback.call_count >= 3, "fixture: request 2 left the fallback"
    assert fallback.saw_artifacts[2] is True, "reasoning of the model that stays was stripped"


@pytest.mark.asyncio
async def test_the_final_answer_after_an_escalated_last_step_gets_no_foreign_reasoning(
        system_config, monkeypatch):
    """The call after max_steps picks its model itself (the base, the
    escalation window ended with the loop)."""
    original = _ScriptedLLM("original", 65_000)
    advanced = _ScriptedLLM("advanced", 1_000_000, reasoning=True)
    agent = _agent(system_config, original)
    _open_escalation(agent, monkeypatch, advanced)
    _record_hooks(agent)

    await _run(agent)

    assert advanced.call_count == 1 and original.call_count == 1, "fixture: no escalated step before the final call"
    assert original.saw_artifacts[0] is False, "the advanced model's reasoning reached the final call"


@pytest.mark.asyncio
async def test_the_hooks_see_the_escalation_model(system_config, monkeypatch):
    original = _ScriptedLLM("original", 65_000)
    advanced = _ScriptedLLM("advanced", 1_000_000)
    agent = _agent(system_config, original)

    def open_window(already_advanced):
        escalator = StuckEscalator(enabled=True, rounds=2, max_calls=6)
        escalator.trigger("stuck")
        return escalator

    monkeypatch.setattr(agent, "_create_stuck_escalator", open_window)
    monkeypatch.setattr(agent, "_get_escalation_llm", lambda: advanced)
    seen = _record_hooks(agent)

    await _run(agent)

    assert advanced.call_count >= 1, "fixture: the step was not escalated"
    assert seen["pre"][0][1] is advanced
    assert seen["post"][0][1] is advanced
