"""A text answer the output cap cut off is not the run's answer.

Measured in a coder session (24.09.2026): three calls in a row stopped at 16384
completion tokens. Each lost the tool call that was writing a whole file, and the
text before it -- an announcement of that file -- ended the run as if it were the
reply; the user had to push three times. The loop now sends it back with a note
where the agent opted in (agent_config.output_cap_notes: that many in a row),
never on the final call. Every other agent gets its cut-off text delivered, as
before: a pipeline agent's product is that text, and a model looping until a
120k cap must not be sent back for two more rounds of it.
"""
import pytest
from unittest.mock import AsyncMock

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.llm.message_roles import DEVELOPER
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry
from test_agent_finish_reason_transport import _llm_system


def _agent(max_steps, output_cap_notes=2):
    agent_config = AgentConfig(max_steps=max_steps, llm_profile="normal", output_cap_notes=output_cap_notes)
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", AgentSystemConfig(llm_system=_llm_system()), server_config, ToolServerRegistry())


def _scripted(answers):
    """One streamed answer per call: (content, finish_reason); finish_reason
    "tool" is a step that calls a tool (one the agent does not have)."""
    seen = []

    async def stream(messages, tools, cancellation_token=None, status_scope=None):
        seen.append(list(messages))
        content, finish = answers[min(len(seen), len(answers)) - 1]
        assistant = {"role": "assistant", "content": content}
        if finish == "tool":
            assistant["tool_calls"] = [{"id": f"c{len(seen)}", "type": "function",
                                        "function": {"name": "write_part", "arguments": "{}"}}]
        yield {"type": "final", "assistant": assistant,
               "usage": {"completion_tokens": 16384 if finish == "length" else 20},
               "finish_reason": "tool_calls" if finish == "tool" else finish}

    llm = AsyncMock()
    llm.supports_streaming = lambda: True
    llm.chat_tools_streaming = stream

    async def no_blocking_call(*args, **kwargs):
        raise AssertionError("chat_tools() must not be reached in a streaming test")

    llm.chat_tools = no_blocking_call
    return llm, seen


def _notes(messages):
    return [m for m in messages if getattr(m, "injected_by", None) == "agent.output_cap"]


async def _run(agent):
    return [e async for e in agent.run_events("write the game", session_id="cap")]


@pytest.mark.asyncio
async def test_a_cut_off_announcement_is_sent_back_not_delivered():
    agent = _agent(max_steps=4)
    agent.llm, seen = _scripted([("Now the engine, the big file:", "length"), ("Done, in three parts.", "stop")])

    events = await _run(agent)

    assert len(seen) == 2, "the cut-off answer ended the run"
    notes = _notes(seen[1])
    assert len(notes) == 1 and notes[0].role == DEVELOPER
    assert "cut off at the output limit (16384 tokens)" in notes[0].content
    # What the model said stays, so it knows where it stopped.
    assert any(m.role == "assistant" and "the big file" in str(m.content) for m in seen[1])
    finals = [e for e in events if e.get("type") == "final"]
    assert finals and "three parts" in finals[-1]["summary"]


@pytest.mark.asyncio
async def test_a_model_that_keeps_hitting_the_cap_is_not_sent_back_forever():
    agent = _agent(max_steps=10)
    agent.llm, seen = _scripted([("part", "length")])

    events = await _run(agent)

    assert len(seen) == 3, f"{len(seen)} calls: two notes, then the answer stands"
    assert [e for e in events if e.get("type") == "final"]


@pytest.mark.asyncio
async def test_a_step_that_ended_on_its_own_starts_the_count_again():
    """Two cut-offs, then a tool call -- the model writing in parts -- then a
    cut-off again: that one is the first of a new row, not the third."""
    agent = _agent(max_steps=10)
    agent.llm, seen = _scripted([("part one", "length"), ("part two", "length"), ("", "tool"),
                                 ("part three", "length"), ("all written", "stop")])

    events = await _run(agent)

    assert len(seen) == 5, f"{len(seen)} calls: the third cut-off ended the run"
    finals = [e for e in events if e.get("type") == "final"]
    assert finals and finals[-1]["summary"].startswith("all written")


@pytest.mark.asyncio
async def test_on_the_call_after_the_step_budget_the_answer_stands():
    """max_steps=1: the one step is sent back with its note, the call after the
    budget has no step left to continue in -- its answer is delivered."""
    agent = _agent(max_steps=1)
    agent.llm, seen = _scripted([("the last word, cut", "length")])

    events = await _run(agent)

    assert len(seen) == 2 and len(_notes(seen[1])) == 1
    finals = [e for e in events if e.get("type") == "final"]
    assert finals and "the last word" in finals[-1]["summary"]


@pytest.mark.asyncio
async def test_an_answer_that_ended_on_its_own_is_the_answer():
    agent = _agent(max_steps=4)
    agent.llm, seen = _scripted([("complete", "stop")])

    await _run(agent)

    assert len(seen) == 1 and not _notes(seen[0])


@pytest.mark.asyncio
async def test_an_agent_that_did_not_opt_in_gets_its_cut_off_text_delivered():
    agent = _agent(max_steps=4, output_cap_notes=0)
    agent.llm, seen = _scripted([("the review, cut off mid", "length")])

    events = await _run(agent)

    assert len(seen) == 1 and not _notes(seen[0])
    finals = [e for e in events if e.get("type") == "final"]
    assert finals and "cut off mid" in finals[-1]["summary"]


def test_the_default_is_off():
    assert AgentConfig().output_cap_notes == 0
