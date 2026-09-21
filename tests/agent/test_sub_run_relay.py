"""A run started under a run reaches the stream the viewer watches -- all of it.

Before, only a sub-agent's status lines did (they travel over the process-wide
status bus); its steps, its thinking and its tool calls were read by whoever
started it and went nowhere else. So the chat could show a sub-agent's lines,
never its steps.

Driven through real Agents: the parent's model starts a child run the way
sub_agent_manager does -- `<tool call id>_async_<id>` as its request id,
consumed inside the parent's call -- and the child starts a grandchild the same
way.
"""
import asyncio
import json
from collections import Counter
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.servers.agent.components import status_forwarding
from agent_system.servers.agent.components.status_forwarding import relay_run_event
from agent_system.utils.tree_hierarchy import parse_request_id_hierarchy
from test_reasoning_loop_wiring import _real_agent

PARENT = "prt"
CHILD = f"{PARENT}_007_async_abc123"
GRANDCHILD = f"{CHILD}_009_sub_g1"
STREAMS = ("reasoning_delta", "thinking_delta")


class _AnsweringLLM:
    """Thinks in two pieces, streams its answer in two, and runs `before_answer` first."""

    model = "test/model"

    def __init__(self, answer, before_answer=None):
        self.answer = answer
        self.before_answer = before_answer

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        if self.before_answer is not None:
            await self.before_answer()
        yield {"type": "thinking_delta", "delta": f"{self.answer} thinks "}
        yield {"type": "thinking_delta", "delta": "twice"}
        half = len(self.answer) // 2
        yield {"type": "content_delta", "delta": self.answer[:half], "accumulated": self.answer[:half]}
        yield {"type": "content_delta", "delta": self.answer[half:], "accumulated": self.answer}
        yield {"type": "final", "assistant": {"role": "assistant", "content": self.answer}}


def _agent(name, llm):
    agent = _real_agent()
    agent.name = name
    agent.agent_config.max_steps = 1
    agent.llm = llm
    return agent


async def _started(request_id):
    forwarder = status_forwarding.StatusEventForwarder()
    await forwarder.start_forwarding(request_id)
    return forwarder


@pytest.mark.asyncio
async def test_a_sub_run_reaches_the_parent_stream_once_wrapped_and_placed():
    seen = {}

    async def run_grandchild():
        grandchild = _agent("grandchild_agent", _AnsweringLLM("grandchild answer"))
        seen["grandchild"] = [e async for e in grandchild.run_events(
            "grandchild task", request_id=GRANDCHILD, session_id="relay-grandchild")]

    async def run_child():
        child = _agent("child_agent", _AnsweringLLM("child answer", before_answer=run_grandchild))
        seen["child"] = [e async for e in child.run_events(
            "child task", request_id=CHILD, session_id="relay-child")]
        # An id that merely starts with the same letters is another run.
        stranger = await _started(f"{PARENT}x_001")
        relay_run_event(stranger, {"type": "final", "summary": "not ours"}, "stranger")
        await stranger.stop_forwarding()

    parent = _agent("parent_agent", _AnsweringLLM("parent answer", before_answer=run_child))
    events = [e async for e in parent.run_events("parent task", request_id=PARENT, session_id="relay-parent")]

    assert seen.get("child") and seen.get("grandchild"), "fixture: the sub-runs never ran"
    wrapped = [e for e in events if e["type"] == "sub_run"]

    # Placed by the call that started it, which is what the page anchors to.
    by_run = {}
    for envelope in wrapped:
        by_run.setdefault(envelope["run_id"], []).append(envelope)
    assert set(by_run) == {CHILD, GRANDCHILD}, set(by_run)
    assert {e["spawned_by"] for e in by_run[CHILD]} == {f"{PARENT}_007"}
    assert {e["spawned_by"] for e in by_run[GRANDCHILD]} == {f"{CHILD}_009"}
    assert {e["agent"] for e in by_run[CHILD]} == {"child_agent"}
    # The page indents a run's own lines from its depth: a scope directly under the run
    # is one level below it, on the scale the status tree uses.
    for own in (CHILD, GRANDCHILD):
        assert {e["depth_level"] for e in by_run[own]} == {parse_request_id_hierarchy(f"{own}_001")["depth"] - 1}

    for run_id, own in (("child", CHILD), ("grandchild", GRANDCHILD)):
        inner = [e["event"] for e in by_run[own]]
        kinds = [e["type"] for e in inner]
        # Everything the run said, each once -- the grandchild is not ALSO relayed
        # a second time wrapped inside the child's stream.
        # Token streams are folded, so they are compared by presence; everything
        # else by count.
        relayable = [e["type"] for e in seen[run_id] if e["type"] not in ("status", "sub_run", "heartbeat")]
        assert (Counter(k for k in kinds if k not in STREAMS)
                == Counter(k for k in relayable if k not in STREAMS)), (run_id, kinds, relayable)
        assert {k for k in kinds if k in STREAMS} == {k for k in relayable if k in STREAMS}, (run_id, kinds)
        assert kinds.count("final") == 1 and kinds.count("start") == 1 and kinds[-1] == "end", (run_id, kinds)
        assert any(e["type"] == "thinking" and e.get("step") == 1 and "assistant" not in e for e in inner), \
            f"{run_id}: the marker that opens its step 1 is missing"
        # The two reasoning tokens arrive as ONE event: three sub-agents thinking at
        # once must not put an event per token into the stream.
        reasoning = [e for e in inner if e["type"] == "reasoning_delta"]
        assert len(reasoning) == 1 and reasoning[0]["delta"].endswith("thinks twice"), reasoning
        # The streamed answer too, and the one left is the newest: it carries all of it.
        answering = [e for e in inner if e["type"] == "thinking_delta"]
        assert [e["accumulated"] for e in answering] == [f"{run_id} answer"], answering
        assert not {"status", "sub_run", "heartbeat"} & set(kinds), kinds

    # The parent's own events stay its own.
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1 and "parent answer" in finals[0]["summary"], finals
    assert all("not ours" not in str(e) for e in events), "a run with a lookalike id was relayed"

    # The run that started a sub-run reads the sub-run's own events unwrapped, as
    # before (runs started further down reach it as envelopes, which every consumer of
    # run_events passes over), and what was relayed is a copy: the API renders relayed
    # answers in place.
    assert "final" in [e["type"] for e in seen["child"]]
    [child_step] = [e for e in by_run[CHILD] if e["event"]["type"] == "thinking_complete"]
    [original] = [e for e in seen["child"] if e["type"] == "thinking_complete"]
    child_step["event"]["assistant"]["content"] = "<p>rendered</p>"
    assert original["assistant"]["content"] == "child answer", "the relayed step shares the run's own dict"

    assert not status_forwarding._live_forwarders, "a finished run is still listening"


