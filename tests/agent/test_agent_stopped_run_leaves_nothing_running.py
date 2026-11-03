"""A run stopped while it waits on work of its own leaves none of it running.

The step loop waits on tasks it started -- the pre/post-LLM hooks, the tool
calls -- by polling them while it streams status events. Closed or cancelled at
such a poll (the run was stopped), the task used to run on unowned: a context
summarizer kept billing and could stage messages after the run's final save, a
sub-agent ran on for hours. Plus the small rules beside it: a heartbeat interval
under one poll, and a cancel of the task that collects a run's result.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_system.core.request_context import in_request_tree
from agent_system.llm.message_roles import conversation_messages, is_compaction_system_message
from agent_system.llm.models import ChatMessage, TextContent, task_text
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager, _StepCalls
from agent_system.servers.agent.mixins.llm_loop.llm_call import _polled_call
from agent_system.servers.agent.server import Agent


async def _close_after_first_poll(gen) -> None:
    """Start the generator, let it poll once, then close it as a stopped run does."""
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.25)
    first.cancel()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await first
    await gen.aclose()


@pytest.mark.parametrize("phase, manager_call", [
    ("_run_pre_llm_hooks", "execute_pre_llm_hooks"),
    ("_run_post_llm_hooks", "execute_post_llm_hooks"),
])
async def test_a_hook_task_does_not_outlive_its_step(phase, manager_call):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hooks(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent = SimpleNamespace(_hook_manager=SimpleNamespace(**{manager_call: hooks}))
    run = SimpleNamespace(messages=[], request_id="r1", session_id="s1", main_token=None, max_steps=3,
                          max_steps_note=None, pending_status_events=lambda: [], llm_out=None)
    st = SimpleNamespace(step=0, current_llm=None, final_call=False, assistant_msg=SimpleNamespace(), llm_out=None)

    await _close_after_first_poll(getattr(Agent, phase)(agent, run, st))
    assert started.is_set(), "fixture: the hooks never ran"
    await asyncio.wait_for(cancelled.wait(), 2)


async def test_tool_calls_do_not_outlive_their_step():
    cancelled = asyncio.Event()

    async def runs_for_hours():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.ensure_future(runs_for_hours())
    calls = _StepCalls(step=0, request_id="r1", session_id="s1", user_id=None, status_forwarder=None)
    manager = ToolExecutionManager(registry=None)

    await _close_after_first_poll(manager._stream_until_done(calls, [task]))
    await asyncio.wait_for(cancelled.wait(), 2)


@pytest.mark.parametrize("seconds, expected", [(0, 0), (0.05, 3), (0.1, 3)])
async def test_a_heartbeat_interval_under_one_poll_does_not_divide_by_zero(seconds, expected):
    async def answers_after_three_polls(*_args, **_kwargs):
        await asyncio.sleep(0.25)
        return {"assistant": {"content": "done"}}

    llm = SimpleNamespace(chat_tools=answers_after_three_polls)
    events = [event async for event in _polled_call(
        llm, [], [], cancellation_token=None, step=0, yield_pending_status_fn=lambda: [], status_scope=None,
        wire_format={}, timeouts=None, heartbeat_interval_seconds=seconds)]

    assert events[-1]["type"] == "thinking_complete"
    # 0 turns heartbeats off; under one poll (0.1s) there is one every poll.
    assert len([e for e in events if e["type"] == "heartbeat"]) == expected


async def test_collecting_a_result_lets_a_cancel_through():
    from agent_system.servers.agent.result_utils import collect_final_result

    class Waits:
        name = "waits"

        async def run_events(self, *_args, **_kwargs):
            await asyncio.Event().wait()
            yield {}

    task = asyncio.ensure_future(collect_final_result(Waits(), "do it", session_id="s1"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_request_tree_is_its_root_and_the_ids_under_it():
    assert in_request_tree("r1", "r1")
    assert in_request_tree("r1_003", "r1")
    assert in_request_tree("r1_003_sub_x", "r1")
    assert not in_request_tree("r10", "r1")
    assert not in_request_tree("r1", "r1_003")
    assert not in_request_tree("r1", "")


def test_the_task_text_of_a_message():
    assert task_text("plain") == "plain"
    assert task_text(ChatMessage(role="user", content="hi")) == "hi"
    assert task_text(ChatMessage(role="user", content=[TextContent(text="a"), TextContent(text="b")])) == "a b"
    assert task_text(ChatMessage(role="user", content=[])) == "[multimodal input]"


def test_conversation_keeps_compacted_system_messages_only():
    breadcrumb = ChatMessage(role="system", content='{"type": "pruned_notice", "count": 3}')
    assert is_compaction_system_message(breadcrumb)
    kept = conversation_messages([ChatMessage(role="system", content="prompt"), breadcrumb,
                                  {"role": "user", "content": "hi"}])
    assert kept == [breadcrumb, {"role": "user", "content": "hi"}]
