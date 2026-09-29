"""What a run hands back and what the tools answer: the conversation it stages, the calls still running, the
summaries' marker, a manual run's overrides, and the figures check_stats recommends by.

Driven through the plugin's own tool entry (``plugin.call``) with an agent stub that holds a live message list, as
the agent loop does mid-run; only the summarizing LLM is stubbed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.llm.message_roles import opens_a_turn
from agent_system.llm.models import ChatMessage
from plugins.context_summarizer.plugin import PLUGIN_FACTORY

PROMPT = "You are the agent."
PENDING = {"id": "call-1", "type": "function",
           "function": {"name": "context_summarizer_summarize", "arguments": "{}"}}


def conversation(count: int) -> list[ChatMessage]:
    return [ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"Message {i} " + "words " * 100)
            for i in range(count)]


@pytest.fixture
def plugin():
    plugin = PLUGIN_FACTORY("context_summarizer", AgentSystemConfig(), ToolServerConfig())
    plugin.server._hooks_impl._summarizer_llm = AsyncMock(model_name="stub", chat=AsyncMock(return_value="short"))
    return plugin


def agent_with(messages, *, overrides=None, latest=None):
    agent = Mock()
    agent.get_live_messages = Mock(return_value=messages)
    agent.next_internal_tool_request_id = AsyncMock(return_value="summary-request")
    agent.llm_for_session = Mock(return_value=Mock(context_window=1_000_000))
    agent.agent_config = SimpleNamespace(hooks=SimpleNamespace(overrides=overrides or {}))
    agent.get_live_tools_schema = Mock(return_value=[])
    tracker = Mock(get_latest=Mock(return_value=latest))
    agent.system_config = SimpleNamespace(tool_registry=Mock(get_server=Mock(return_value=Mock(tracker=tracker))))
    return agent


async def summarize(plugin, agent, **params):
    return await plugin.call("context_summarizer_summarize", {"_session_id": "s-1", "_agent": agent, **params})


def staged(agent) -> list[ChatMessage]:
    return agent._session_tracker.set_compacted_messages.call_args[0][1]


async def test_a_manual_run_keeps_the_call_it_runs_in(plugin):
    """The tool runs inside the model's call: that call has no result yet, and it is appended after the run.
    Stripped from the history as "orphaned", the result would answer a call that is no longer there."""
    live = [ChatMessage(role="system", content=PROMPT), *conversation(30),
            ChatMessage(role="assistant", content="", tool_calls=[PENDING])]

    agent = agent_with(live)

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert staged(agent)[-1].tool_calls == [PENDING]


async def test_a_manual_run_stages_the_conversation_without_the_prompts(plugin):
    """The session keeps the conversation; the prompts are rendered anew each turn. Staged with the run's
    rendered prompt, the session stored it and every later turn sent it again behind the fresh one."""
    agent = agent_with([ChatMessage(role="system", content=PROMPT), *conversation(30)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert [m for m in staged(agent) if m.role == "system"] == []
    assert any(m.name == "__context_summary__" for m in staged(agent))


async def test_summaries_are_marked_as_written_by_the_plugin_not_a_person(plugin):
    """Unmarked, every summary opened a turn: /undo and /retry cut at it, compaction aged the task by it."""
    agent = agent_with(conversation(30))

    await summarize(plugin, agent)

    summaries = [m for m in staged(agent) if m.name == "__context_summary__"]
    assert summaries
    assert all(m.injected_by == "context_summarizer" and not opens_a_turn(m) for m in summaries)


async def test_the_overrides_of_a_manual_run_stay_in_that_run(plugin):
    """Set on the shared hook object, a manual run's chunk_size and preserve_recent also reached every other
    session's run while it lasted."""
    hooks = plugin.server._hooks_impl
    seen = []

    async def chat(*args, **kwargs):
        seen.append((hooks.chunk_size, hooks.preserve_recent))
        return "short"

    hooks._summarizer_llm.chat = AsyncMock(side_effect=chat)
    agent = agent_with(conversation(40))

    answer = await summarize(plugin, agent, chunk_size=5, preserve_recent=15)

    assert answer["modified"] is True
    assert seen and set(seen) == {(10, 10)}
    kept = [m for m in staged(agent) if m.name != "__context_summary__"]
    assert len(kept) == 15
    assert len(seen) == 5  # 25 older messages in chunks of 5