class _ToolCallFirstLLM(_AnsweringLLM):
    """Opens its answer with a tool call's delta, the way real clients stream one."""

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        await self.before_answer()
        yield {"type": "tool_call_delta", "index": 0, "delta": {}}
        yield {"type": "final", "assistant": {"role": "assistant", "content": self.answer}}


class _NonStreamingLLM(_AnsweringLLM):
    """Answers in one piece; the loop polls it meanwhile."""

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, **kwargs):
        await self.before_answer()
        return {"assistant": {"role": "assistant", "content": self.answer}}


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_llm", [_ToolCallFirstLLM, _NonStreamingLLM])
async def test_a_sub_agent_working_while_its_caller_waits_for_its_model_reaches_the_stream(parent_llm):
    """An async sub-agent works on while its caller asks its model the next thing. What
    it says meanwhile is collected by the caller's call and has to come out of it."""
    seen = {}

    async def run_child():
        child = _agent("child_agent", _AnsweringLLM("child answer"))
        seen["child"] = [e async for e in child.run_events("child task", request_id=CHILD, session_id="wait-child")]

    parent = _agent("parent_agent", parent_llm("parent answer", before_answer=run_child))
    events = [e async for e in parent.run_events("parent task", request_id=PARENT, session_id="wait-parent")]

    assert seen.get("child"), "fixture: the sub-run never ran"
    kinds = [e["event"]["type"] for e in events if e["type"] == "sub_run" and e["run_id"] == CHILD]
    assert kinds and kinds[0] == "start" and "final" in kinds and kinds[-1] == "end", kinds


def _sam():
    from agent_system.config import AgentSystemConfig, ToolServerConfig
    from plugins.sub_agent_manager.server import SubAgentManagerServer

    server_config = Mock(spec=ToolServerConfig)
    server_config.max_sub_agents_per_session = 10
    server_config.max_nesting_depth = 5
    server_config.max_sub_agents_per_type = 3
    server_config.allowed_agents = ["*"]
    server_config.blocked_agents = []
    return SubAgentManagerServer(name="sub_agent_manager", system_config=Mock(spec=AgentSystemConfig),
                                 server_config=server_config)


