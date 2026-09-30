"""Conversation scope, all-or-nothing pushes, strict numbers, expiry and the
hook through the real dispatcher -- the bugs found while writing the guide."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.hooks import HookRegistry, HookType
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage
from plugins.cognitive_stack.plugin import PLUGIN_FACTORY
from plugins.cognitive_stack.server import CognitiveStackServer


def make_server(name="cognitive_stack", **settings):
    config = ToolServerConfig(type="cognitive_stack", enabled=True, **settings)
    return CognitiveStackServer(name, AgentSystemConfig(), config)


def push(server, conv, *contexts, **extra):
    return server.execute({"operation": "push_batch", "_session_id": conv,
                           "items": [{"context": c} for c in contexts], **extra})


@pytest.mark.asyncio
async def test_a_second_push_without_stack_id_lands_on_the_same_stack():
    server = make_server()
    first = await push(server, "conv", "outer task")
    second = await push(server, "conv", "interruption")
    assert second["stack_id"] == first["stack_id"]
    assert second["depth"] == 2
    listed = await server.execute({"operation": "list", "_session_id": "conv"})
    assert [f["context"] for f in listed["frames"]] == ["outer task", "interruption"]


@pytest.mark.asyncio
async def test_a_bad_item_pushes_nothing():
    server = make_server()
    await push(server, "conv", "kept")
    result = await server.execute({"operation": "push_batch", "_session_id": "conv",
                                   "items": [{"context": "new"}, {"context": 7}]})
    assert result["status"] == "error"
    assert "non-empty 'context'" in result["error"]
    listed = await server.execute({"operation": "list", "_session_id": "conv"})
    assert [f["context"] for f in listed["frames"]] == ["kept"]


@pytest.mark.asyncio
async def test_data_that_is_no_object_is_refused_and_the_hook_keeps_working():
    server = make_server()
    await push(server, "conv", "task")
    result = await server.execute({"operation": "push_batch", "_session_id": "conv",
                                   "items": [{"context": "x", "data": ["a", "b"]}]})
    assert result["status"] == "error"
    assert "'data' must be an object" in result["error"]
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="conv",
                          messages=[ChatMessage(role="user", content="go")])
    assert (await server.on_pre_llm_call(context)).modified


@pytest.mark.asyncio
async def test_a_refused_push_over_the_limit_creates_no_stack():
    server = make_server(max_depth=5)
    result = await push(server, "conv", *[f"t{i}" for i in range(6)])
    assert result["status"] == "error"
    assert server._stacks == {}
    assert server._agent_session_mapping == {}


@pytest.mark.asyncio
async def test_numbers_are_parsed_strictly():
    server = make_server()
    await push(server, "conv", "a", "b", "c", "d")
    peek = await server.execute({"operation": "peek", "_session_id": "conv", "depth": "2"})
    assert [f["context"] for f in peek["frames"]] == ["c", "d"]
    popped = await server.execute({"operation": "pop_batch", "_session_id": "conv", "count": "2"})
    assert popped["popped_count"] == 2
    for bad in (2.5, True, "two"):
        result = await server.execute({"operation": "pop_batch", "_session_id": "conv", "count": bad})
        assert result["status"] == "error"
        assert "count must be a whole number" in result["error"]
    listed = await server.execute({"operation": "list", "_session_id": "conv"})
    assert listed["depth"] == 2


@pytest.mark.asyncio
async def test_an_expired_stack_is_gone_for_a_read_too():
    server = make_server(session_ttl_seconds=60)
    await push(server, "conv", "old")
    stack_id = server._agent_session_mapping["conv"]
    server._stacks[stack_id].last_accessed -= timedelta(seconds=120)
    result = await server.execute({"operation": "peek", "_session_id": "conv"})
    assert result["status"] == "error"
    assert stack_id not in server._stacks
    assert "conv" not in server._agent_session_mapping


@pytest.mark.asyncio
async def test_clear_leaves_other_conversations_alone():
    server = make_server()
    await push(server, "conv-a", "mine")
    await push(server, "conv-b", "theirs")
    await server.execute({"operation": "clear", "_session_id": "conv-a"})
    refused = await server.execute({"operation": "clear", "_session_id": "conv-c"})
    assert refused["status"] == "error"
    theirs = await server.execute({"operation": "list", "_session_id": "conv-b"})
    assert [f["context"] for f in theirs["frames"]] == ["theirs"]


# --- through the real dispatcher ----------------------------------------------


async def _registry(*names):
    registry = HookRegistry()
    plugins = []
    for name in names:
        plugin = PLUGIN_FACTORY(name, AgentSystemConfig(),
                                ToolServerConfig(type="cognitive_stack", enabled=True))
        # As plugin discovery does: the plugin is duck-typed, the order comes along.
        await registry.register_hook(HookType.PRE_LLM_CALL, f"{name}.inject_stack_context", plugin,
                                     order_spec={"before": [], "after": []})
        plugins.append(plugin)
    return registry, plugins


async def _call(registry, messages, agent=None):
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="conv",
                          agent=agent, messages=messages)
    return (await registry.execute_hooks(HookType.PRE_LLM_CALL, context)).messages


@pytest.mark.asyncio
async def test_the_hook_reads_the_agents_override():
    registry, (plugin,) = await _registry("cognitive_stack")
    await push(plugin.server, "conv", "alpha", "beta", "gamma")  # words the block header never holds
    agent = SimpleNamespace(agent_config=SimpleNamespace(hooks=SimpleNamespace(overrides={
        "cognitive_stack.inject_stack_context": {"enabled": True, "max_frames_in_prompt": 1}})))
    block = (await _call(registry, [ChatMessage(role="user", content="go")], agent))[-1].content
    assert "gamma" in block
    assert "beta" not in block and "alpha" not in block

    # 0 or less shows every frame, not a slice from the bottom.
    agent.agent_config.hooks.overrides["cognitive_stack.inject_stack_context"]["max_frames_in_prompt"] = -1
    block = (await _call(registry, [ChatMessage(role="user", content="go")], agent))[-1].content
    assert "alpha" in block and "beta" in block and "gamma" in block


@pytest.mark.asyncio
async def test_the_hook_does_not_show_an_expired_stack():
    """The next tool call would find it gone: the prompt must not show it."""
    registry, (plugin,) = await _registry("cognitive_stack")
    await push(plugin.server, "conv", "stale")
    stack_id = plugin.server._agent_session_mapping["conv"]
    plugin.server._stacks[stack_id].last_accessed -= timedelta(seconds=plugin.server.session_ttl_seconds + 60)
    agent = SimpleNamespace(agent_config=SimpleNamespace(hooks=SimpleNamespace(overrides={
        "cognitive_stack.inject_stack_context": {"enabled": True}})))
    messages = await _call(registry, [ChatMessage(role="user", content="go")], agent)
    assert all("stale" not in str(m.content) for m in messages)


@pytest.mark.asyncio
async def test_two_instances_keep_their_own_blocks():
    registry, plugins = await _registry("stack_a", "stack_b")
    for plugin in plugins:
        await push(plugin.server, "conv", f"task of {plugin.name}")
    messages = [ChatMessage(role="user", content="go")]
    for _ in range(3):
        messages = await _call(registry, messages)
    blocks = sorted(m.injected_by for m in messages if m.injected_by)
    assert blocks == ["stack_a", "stack_b"]
