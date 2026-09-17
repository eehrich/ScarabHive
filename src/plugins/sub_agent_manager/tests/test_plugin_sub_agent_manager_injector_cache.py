"""The injected sub-agent block sits right behind the system prompt.

Every change to it invalidates the provider cache for the whole history, so it
may only change when a sub-agent is added, removed or changes status -- and it
must replace exactly its own message, nothing that merely quotes its header.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_system.config.models import ToolServerConfig
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from plugins.sub_agent_manager.server import SubAgentManagerServer


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
    return SubAgentContextInjector(manager, "work_sam", {"format": "markdown", **config}), manager


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


async def test_a_status_change_changes_the_block():
    injector, manager = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])
    await injector.inject_sub_agent_context(context)
    before = _blocks(context.messages)[0].content

    manager.list_sub_sessions.return_value = [_sub_agent(1, status="interrupted")]
    await injector.inject_sub_agent_context(context)

    assert _blocks(context.messages)[0].content != before


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


async def test_an_emptied_list_removes_the_block():
    injector, manager = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])
    await injector.inject_sub_agent_context(context)
    assert _blocks(context.messages), "fixture: nothing was injected"

    manager.list_sub_sessions.return_value = []
    result = await injector.inject_sub_agent_context(context)

    assert result.modified is True
    assert not _blocks(context.messages)


async def test_a_block_persisted_without_a_marker_is_replaced():
    injector, _ = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="system", content="prompt"),
                        ChatMessage(role="system", content="## Active Sub-Agents\n\nstale"),
                        ChatMessage(role="user", content="go")])

    await injector.inject_sub_agent_context(context)

    assert [m.content for m in context.messages if "Active Sub-Agents" in (m.content or "")] \
        == [_blocks(context.messages)[0].content]


async def test_the_hint_names_the_instance_tool():
    injector, _ = _injector([_sub_agent(1)])
    context = _context([ChatMessage(role="user", content="go")])

    await injector.inject_sub_agent_context(context)

    assert "work_sam_manage_sub_agent(" in _blocks(context.messages)[0].content


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