@pytest.mark.parametrize("params", [
    {"chunk_size": 5, "preserve_recent": "many"},
    {"chunk_size": 1},
    {"preserve_recent": 500},
])
async def test_a_bad_override_is_refused_and_changes_nothing(plugin, params):
    hooks = plugin.server._hooks_impl
    agent = agent_with(conversation(40))

    answer = await summarize(plugin, agent, **params)

    assert answer["status"] == "error"
    assert "whole number" in answer["error"]
    assert (hooks.chunk_size, hooks.preserve_recent) == (10, 10)
    hooks._summarizer_llm.chat.assert_not_called()
    agent._session_tracker.set_compacted_messages.assert_not_called()


async def test_a_run_that_changes_nothing_says_why(plugin):
    status = Mock(progress=AsyncMock(), end=AsyncMock(), error=AsyncMock())
    agent = agent_with(conversation(5))

    answer = await summarize(plugin, agent, _status=status)

    assert answer["modified"] is False
    assert answer["not_applied"] == "insufficient_old_messages"
    status.end.assert_awaited_once_with("Not summarized: insufficient_old_messages")


async def test_a_failed_run_names_what_went_wrong(plugin, monkeypatch):
    def broken(context):
        raise RuntimeError("the window could not be read")

    monkeypatch.setattr(plugin.server._hooks_impl, "_get_context_window", broken)

    answer = await summarize(plugin, agent_with(conversation(30)))

    assert answer == {"status": "error", "error": "Summarization failed: the window could not be read"}


async def check_stats(plugin, agent):
    return await plugin.call("context_summarizer_check_stats", {"_session_id": "s-1", "_agent": agent})


async def test_check_stats_ignores_a_measurement_taken_before_a_compaction(plugin):
    """Stale figures count messages that are gone; the hook ignores them, so the stats must too, or they
    recommend summarizing again right after a summary."""
    agent = agent_with(conversation(10), latest={"prompt_tokens": 900_000, "is_stale": True})
    agent.llm_for_session = Mock(return_value=Mock(context_window=1_000_000))

    answer = await check_stats(plugin, agent)

    assert answer["actual_tokens"] == 0
    assert answer["recommendation"] == "ok"

    agent.system_config.tool_registry.get_server.return_value.tracker.get_latest.return_value = {
        "prompt_tokens": 900_000}
    assert (await check_stats(plugin, agent))["recommendation"] == "summarize"


@pytest.fixture
def registered(monkeypatch):
    """The hook as the registry holds it once the instance is enabled: on by default (schema.yaml)."""
    hooks = {"context_summarizer.summarize_context": {"enabled": True}}
    registry = SimpleNamespace(get_hook_info=lambda name: hooks.get(name))
    monkeypatch.setattr("plugins.context_summarizer.server.get_hook_registry", lambda: registry)
    return hooks


async def test_check_stats_recommends_by_the_message_count_the_hook_fires_on(plugin, registered):
    """max_messages is what fires the hook in the shipped setup; a recommendation by tokens alone said "ok"
    to a conversation the hook was about to summarize."""
    overrides = {"context_summarizer.summarize_context": {"enabled": True, "max_messages": 20}}

    answer = await check_stats(plugin, agent_with(conversation(30), overrides=overrides))
    assert (answer["max_messages"], answer["recommendation"]) == (20, "summarize")

    assert (await check_stats(plugin, agent_with(conversation(30))))["recommendation"] == "ok"

    plugin.server._hooks_impl.max_messages = 25
    answer = await check_stats(plugin, agent_with(conversation(30)))
    assert (answer["max_messages"], answer["recommendation"]) == (25, "summarize")


async def test_check_stats_counts_no_messages_for_an_agent_the_hook_does_not_run_for(plugin, registered):
    """Switched off for the agent, or not registered at all, no message count ever fires the hook."""
    plugin.server._hooks_impl.max_messages = 25
    off = {"context_summarizer.summarize_context": {"enabled": False, "max_messages": 20}}

    answer = await check_stats(plugin, agent_with(conversation(30), overrides=off))
    assert (answer["max_messages"], answer["recommendation"]) == (0, "ok")

    registered.clear()
    answer = await check_stats(plugin, agent_with(conversation(30)))
    assert (answer["max_messages"], answer["recommendation"]) == (0, "ok")


