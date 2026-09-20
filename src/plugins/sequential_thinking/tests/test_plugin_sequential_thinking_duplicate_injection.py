"""The thinking state is appended when it changes, and never rewritten.

Until 18.09.2026 the hook deleted its previous block and inserted a fresh one
behind the system prompt on every call. A text that is rebuilt every step then
sits in the prompt HEAD, and the cached prefix behind it is invalid on every
call. The state is a turn now: appended at the end, left alone afterwards, and
written again only when it says something new.
"""
import pytest
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage
from plugins.sequential_thinking.server import SequentialThinkingServer

MARKER = "sequential_thinking"


@pytest.fixture
def server(tmp_path):
    """Create SequentialThinkingServer instance for testing."""
    from agent_system.config import AgentSystemConfig, ToolServerConfig

    config = AgentSystemConfig(data_dir=tmp_path)
    # Flat, not under plugin_config: the server reads these off the config
    # object itself (ToolServerConfig allows extra fields), so nested they
    # were read by nobody and the fixture pinned nothing.
    server_config = ToolServerConfig(
        name="sequential_thinking",
        max_history_size=100,
        session_ttl_seconds=3600,
    )

    return SequentialThinkingServer("sequential_thinking", config, server_config)


def blocks(context):
    return [msg for msg in context.messages
            if getattr(msg, "injected_by", None) == MARKER]


@pytest.mark.asyncio
async def test_an_unchanged_state_is_not_written_again(server):
    """The second call adds nothing -- that is what keeps the prefix cached."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_001",
        session_id="test_agent_session_1",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
    )

    first = await server.on_pre_llm_call(context)
    assert first.modified is True
    assert len(blocks(context)) == 1
    assert context.messages[-1].role == DEVELOPER

    second = await server.on_pre_llm_call(context)

    assert second.modified is False, "nothing changed, so nothing may be written"
    assert len(blocks(context)) == 1


@pytest.mark.asyncio
async def test_a_started_session_is_appended_behind_the_reminder(server):
    """The reminder stays where it is; the session state follows it."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_002",
        session_id="test_agent_session_2",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Let's think about this")
        ]
    )

    await server.on_pre_llm_call(context)
    reminder = blocks(context)[0]
    assert "## Sequential Thinking Tool Available" in reminder.content

    result = await server.execute({
        "thought": "First thought about the problem",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_session_id": "test_agent_session_2"
    })
    assert result["status"] == "success"

    second = await server.on_pre_llm_call(context)

    assert second.modified is True
    current = blocks(context)
    assert len(current) == 2
    assert current[0] is reminder and current[0].content == reminder.content
    assert "## Active Sequential Thinking Session" in current[-1].content
    assert context.messages[-1] is current[-1], "the newest state is the last word"


@pytest.mark.asyncio
async def test_a_block_that_compaction_removed_comes_back(server):
    """Gone is the same case as never written."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_003",
        session_id="test_agent_session_3",
        messages=[ChatMessage(role="system", content="You are a helpful assistant.")]
    )

    await server.on_pre_llm_call(context)
    assert len(blocks(context)) == 1

    context.messages = [msg for msg in context.messages
                        if getattr(msg, "injected_by", None) != MARKER]
    context.messages.append(ChatMessage(role="user", content="still there?"))
    result = await server.on_pre_llm_call(context)

    assert result.modified is True
    assert len(blocks(context)) == 1


@pytest.mark.asyncio
async def test_five_calls_with_nothing_happening_write_one_block(server):
    """A hook that runs every step must not grow the history by itself."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_004",
        session_id="test_agent_session_4",
        messages=[ChatMessage(role="system", content="You are a helpful assistant.")]
    )

    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Message {i}"))
        result = await server.on_pre_llm_call(context)
        assert result.success is True

    assert len(blocks(context)) == 1


@pytest.mark.asyncio
async def test_the_block_goes_to_the_end_not_behind_the_system_prompt(server):
    """Position is the whole point: at the head it invalidates the cache."""
    context = HookContext(
        hook_type="inject_active_sessions",
        request_id="test_req_005",
        session_id="test_agent_session_5",
        messages=[
            ChatMessage(role="system", content="Main system prompt."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
    )

    await server.on_pre_llm_call(context)

    assert context.messages[-1] is blocks(context)[-1]
    assert [msg.content for msg in context.messages[:3]] == [
        "Main system prompt.", "Hello", "Hi there!"], \
        "everything that was there before must stay byte-identical"
