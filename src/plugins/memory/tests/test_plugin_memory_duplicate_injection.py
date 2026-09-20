"""The memory list is appended when it changes, and never rewritten.

Until 18.09.2026 the hook deleted its previous block and inserted a fresh one
behind the system prompt on every call. A text rebuilt every step then sits in
the prompt HEAD, and the cached prefix behind it is invalid on every call. The
list is a turn now: appended at the end, left alone afterwards, and written
again only when it says something new.
"""
import uuid

import pytest
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage
from plugins.memory.server import MemoryServer

MARKER = "memory"


@pytest.fixture
def server(tmp_path):
    """Create MemoryServer instance for testing."""
    from agent_system.config import AgentSystemConfig, ToolServerConfig

    config = AgentSystemConfig(data_dir=tmp_path)
    # Flat, not under plugin_config: the server reads storage_path off the
    # config object itself, so nested it was ignored and every run wrote into
    # the repository's own data/memories.
    server_config = ToolServerConfig(
        name="memory",
        storage_path=str(tmp_path / "memories"),
    )

    return MemoryServer("memory", config, server_config)


def blocks(context):
    return [msg for msg in context.messages
            if getattr(msg, "injected_by", None) == MARKER]


async def store(server, session_id, title, content):
    await server.execute({
        "operation": "store",
        "title": title,
        "content": content,
        "tags": ["test"],
        "_session_id": session_id,
    })


def a_context(session_id, request_id="req"):
    return HookContext(
        hook_type="pre_llm_call",
        request_id=request_id,
        session_id=session_id,
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Tell me what you know"),
        ],
    )


@pytest.mark.asyncio
async def test_an_unchanged_list_is_not_written_again(server):
    """The second call adds nothing -- that is what keeps the prefix cached."""
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    await store(server, session_id, "Test Memory 1", "Important information")
    context = a_context(session_id)

    first = await server.on_pre_llm_call(context)
    assert first.modified is True
    assert len(blocks(context)) == 1
    assert context.messages[-1].role == DEVELOPER

    second = await server.on_pre_llm_call(context)

    assert second.modified is False, "nothing changed, so nothing may be written"
    assert len(blocks(context)) == 1


@pytest.mark.asyncio
async def test_a_changed_list_is_appended_behind_the_old_one(server):
    """The old state stays put: rewriting it would break the prefix again."""
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    context = a_context(session_id)

    assert (await server.on_pre_llm_call(context)).modified is False  # nothing to say yet

    await store(server, session_id, "New Memory", "Newly stored information")
    assert (await server.on_pre_llm_call(context)).modified is True
    first_block = blocks(context)[-1]
    assert "New Memory" in first_block.content

    await store(server, session_id, "Second Memory", "Another piece of information")
    assert (await server.on_pre_llm_call(context)).modified is True

    current = blocks(context)
    assert len(current) == 2, "the new state is appended, the old one is not touched"
    assert current[0] is first_block and current[0].content == first_block.content
    assert "New Memory" in current[-1].content and "Second Memory" in current[-1].content
    assert context.messages[-1] is current[-1], "the newest state is the last word"


@pytest.mark.asyncio
async def test_the_block_goes_to_the_end_not_behind_the_system_prompt(server):
    """Position is the whole point: at the head it invalidates the cache."""
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    await store(server, session_id, "Positional Memory", "Content")
    context = HookContext(
        hook_type="pre_llm_call",
        request_id="req_pos",
        session_id=session_id,
        messages=[
            ChatMessage(role="system", content="Main system prompt."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!"),
        ],
    )

    await server.on_pre_llm_call(context)

    assert context.messages[-1] is blocks(context)[-1]
    assert [msg.content for msg in context.messages[:3]] == [
        "Main system prompt.", "Hello", "Hi there!"], \
        "everything that was there before must stay byte-identical"


@pytest.mark.asyncio
async def test_a_block_that_compaction_removed_comes_back(server):
    """Gone is the same case as never written."""
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    await store(server, session_id, "Durable Memory", "Content")
    context = a_context(session_id)

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
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    await store(server, session_id, "Stable Memory", "Content")
    context = a_context(session_id)

    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Message {i}"))
        result = await server.on_pre_llm_call(context)
        assert result.success is True

    assert len(blocks(context)) == 1


@pytest.mark.asyncio
async def test_the_query_is_what_a_person_wrote_not_the_last_note(server):
    """Since the block moved to the end, messages[-1] is this hook's own text.

    Selecting the memories by that text makes the injection its own input: the
    list re-ranks itself every step, differs every time, and a new block is
    appended on every single call.
    """
    session_id = f"test_session_{uuid.uuid4().hex[:8]}"
    # More memories than the block shows (max_memories is 10), so the two
    # queries cannot select the same SET and be told apart only by the order
    # a ranking happens to produce.
    for i in range(16):
        await store(server, session_id, f"Memory {i}", f"content about topic {i}")
    context = a_context(session_id)

    first = await server.on_pre_llm_call(context)
    assert first.modified is True
    written = blocks(context)[-1].content

    # what the loop does next: a note of its own at the very end
    context.messages.append(ChatMessage(
        role=DEVELOPER, content="Step 3 of 8: 5 steps left after this one.",
        injected_by="agent.step_budget"))
    second = await server.on_pre_llm_call(context)

    assert second.modified is False, (
        "the memory list was selected by a note instead of by the user turn")
    assert len(blocks(context)) == 1
    assert blocks(context)[-1].content == written