class _FailingLLM(_AnsweringLLM):
    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        raise ValueError("the helper's model is down")
        yield  # pragma: no cover -- an async generator that fails on its first step


@pytest.mark.asyncio
async def test_a_sub_agent_read_the_way_sub_agent_manager_reads_it_reaches_the_stream_to_its_last_word():
    """sub_agent_manager stops reading a run at its end, and at its error or cancel.
    Relayed after the yield, the event it stops at would never be relayed: no sub-agent
    would show its end, and a failed one no error."""
    sam, manager = _sam(), Mock()
    manager.update_sub_agent_activity = AsyncMock()
    fine, failing = f"{PARENT}_003_async_fine01", f"{PARENT}_004_async_fail01"
    said = {}

    async def run_both():
        for request_id, llm in ((fine, _AnsweringLLM("fine answer")), (failing, _FailingLLM("never"))):
            said[request_id] = await sam._consume_run(
                _agent("child_agent", llm), manager, parent_session_id="relay-sam",
                instance_id=f"sam-{request_id}", task="task", request_id=request_id)

    parent = _agent("parent_agent", _AnsweringLLM("parent answer", before_answer=run_both))
    events = [e async for e in parent.run_events("parent task", request_id=PARENT, session_id="relay-sam")]

    assert said.get(fine) == "fine answer" and said.get(failing, "").startswith("Error:"), f"fixture: {said}"
    kinds = {run: [e["event"]["type"] for e in events if e["type"] == "sub_run" and e["run_id"] == run]
             for run in (fine, failing)}
    assert kinds[fine][-1] == "end", kinds[fine]
    assert kinds[failing] and kinds[failing][-1] == "error", kinds[failing]


@pytest.mark.asyncio
async def test_a_caller_cannot_choose_an_id_in_line_with_a_run_in_flight():
    """Every stream above a run takes what starts with the run's id and `_`. A client
    choosing `<someone's live id>_003` for its own run would be listening in on that
    run's status lines and sub-agents; one choosing `reportjob` while `reportjob_1`
    works would take that run's status lines as its own. The guard every
    client-chosen id passes refuses both -- and only while the other run works."""
    from fastapi import HTTPException

    from agent_system.app import _validate_client_request_id

    live = [await _started("victimrun1"), await _started("reportjob_1")]
    try:
        for in_line in ("victimrun1_003", "reportjob"):
            with pytest.raises(HTTPException) as refused:
                await _validate_client_request_id(in_line)
            assert refused.value.status_code == 409, in_line
        # an id that merely starts with the same letters is anybody's
        assert await _validate_client_request_id("victimrun12_003") == "victimrun12_003"
        assert await _validate_client_request_id("reportjob_12") == "reportjob_12"
    finally:
        for forwarder in live:
            await forwarder.stop_forwarding()
    assert await _validate_client_request_id("victimrun1_003") == "victimrun1_003"
    assert await _validate_client_request_id("reportjob") == "reportjob"


@pytest.mark.asyncio
async def test_a_run_reusing_an_ended_runs_id_gets_nothing_of_the_old_runs_sub_agents():
    """The writer dispatches a job again under its old id, while a sub-agent the old run
    never waited for is still at work. Neither it nor a run it starts afterwards is the
    new run's -- the ids say they are, the order they began in says they are not -- so
    none of their run events are relayed to it. (Their status lines still are: the
    status bus goes by the id alone. Both runs are the same job's.) A run's last word
    comes after its forwarder has stopped (its finalize stops it), and still goes where
    the run's other events went."""
    left_over = await _started(CHILD)  # the old run is gone, its async sub-agent works on
    again = await _started(PARENT)
    later = await _started(f"{CHILD}_003_sub_b")  # started by the left-over after that
    own = await _started(f"{PARENT}_002_async_new")
    try:
        relay_run_event(left_over, {"type": "final", "summary": "the old run's helper"}, "child_agent")
        relay_run_event(later, {"type": "final", "summary": "its helper"}, "grand_agent")
        relay_run_event(own, {"type": "final", "summary": "mine"}, "child_agent")
        await own.stop_forwarding()
        relay_run_event(own, {"type": "end"}, "child_agent")
        await left_over.stop_forwarding()
        relay_run_event(left_over, {"type": "end"}, "child_agent")
        got = [(e["run_id"], e["event"]["type"]) for e in again.get_pending_events()]
        assert got == [(own.request_id, "final"), (own.request_id, "end")], got
    finally:
        for forwarder in (later, again, left_over):
            await forwarder.stop_forwarding()