def test_the_summarizer_is_told_which_tools_were_called(plugin):
    """A turn that only calls a tool has no text; without its calls the summarizer read an empty line and
    results answering nothing it could see."""
    text = plugin.server._hooks_impl._format_messages_for_summary([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c-1", "function": {"name": "file_ops_read", "arguments": '{"path": "notes.md"}'}}]},
        {"role": "tool", "tool_call_id": "c-1", "content": "the notes"},
    ])
    assert 'file_ops_read({"path": "notes.md"})' in text


def tool_chain(pairs: int, start: int = 0) -> list[ChatMessage]:
    chain = []
    for i in range(start, start + pairs):
        chain.append(ChatMessage(role="assistant", content="", tool_calls=[
            {"id": f"c-{i}", "type": "function", "function": {"name": "file_ops_read", "arguments": "{}"}}]))
        chain.append(ChatMessage(role="tool", tool_call_id=f"c-{i}", content=f"result {i} " + "words " * 100))
    return chain


TASK = "Write the quarterly report from the files in reports/."


def is_summary(message) -> bool:
    return message.name == "__context_summary__"


@pytest.mark.parametrize("pairs", [15, 5])
async def test_the_head_of_the_running_turn_is_never_summarized(plugin, pairs):
    """A long tool chain pushes the person's question out of the recent messages. Summarized, the model lost its
    task, and with the summaries marked as injected no message opened the turn any more (/undo, /retry,
    file_checkpoints). It stays in place, between the summaries of what came before and after it. With 5 pairs
    it is the last of the older messages: nothing after it is summarized. 18 messages in front of it: without the
    protection it would share a chunk with them, not sit alone in one that is never summarized."""
    agent = agent_with([*conversation(18), ChatMessage(role="user", content=TASK), *tool_chain(pairs)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    history = staged(agent)
    heads = [m for m in history if opens_a_turn(m)]
    assert [m.content for m in heads] == [TASK]
    at = history.index(heads[0])
    assert is_summary(history[at - 1])
    assert is_summary(history[at + 1]) is (pairs == 15)
    assert history[-10:] == agent.get_live_messages.return_value[-10:]


CHART = "Also add a chart of the monthly totals."


async def test_every_head_of_the_running_turn_stays_in_place(plugin):
    """A person may write into a run while it works (the chat's append): that message opens a turn too, and the
    task in front of it is still the one being worked on. Both stay, each between the summaries around it."""
    agent = agent_with([*conversation(18), ChatMessage(role="user", content=TASK), *tool_chain(8),
                        ChatMessage(role="user", content=CHART), *tool_chain(10, start=100)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    history = staged(agent)
    heads = [m for m in history if opens_a_turn(m)]
    assert [m.content for m in heads] == [TASK, CHART]
    task_at, chart_at = history.index(heads[0]), history.index(heads[1])
    assert is_summary(history[task_at - 1]) and is_summary(history[task_at + 1])
    assert is_summary(history[chart_at - 1]) and is_summary(history[chart_at + 1])


async def test_a_run_split_by_heads_stays_within_max_chunks(plugin):
    """Chunked apart, the parts around a head took one chunk more than max_chunks when the size came from the
    total: 6 before and 14 after with max_chunks 2 gave 3 calls."""
    hooks = plugin.server._hooks_impl
    hooks.max_chunks = 2
    agent = agent_with([*conversation(6), ChatMessage(role="user", content=TASK), *tool_chain(12)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert hooks._summarizer_llm.chat.await_count == 2


async def test_the_run_event_counts_only_what_was_summarized(plugin):
    """The run's event lists the messages summarized and the summaries written -- not the turn's head, which
    stayed as it was."""
    agent = agent_with([*conversation(18), ChatMessage(role="user", content=TASK), *tool_chain(15)])

    await summarize(plugin, agent)

    event = plugin.server.summarization_history[-1]
    assert event["status"] == "success"
    assert all(TASK not in m["content"] for m in event["before_messages"] + event["after_messages"])
    assert event["messages_summarized"] == len(event["before_messages"]) == 18 + 20
    assert all(m["name"] == "__context_summary__" for m in event["after_messages"])


@pytest.mark.parametrize("stop", ["cancelled", "no summarizing model"])
async def test_a_run_that_sends_no_call_keeps_the_head_of_the_turn(plugin, stop):
    """Stopped before the first chunk, the older messages come back as they were -- the head among them. The
    head is large here: dropped, the run would be applied without it."""
    from agent_system.hooks import HookContext, HookType

    hooks = plugin.server._hooks_impl
    task = ChatMessage(role="user", content=TASK + " " + "details " * 1500)
    messages = [*conversation(4), task, *tool_chain(15)]
    token = SimpleNamespace(is_cancelled=stop == "cancelled")
    if stop != "cancelled":
        hooks._summarizer_llm = None
        hooks._get_summarizer_llm = lambda context: None
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r-1", session_id="s-1", messages=messages,
                          llm=SimpleNamespace(context_window=1_000_000), metadata={"manual_trigger": True},
                          cancellation_token=token)

    result = await hooks.summarize_context(context)

    kept = result.context.messages
    assert any(opens_a_turn(m) and (m.content if hasattr(m, "content") else m["content"]) == task.content
               for m in kept)


async def test_the_summaries_name_the_instance_that_wrote_them():
    """A second instance of the plugin is shown as the one that injected its summaries."""
    plugin = PLUGIN_FACTORY("second_summarizer", AgentSystemConfig(), ToolServerConfig())
    plugin.server._hooks_impl._summarizer_llm = AsyncMock(model_name="stub", chat=AsyncMock(return_value="short"))
    agent = agent_with(conversation(30))

    await plugin.call("second_summarizer_summarize", {"_session_id": "s-1", "_agent": agent})

    summaries = [m for m in staged(agent) if m.name == "__context_summary__"]
    assert summaries and {m.injected_by for m in summaries} == {"second_summarizer"}


async def test_a_turn_a_hook_continued_keeps_its_task(plugin):
    """agent_continuation answers an answer with an injected "Continue" and the run goes on. Counted from that
    answer, the task in front of it was summarized and the turn had no head left."""
    agent = agent_with([*conversation(18), ChatMessage(role="user", content=TASK), *tool_chain(3),
                        ChatMessage(role="assistant", content="The report is drafted. Anything else?"),
                        ChatMessage(role="user", content="Continue.", injected_by="agent_continuation"),
                        *tool_chain(15, start=100)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert [m.content for m in staged(agent) if opens_a_turn(m)] == [TASK]


async def test_the_chunk_budget_counts_the_chunks_as_they_are_cut(plugin):
    """A call stays whole with its results, so a chain cuts into more chunks than its length over the size: one
    head and 50 chain messages with max_chunks 2 made 3 calls at size 25 (24 + 24 + 2)."""
    hooks = plugin.server._hooks_impl
    hooks.max_chunks = 2
    agent = agent_with([ChatMessage(role="user", content=TASK), *tool_chain(30)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert hooks._summarizer_llm.chat.await_count == 2


async def test_a_person_writing_after_a_continued_answer_keeps_the_task(plugin):
    """The run drains what a person types into it as an unmarked message. After a hook's "Continue" that message
    became the last head, the continued answer ended the turn again and the task in front of it was summarized."""
    agent = agent_with([*conversation(18), ChatMessage(role="user", content=TASK), *tool_chain(3),
                        ChatMessage(role="assistant", content="The report is drafted. Anything else?"),
                        ChatMessage(role="user", content="Continue.", injected_by="agent_continuation"),
                        *tool_chain(3, start=100), ChatMessage(role="user", content=CHART),
                        *tool_chain(15, start=200)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert [m.content for m in staged(agent) if opens_a_turn(m)] == [TASK, CHART]


async def test_the_chunk_size_for_a_long_history_is_found_in_few_passes(plugin, monkeypatch):
    """Counting the size up one by one re-chunked the whole history each step: some 600 passes, seconds inside
    the hook, for a few thousand messages."""
    hooks = plugin.server._hooks_impl
    passes = []
    chunk = hooks._create_smart_chunks

    def counted(messages, size):
        passes.append(size)
        return chunk(messages, size)

    monkeypatch.setattr(hooks, "_create_smart_chunks", counted)
    agent = agent_with([ChatMessage(role="user", content=TASK), *tool_chain(3000)])

    answer = await summarize(plugin, agent)

    assert answer["modified"] is True
    assert hooks._summarizer_llm.chat.await_count <= hooks.max_chunks
    assert len(passes) <= 40
