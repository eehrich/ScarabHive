"""The injected sub-agent block is appended, and only when it says something new.

It used to sit right behind the system prompt, where a provider hoists it into
the prompt head and every change invalidated the cache for the whole history.
It is a developer turn at the end now: written when a sub-agent is added,
removed or changes status, never rewritten, and never confused with a message
that merely quotes its header.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_system.config.models import ToolServerConfig
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from plugins.sub_agent_manager.server import SubAgentManagerServer


async def stored_status(metadata):
    """What the injector is told a sub-agent is doing: here, the stored status as it stands."""
    return metadata.get("status", "unknown")


def _sub_agent(n, *, status="active", last_used="2026-09-15T10:00:00Z", messages=3):
    return {"instance_id": f"sub_worker_{n:04d}", "agent_type": "worker", "status": status,
            "created_at": f"2026-09-15T09:00:0{n}Z", "last_used": last_used,
            "message_count": messages, "task_summary": f"task {n}"}


def _context(messages):
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="req", session_id="parent",
                       agent=MagicMock(), agent_name="coordinator", messages=messages, step=1)


def _injector(sub_agents, **config):
    manager = MagicMock()
    manager.list_sub_sessions = AsyncMock(return_value=sub_agents)
    return SubAgentContextInjector(manager, "work_sam", {"format": "markdown", **config}, status_of=stored_status), manager


def _blocks(messages):
    return [m for m in messages if m.injected_by == "sub_agent_manager:work_sam"]


@pytest.mark.parametrize("fmt", ["markdown", "text"])
async def test_the_block_does_not_change_when_a_sub_agent_is_continued(fmt):
    injector, manager = _injector([_sub_agent(1), _sub_agent(2)], format=fmt)
    first = _context([ChatMessage(role="system", content="prompt"),
                      ChatMessage(role="user", content="go")])
    await injector.inject_sub_agent_context(first)

    # sub-agent 1 was continued: newer last_used, more messages
    manager.list_sub_sessions.return_value = [
        _sub_agent(1, last_used="2026-09-15T11:00:00Z", messages=9), _sub_agent(2)]
    second = _context(list(first.messages) + [ChatMessage(role="assistant", content="ok")])
    await injector.inject_sub_agent_context(second)

    assert [m.content for m in second.messages[:3]] == [m.content for m in first.messages[:3]]
    assert len(_blocks(second.messages)) == 1


async def test_a_status_change_appends_a_new_block():
    injector, manager = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])
    await injector.inject_sub_agent_context(context)
    first = _blocks(context.messages)[0]

    manager.list_sub_sessions.return_value = [_sub_agent(1, status="interrupted")]
    result = await injector.inject_sub_agent_context(context)

    assert result.modified is True
    blocks = _blocks(context.messages)
    assert len(blocks) == 2, "the new state is appended, the old one keeps its place"
    assert blocks[0] is first and blocks[0].content == first.content
    assert blocks[-1].content != first.content
    assert context.messages[-1] is blocks[-1]


async def test_an_unchanged_list_is_not_written_again():
    """The whole point: no write, no new prefix, the cache survives."""
    injector, _ = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])
    assert (await injector.inject_sub_agent_context(context)).modified is True

    second = await injector.inject_sub_agent_context(context)

    assert second.modified is False
    assert len(_blocks(context.messages)) == 1


async def test_a_tool_result_quoting_the_header_survives():
    injector, _ = _injector([_sub_agent(1)])
    tool_result = ChatMessage(role="tool", tool_call_id="c1", name="read",
                              content="## Active Sub-Agents\n(a doc that was read)")
    context = _context([ChatMessage(role="system", content="prompt"),
                        ChatMessage(role="assistant", content=None,
                                    tool_calls=[{"id": "c1", "type": "function",
                                                 "function": {"name": "read", "arguments": "{}"}}]),
                        tool_result])

    await injector.inject_sub_agent_context(context)

    assert tool_result in context.messages


async def test_an_emptied_list_is_superseded_not_deleted():
    injector, manager = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])
    await injector.inject_sub_agent_context(context)
    assert _blocks(context.messages), "fixture: nothing was injected"

    manager.list_sub_sessions.return_value = []
    result = await injector.inject_sub_agent_context(context)

    assert result.modified is True
    blocks = _blocks(context.messages)
    assert len(blocks) == 2, "the old list stays; what is gone is said, not erased"
    assert "None" in blocks[-1].content

    # and it is said once, not on every call afterwards
    assert (await injector.inject_sub_agent_context(context)).modified is False
    assert len(_blocks(context.messages)) == 2


async def test_a_task_cannot_break_the_table():
    """The task is what the model wrote: a pipe or a line break in it ends the row early."""
    injector, _ = _injector([{**_sub_agent(1), "task_summary": "compare a | b\nand then c"}])
    context = _context([ChatMessage(role="user", content="go")])

    await injector.inject_sub_agent_context(context)

    assert "| worker | `sub_worker_0001` | active | compare a \\| b and then c |" in _blocks(context.messages)[-1].content


async def test_a_block_persisted_without_a_marker_counts_as_the_previous_one():
    """An old unmarked block is mine: when the list has emptied since, that is news.

    Blocks written before the marker existed were persisted as plain system
    messages, under the header they had then. Not recognising one leaves its
    list of sub-agents as the last word of a resumed session whose sub-agents
    are all archived by now.
    """
    injector, _ = _injector([])
    unmarked = ChatMessage(role="system", content="## Active Sub-Agents\n\n| worker | `sub_worker_0001` | active |")
    context = _context([ChatMessage(role="system", content="prompt"), unmarked,
                        ChatMessage(role="user", content="go")])
    result = await injector.inject_sub_agent_context(context)

    assert result.modified is True, "the unmarked block was not recognised as the previous one"
    assert "None left" in _blocks(context.messages)[-1].content
    assert unmarked in context.messages


async def test_a_stale_unmarked_block_is_superseded_not_rewritten():
    """Recognising it is not keeping it: a list that says something new lands."""
    injector, _ = _injector([_sub_agent(1)])
    stale = ChatMessage(role="system", content="## Active Sub-Agents\n\nstale")
    context = _context([ChatMessage(role="system", content="prompt"), stale,
                        ChatMessage(role="user", content="go")])

    result = await injector.inject_sub_agent_context(context)

    assert result.modified is True
    assert stale in context.messages, "history is not rewritten, not even an old block"
    assert context.messages[-1] is _blocks(context.messages)[-1]


def test_a_reload_lets_the_hook_config_follow():
    """The lazily built hook config is cached -- a reload has to drop it.

    Otherwise the reloaded server answers the same question twice: freshly
    from its own fields, and from a config built before the reload.
    """
    server = SubAgentManagerServer("work_sam", MagicMock(), ToolServerConfig(
        enabled=True, hook_config={"max_sub_agents_shown": 3}))
    assert server.config["max_sub_agents_shown"] == 3, "fixture: builds and caches it"

    server.reload_config(ToolServerConfig(
        enabled=True, hook_config={"max_sub_agents_shown": 1}))

    assert server.config["max_sub_agents_shown"] == 1


async def test_the_hint_names_the_instance_tool():
    injector, _ = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])

    await injector.inject_sub_agent_context(context)

    assert "work_sam_manage_sub_agent(" in _blocks(context.messages)[-1].content


async def test_the_injector_reads_its_options_from_hook_config():
    config = ToolServerConfig(enabled=True, hook_config={
        "inject_sub_agent_context": {"max_sub_agents_shown": 1, "format": "markdown"}})
    server = SubAgentManagerServer("work_sam", MagicMock(), config)
    agent = MagicMock()
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="req", session_id="parent",
                          agent=agent, agent_name="coordinator",
                          messages=[ChatMessage(role="user", content="go")], step=1)
    manager = MagicMock()
    manager.list_sub_sessions = AsyncMock(return_value=[_sub_agent(1), _sub_agent(2), _sub_agent(3)])

    with patch.object(server, "_get_manager", return_value=manager):
        await server.on_pre_llm_call(context)

    block = _blocks(context.messages)[0].content
    assert sum(line.startswith("| worker |") for line in block.splitlines()) == 1, block


def _server(allowed, blocked=(), phase_agents=None):
    config = ToolServerConfig(enabled=True, allowed_agents=list(allowed), blocked_agents=list(blocked),
                       phase_filtering={"enabled": phase_agents is not None,
                                        "phase_agents": phase_agents or {}})
    return SubAgentManagerServer("work_sam", MagicMock(), config)


def test_a_glob_in_allowed_agents_lists_the_agents_it_lets_through(monkeypatch):
    from agent_system.plugins import tool_adapter

    servers = {}
    for name in ("coder_explorer", "coder_reviewer", "other_agent"):
        srv = MagicMock(spec=Agent)
        srv._tool_public, srv._tool_visible = False, True
        servers[name] = MagicMock(plugin_server=srv)
    monkeypatch.setattr(tool_adapter.plugin_tool_registry, "plugin_servers", servers)
    monkeypatch.setattr(tool_adapter.plugin_tool_registry, "list_servers", lambda: list(servers))
    server = _server(["coder_*"])

    listed = server.get_template_vars()["allowed_agents"]

    assert sorted(listed) == ["coder_explorer", "coder_reviewer"]
    assert all(server._is_agent_allowed(name) for name in listed)


@pytest.mark.parametrize("agent, expected", [("blocked_one", "agent_blocked"),
                                             ("elsewhere", "phase_blocked")])
async def test_a_denial_names_the_check_that_denied_it(agent, expected):
    server = _server(["*"], blocked=["blocked_one"],
                     phase_agents={"planning": ["blocked_one", "planner"]})
    caller = MagicMock()
    caller._session_tracker.get_session_template_vars.return_value = {"workflow_phase": "planning"}

    result = await server.manage_sub_agent({"operation": "create", "agent_type": agent,
                                            "task": "t", "_session_id": "parent", "_agent": caller})

    assert result.get("error_type") == expected, result


async def test_a_reload_applies_changed_injector_options():
    server = SubAgentManagerServer("work_sam", MagicMock(), ToolServerConfig(enabled=True, hook_config={
        "inject_sub_agent_context": {"max_sub_agents_shown": 3}}))
    server.reload_config(ToolServerConfig(enabled=True, hook_config={
        "inject_sub_agent_context": {"max_sub_agents_shown": 1}}))
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="req", session_id="parent",
                          agent=MagicMock(), agent_name="coordinator",
                          messages=[ChatMessage(role="user", content="go")], step=1)
    manager = MagicMock()
    manager.list_sub_sessions = AsyncMock(return_value=[_sub_agent(1), _sub_agent(2), _sub_agent(3)])

    with patch.object(server, "_get_manager", return_value=manager):
        await server.on_pre_llm_call(context)

    block = _blocks(context.messages)[0].content
    assert sum(line.startswith("| worker |") for line in block.splitlines()) == 1, block
