"""The task list is appended when it changes, and never rewritten.

Until 18.09.2026 the hook deleted its previous block and inserted a fresh one
behind the system prompt on every call. That put a text which is rebuilt every
step into the prompt HEAD, so the cached prefix behind it was invalid on every
call. The list is a turn now: appended at the end, left alone afterwards, and
only written again when it says something new.
"""
import pytest
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage
from plugins.todo.server import TodoServer


@pytest.fixture
def server(tmp_path):
    """Create TodoServer instance for testing."""
    from agent_system.config import AgentSystemConfig, ToolServerConfig

    config = AgentSystemConfig(data_dir=tmp_path)
    # Flat, not under plugin_config: the server reads these off the config
    # object itself. Nested they were silently ignored, and every run of these
    # tests wrote into the repository's own data/todos -- where the leftovers
    # of the previous run then decided what the next one saw.
    server_config = ToolServerConfig(
        name="todo",
        storage_path=str(tmp_path / "todos"),
        max_tasks_per_session=20,
    )

    return TodoServer("todo", config, server_config)


def todo_blocks(context):
    return [msg for msg in context.messages
            if getattr(msg, "injected_by", None) == "todo"]


@pytest.mark.asyncio
async def test_an_unchanged_list_is_not_written_again(server):
    """The second call adds nothing -- that is what keeps the prefix cached."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_001",
        session_id="test_session_123",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
    )

    first = await server.on_pre_llm_call(context)
    assert first.modified is True
    assert len(todo_blocks(context)) == 1

    second = await server.on_pre_llm_call(context)

    assert second.modified is False, "nothing changed, so nothing may be written"
    assert len(todo_blocks(context)) == 1


@pytest.mark.asyncio
async def test_the_block_is_a_turn_at_the_end_not_a_block_at_the_head(server):
    """Position is the whole point: at the head it invalidates the cache."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_004",
        session_id="test_session_pos",
        messages=[
            ChatMessage(role="system", content="Main system prompt."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
    )

    await server.on_pre_llm_call(context)

    assert context.messages[-1] is todo_blocks(context)[-1]
    assert context.messages[-1].role == DEVELOPER
    assert [msg.content for msg in context.messages[:3]] == [
        "Main system prompt.", "Hello", "Hi there!"], \
        "everything that was there before must stay byte-identical"


@pytest.mark.asyncio
async def test_a_changed_list_is_appended_behind_the_old_one(server):
    """The old state stays put: rewriting it would break the prefix again."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_002",
        session_id="test_session_456",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Create a task")
        ]
    )

    await server.on_pre_llm_call(context)
    reminder = todo_blocks(context)[0]
    assert "## TODO Tool Available" in reminder.content

    await server.create_todo(
        title="Test task",
        description="Test description",
        priority="high",
        context={"session_id": "test_session_456"}
    )
    result = await server.on_pre_llm_call(context)

    assert result.modified is True
    blocks = todo_blocks(context)
    assert len(blocks) == 2, "the new state is appended, the old one is not touched"
    assert blocks[0] is reminder and blocks[0].content == reminder.content
    assert "Current active tasks:" in blocks[-1].content
    assert context.messages[-1] is blocks[-1], "the newest state is the last word"


@pytest.mark.asyncio
async def test_a_block_that_compaction_removed_comes_back(server):
    """Rule one: gone is the same case as never written."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_003",
        session_id="test_session_789",
        messages=[ChatMessage(role="system", content="You are a helpful assistant.")]
    )

    await server.on_pre_llm_call(context)
    assert len(todo_blocks(context)) == 1

    # what compaction does: the note is bound to a moment and may be archived
    context.messages = [msg for msg in context.messages
                        if getattr(msg, "injected_by", None) != "todo"]
    context.messages.append(ChatMessage(role="user", content="still there?"))
    result = await server.on_pre_llm_call(context)

    assert result.modified is True
    assert len(todo_blocks(context)) == 1


@pytest.mark.asyncio
async def test_five_calls_with_nothing_happening_write_one_block(server):
    """A hook that runs every step must not grow the history by itself."""
    context = HookContext(
        hook_type="inject_todo_tasks",
        request_id="test_req_005",
        session_id="test_session_stable",
        messages=[ChatMessage(role="system", content="You are a helpful assistant.")]
    )

    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Message {i}"))
        result = await server.on_pre_llm_call(context)
        assert result.success is True

    assert len(todo_blocks(context)) == 1


class TestTheHookConfiguration:
    """Where the injected block's options come from, in the order they win."""

    @staticmethod
    def _context(agent=None):
        return HookContext(
            hook_type="inject_todo_tasks",
            request_id="req_cfg",
            session_id="session_cfg",
            agent=agent,
            messages=[ChatMessage(role="user", content="go")],
        )

    @pytest.mark.asyncio
    async def test_the_instance_setting_beats_the_schema_default(self, tmp_path):
        """`hook_config:` in plugins.yaml is where the operator says otherwise.

        todo used to read the schema defaults through a private copy of the
        extractor, so this block was read by nobody.
        """
        from agent_system.config import AgentSystemConfig, ToolServerConfig

        server = TodoServer("todo", AgentSystemConfig(data_dir=tmp_path),
                            ToolServerConfig(name="todo",
                                             storage_path=str(tmp_path / "todos"),
                                             hook_config={"format": "text"}))
        await server.create_todo(title="Write the report", priority="high",
                                 context={"session_id": "session_cfg"})
        context = self._context()

        await server.on_pre_llm_call(context)

        block = todo_blocks(context)[-1].content
        assert block.startswith("=== TODO Tool Available ==="), (
            f"the markdown default was used although format=text was set: {block}")
        assert "Write the report" in block

    @pytest.mark.asyncio
    async def test_an_agents_own_override_wins(self, server):
        """`hooks.overrides` reaches the hook as context.hook_config.

        The README has offered those keys since before this hook existed, and
        the registry fills them in -- but the hook read only its own config,
        so everything an agent set there was silently ignored.
        """
        for n in range(3):
            await server.create_todo(title=f"Task {n}", priority="medium",
                                     context={"session_id": "session_cfg"})
        context = self._context()
        context.hook_config = {"max_tasks": 1}

        await server.on_pre_llm_call(context)

        block = todo_blocks(context)[-1].content
        listed = sum(1 for line in block.splitlines() if "Task " in line)
        assert listed == 1, f"max_tasks=1 did not arrive, {listed} tasks listed: {block}"