@pytest.mark.asyncio
async def test_a_run_whose_id_a_live_one_extends_gets_none_of_its_sub_runs():
    """`reportjob` begun while `reportjob_1` works: by the ids, `reportjob_1` would be a
    run under `reportjob`, but it began first, and its sub-runs' events are its own.
    (A client choosing an id in line with a live one is refused at the door.)"""
    victim = await _started("reportjob_1")
    prefix = await _started("reportjob")
    sub = await _started("reportjob_1_003_async_x")
    try:
        relay_run_event(sub, {"type": "tool_result", "result": "the victim's data"}, "child_agent")
        assert prefix.get_pending_events() == []
        assert [e["run_id"] for e in victim.get_pending_events()] == [sub.request_id]
    finally:
        for forwarder in (sub, prefix, victim):
            await forwarder.stop_forwarding()


HIERARCHY = {"parent_id": f"{PARENT}_007", "depth": 2}


def test_what_a_run_says_between_two_of_its_deltas_is_not_overtaken_by_the_second():
    """Folding a delta into the one still queued moves it to that one's place: across
    anything else the run said in between, it would arrive before it."""
    forwarder = status_forwarding.StatusEventForwarder()
    for event in ({"type": "reasoning_delta", "step": 1, "delta": "before "},
                  {"type": "tool_call", "step": 1, "action": "search"},
                  {"type": "reasoning_delta", "step": 1, "delta": "after"}):
        forwarder.add_sub_run_event(CHILD, HIERARCHY, "child_agent", event)
    said = [(e["event"]["type"], e["event"].get("delta")) for e in forwarder.get_pending_events()]
    assert said == [("reasoning_delta", "before "), ("tool_call", None), ("reasoning_delta", "after")], said


def test_a_delta_after_its_run_was_handed_on_is_sent_on_its_own():
    """The caller hands its queue on every few milliseconds while a sub-agent thinks. A
    token folded into an envelope already sent would never be seen."""
    forwarder = status_forwarding.StatusEventForwarder()
    forwarder.add_sub_run_event(CHILD, HIERARCHY, "child_agent", {"type": "reasoning_delta", "step": 1, "delta": "a"})
    sent = json.dumps(forwarder.get_pending_events())
    forwarder.add_sub_run_event(CHILD, HIERARCHY, "child_agent", {"type": "reasoning_delta", "step": 1, "delta": "b"})
    assert [e["event"]["delta"] for e in forwarder.get_pending_events()] == ["b"]
    assert [e["event"]["delta"] for e in json.loads(sent)] == ["a"]


class _InterleavingLLM(_AnsweringLLM):
    """Thinks token by token, handing the loop on after each, and notes each in `order`.

    Starts only when its sibling is ready too (`together`): the two runs' setup awaits
    threads, and one that got there first would stream all its tokens alone."""

    def __init__(self, answer, order, together):
        super().__init__(answer)
        self.order = order
        self.together = together

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        await self.together.wait()
        for token in ("one ", "two ", "three ", "four"):
            self.order.append(self.answer)
            yield {"type": "thinking_delta", "delta": token}
            await asyncio.sleep(0)
        yield {"type": "final", "assistant": {"role": "assistant", "content": self.answer}}


@pytest.mark.asyncio
async def test_sub_runs_thinking_side_by_side_are_folded_each_on_its_own():
    """Two sub-agents at once take turns token by token. Folded into "the last queued
    entry" nothing would ever fold, and the stream would carry one event per token."""
    ids = [f"{PARENT}_00{n}_async_side{n}" for n in (3, 4)]
    order = []
    together = asyncio.Barrier(len(ids))

    async def run_both():
        async def one(request_id):
            child = _agent("child_agent", _InterleavingLLM(request_id, order, together))
            return [e async for e in child.run_events("task", request_id=request_id, session_id=f"side-{request_id}")]
        await asyncio.gather(*(one(request_id) for request_id in ids))

    parent = _agent("parent_agent", _AnsweringLLM("parent answer", before_answer=run_both))
    events = [e async for e in parent.run_events("parent task", request_id=PARENT, session_id="relay-side")]

    for request_id in ids:
        mine = [i for i, run in enumerate(order) if run == request_id]
        assert any(run != request_id for run in order[mine[0]:mine[-1]]), f"fixture: the sub-runs did not interleave: {order}"
        reasoning = [e["event"] for e in events
                     if e["type"] == "sub_run" and e["run_id"] == request_id and e["event"]["type"] == "reasoning_delta"]
        assert [e["delta"] for e in reasoning] == ["one two three four"], (request_id, reasoning)
