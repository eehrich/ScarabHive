"""Numbering, bounded state, conversation scope, hook settings and the texts
that name the tools -- the bugs found while writing the plugin's guide."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.hooks import HookType
from agent_system.hooks.plugin_hook import HookContext
from agent_system.hooks.registry import HookRegistry
from agent_system.llm.models import ChatMessage
from plugins.sequential_thinking.server import SequentialThinkingServer

BASE = {"thought_number": 1, "total_thoughts": 3, "next_thought_needed": True}


def make_server(name="sequential_thinking", **settings):
    config = ToolServerConfig(type="sequential_thinking", enabled=True, **settings)
    return SequentialThinkingServer(name, AgentSystemConfig(), config)


def hook_context(session_id, hook_config=None):
    return HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="req",
        session_id=session_id,
        agent=None,
        messages=[ChatMessage(role="user", content="go")],
        hook_config=hook_config or {},
    )


@pytest.mark.asyncio
async def test_a_new_thought_after_a_revision_takes_the_next_number():
    server = make_server()
    sid = (await server.execute({**BASE, "thought": "one"}))["session_id"]
    await server.execute({**BASE, "thought": "two", "session_id": sid})
    revision = await server.execute({**BASE, "thought": "two again", "session_id": sid,
                                     "is_revision": True, "revises_thought": 2})
    assert revision["current_thought_number"] == 2
    third = await server.execute({**BASE, "thought": "three", "session_id": sid})
    assert third["current_thought_number"] == 3


@pytest.mark.asyncio
async def test_the_memory_limit_bounds_the_branches_too():
    server = make_server(max_history_size=5)
    sid = (await server.execute({**BASE, "thought": "t0"}))["session_id"]
    for i in range(1, 12):
        await server.execute({**BASE, "thought": f"t{i}", "session_id": sid})
    session = server._sessions[sid]
    assert len(session.thoughts) == 5
    assert len(session.branches["main"].thoughts) == 5


@pytest.mark.asyncio
async def test_a_refused_call_creates_no_session():
    server = make_server()
    result = await server.execute({**BASE, "thought": "   ", "_session_id": "conv"})
    assert result["status"] == "error"
    assert server._sessions == {}
    assert server._agent_session_mapping == {}


@pytest.mark.asyncio
async def test_clearing_forgets_the_mapping_and_the_idempotency_keys():
    server = make_server()
    first = await server.execute({**BASE, "thought": "a", "_session_id": "conv",
                                  "idempotency_key": "k1"})
    await server.clear_history({"session_id": first["session_id"]})
    assert server._agent_session_mapping == {}
    assert server._idempotency_cache == {}

    await server.execute({**BASE, "thought": "b", "_session_id": "conv", "idempotency_key": "k2"})
    await server.clear_history({"_session_id": "conv"})
    assert server._agent_session_mapping == {}
    assert server._idempotency_cache == {}


@pytest.mark.asyncio
async def test_trimmed_thoughts_take_their_idempotency_keys_with_them():
    server = make_server(max_history_size=2)
    sid = (await server.execute({**BASE, "thought": "a", "idempotency_key": "old"}))["session_id"]
    for i in range(3):
        await server.execute({**BASE, "thought": f"n{i}", "session_id": sid})
    assert "old" not in server._idempotency_cache


@pytest.mark.asyncio
async def test_an_expired_session_takes_its_idempotency_keys_with_it():
    server = make_server(session_ttl_seconds=60)
    first = await server.execute({**BASE, "thought": "a", "_session_id": "conv",
                                  "idempotency_key": "k"})
    server._sessions[first["session_id"]].last_accessed -= timedelta(seconds=120)
    await server.execute({**BASE, "thought": "b"})  # runs the TTL cleanup
    assert first["session_id"] not in server._sessions
    assert "k" not in server._idempotency_cache
    assert "conv" not in server._agent_session_mapping


@pytest.mark.asyncio
async def test_clear_all_only_touches_the_calling_conversation():
    server = make_server()
    mine = (await server.execute({**BASE, "thought": "mine", "_session_id": "conv-a"}))["session_id"]
    theirs = (await server.execute({**BASE, "thought": "theirs", "_session_id": "conv-b"}))["session_id"]
    result = await server.clear_history({"_session_id": "conv-a"})
    assert result["cleared_sessions"] == 1
    assert mine not in server._sessions
    assert theirs in server._sessions


@pytest.mark.asyncio
async def test_numbers_sent_as_strings_are_taken_as_numbers():
    server = make_server()
    sid = (await server.execute({**BASE, "thought": "a"}))["session_id"]
    revision = await server.execute({**BASE, "thought": "a2", "session_id": sid,
                                     "is_revision": True, "revises_thought": "1"})
    assert revision["status"] == "success", revision
    branch = await server.execute({**BASE, "thought": "b", "session_id": sid,
                                   "branch_from_thought": "1", "branch_id": "alt"})
    assert branch["status"] == "success", branch
    summary = await server.get_summary({"session_id": sid, "max_thoughts": "1"})
    assert summary["status"] == "success", summary
    assert len(summary["thoughts"]) == 1


@pytest.mark.asyncio
async def test_the_hook_reads_the_agents_override():
    server = make_server(max_thoughts_in_prompt=5)
    sid = (await server.execute({**BASE, "thought": "first", "_session_id": "conv"}))["session_id"]
    await server.execute({**BASE, "thought": "second", "session_id": sid, "_session_id": "conv"})

    # The key and shape the registry hands over from the agent's YAML.
    agent = SimpleNamespace(agent_config=SimpleNamespace(hooks=SimpleNamespace(overrides={
        "sequential_thinking.inject_active_sessions": {
            "enabled": True, "max_thoughts_in_prompt": 1, "show_quick_actions": False}})))
    probe = hook_context("conv")
    probe.agent = agent
    settings = HookRegistry()._extract_agent_hook_config(
        probe, "sequential_thinking.inject_active_sessions")

    context = hook_context("conv", settings)
    await server.on_pre_llm_call(context)
    block = context.messages[-1].content
    assert "Thought #2" in block
    assert "Thought #1" not in block
    assert "**Examples:**" not in block


@pytest.mark.asyncio
async def test_the_hints_name_the_tools_of_this_instance():
    server = make_server(name="deep_think", max_sessions_in_prompt=2)
    context = hook_context("conv")
    await server.on_pre_llm_call(context)
    assert "`deep_think()`" in context.messages[-1].content
    assert "sequential_thinking" not in context.messages[-1].content

    await server.execute({**BASE, "thought": "a", "_session_id": "conv"})
    context = hook_context("conv")
    await server.on_pre_llm_call(context)
    assert "`deep_think(session_id=" in context.messages[-1].content
    assert "sequential_thinking" not in context.messages[-1].content

    await server.execute({**BASE, "thought": "b", "session_id": "second", "_session_id": "conv"})
    context = hook_context("conv")
    await server.on_pre_llm_call(context)
    block = context.messages[-1].content
    assert "`deep_think_get_summary(session_id=" in block
    assert "sequential_thinking" not in block


def test_the_summary_default_in_the_schema_follows_the_server_setting():
    server = make_server(max_summary_thoughts=7)
    tools = {t["function"]["name"]: t["function"] for t in server.get_tools()}
    max_thoughts = tools["sequential_thinking_get_summary"]["parameters"]["properties"]["max_thoughts"]
    assert max_thoughts["default"] == 7


@pytest.mark.asyncio
async def test_the_cli_reaches_the_tools(monkeypatch, capsys):
    from plugins.sequential_thinking import __main__ as cli

    monkeypatch.setattr(sys, "argv", ["x", "--thought", "hello", "--no-next-thought-needed"])
    await cli.async_main()
    out = capsys.readouterr().out
    result = json.loads(out[out.index("{"):])
    assert result["status"] == "success", result
    assert result["next_thought_needed"] is False

    monkeypatch.setattr(sys, "argv", ["x", "--operation", "summary",
                                      "--session-id", result["session_id"]])
    await cli.async_main()
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{"):])
    # A fresh server per CLI call: the session is gone, but the tool answered.
    assert summary["status"] == "error"
    assert "not found" in summary["error"]


@pytest.mark.asyncio
async def test_a_refused_revision_or_branch_switch_leaves_no_session():
    server = make_server()
    ghost = await server.execute({**BASE, "thought": "x", "session_id": "ghost", "_session_id": "conv",
                                  "is_revision": True, "revises_thought": 1})
    assert ghost["status"] == "error"
    nope = await server.execute({**BASE, "thought": "x", "_session_id": "conv", "branch_id": "nope"})
    assert nope["status"] == "error"
    fork = await server.execute({**BASE, "thought": "x", "_session_id": "conv",
                                 "branch_from_thought": 3, "branch_id": "alt"})
    assert fork["status"] == "error"
    assert server._sessions == {}
    assert server._agent_session_mapping == {}


@pytest.mark.asyncio
async def test_a_refused_call_does_not_keep_a_session_alive():
    server = make_server()
    sid = (await server.execute({**BASE, "thought": "a"}))["session_id"]
    before = server._sessions[sid].last_accessed - timedelta(seconds=100)
    server._sessions[sid].last_accessed = before
    refused = await server.execute({**BASE, "thought": "x", "session_id": sid, "branch_id": "nope"})
    assert refused["status"] == "error"
    assert server._sessions[sid].last_accessed == before


@pytest.mark.asyncio
async def test_clear_without_a_conversation_spares_the_conversations_sessions():
    server = make_server()
    owned = (await server.execute({**BASE, "thought": "mine", "_session_id": "conv"}))["session_id"]
    loose = (await server.execute({**BASE, "thought": "loose"}))["session_id"]
    result = await server.clear_history({})
    assert result["cleared_sessions"] == 1
    assert loose not in server._sessions
    assert owned in server._sessions


@pytest.mark.asyncio
async def test_trimming_does_not_sweep_every_session():
    server = make_server(max_history_size=2)
    sid = (await server.execute({**BASE, "thought": "a", "idempotency_key": "old"}))["session_id"]
    await server.execute({**BASE, "thought": "b", "session_id": sid, "idempotency_key": "kept"})

    def sweep():
        raise AssertionError("the full sweep ran on the hot path")

    server._forget_removed_state = sweep
    result = await server.execute({**BASE, "thought": "c", "session_id": sid})
    assert result["status"] == "success", result
    assert "old" not in server._idempotency_cache
    assert "kept" in server._idempotency_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2.5, True, "2.5", "two"])
async def test_a_number_that_is_not_whole_is_refused(value):
    server = make_server()
    sid = (await server.execute({**BASE, "thought": "a"}))["session_id"]
    await server.execute({**BASE, "thought": "b", "session_id": sid})
    result = await server.execute({**BASE, "thought": "c", "session_id": sid,
                                   "is_revision": True, "revises_thought": value})
    assert result["status"] == "error"
    assert "revises_thought must be a whole number" in result["error"]
    assert len(server._sessions[sid].thoughts) == 2


@pytest.mark.asyncio
async def test_flags_sent_as_text_are_parsed():
    server = make_server()
    sid = (await server.execute({**BASE, "thought": "a"}))["session_id"]
    result = await server.execute({**BASE, "thought": "b", "session_id": sid, "is_revision": "false",
                                   "revises_thought": "1.0", "next_thought_needed": "false"})
    assert result["status"] == "success", result
    assert result["current_thought_number"] == 2
    assert result["next_thought_needed"] is False
    summary = await server.get_summary({"session_id": sid, "include_branches": "false"})
    assert "branches" not in summary
    bad = await server.execute({**BASE, "thought": "c", "session_id": sid, "is_revision": "maybe"})
    assert bad["status"] == "error"


@pytest.mark.asyncio
async def test_two_instances_with_the_hook_do_not_append_on_every_step():
    first, second = make_server(name="think_a"), make_server(name="think_b")
    context = hook_context("conv")
    for _ in range(3):
        await first.on_pre_llm_call(context)
        await second.on_pre_llm_call(context)
    assert len(context.messages) == 3  # the user message and one block each


@pytest.mark.asyncio
async def test_progress_does_not_count_revisions():
    server = make_server()
    status = AsyncMock()
    sid = (await server.execute({**BASE, "thought": "a", "total_thoughts": 2,
                                 "_session_id": "conv"}))["session_id"]
    await server.execute({**BASE, "thought": "b", "session_id": sid, "total_thoughts": 2})
    for _ in range(3):
        result = await server.execute({**BASE, "thought": "b again", "session_id": sid,
                                       "total_thoughts": 2, "is_revision": True,
                                       "revises_thought": 2, "_status": status})
    assert result["progress"] == "Thought 2/2"
    assert result["total_thoughts_estimate"] == 2
    assert "warnings" not in result
    assert status.end.await_args.args[0].startswith("Thought #2 added (2/2)")
    context = hook_context("conv")
    await server.on_pre_llm_call(context)
    assert "**Progress**: 2/2 thoughts" in context.messages[-1].content


@pytest.mark.asyncio
async def test_zero_thoughts_in_prompt_shows_three_per_session_with_several_sessions():
    server = make_server(max_sessions_in_prompt=2, max_thoughts_in_prompt=0)
    for sid in ("one", "two"):
        for i in range(5):
            await server.execute({**BASE, "thought": f"{sid}-{i}", "session_id": sid,
                                  "_session_id": "conv"})
    context = hook_context("conv")
    await server.on_pre_llm_call(context)
    block = context.messages[-1].content
    assert "one-1" not in block and "one-2" in block
    assert "two-1" not in block and "two-2" in block
