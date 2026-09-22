"""A session read back finds a sub-agent's run under the call that started it.

The ids tie them: a tool runs under an internal request id (<run>_NNN), and a
run the tool starts carries that id as its prefix (<run>_NNN_async_...). The
core stamps both ends -- the message with the calls with the id each call's
tool runs under, as the tools start, and the first message a run stores with
the run's own id -- and a sub-session's index row lists its runs by it, which
is what /children hands the page.

Driven through a real Agent with a registered tool, and a real SessionManager
on a temporary folder.
"""
from types import SimpleNamespace

import pytest

from agent_system.api.session_endpoints import list_session_children
from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.tools.base import ToolServerRegistry

RUN = "stamprun01"
NEXT_RUN = "stamprun02"


class _Tool:
    """Remembers the request id it was run under, and what a viewer saw of its call meanwhile."""

    def __init__(self):
        self.name = "probe"
        self.ran_under = []
        self.agent = None
        self.seen_live = []

    def get_default_action(self):
        return "call"

    async def list_tools(self):
        return [SimpleNamespace(name="probe", description="Says ok.",
                                input_schema={"type": "object", "properties": {}})]

    async def call(self, action, params):
        self.ran_under.append(params.get("request_id"))
        # what a viewer joining now reads (GET /api/sessions/{id} of a running session)
        live = self.agent.get_live_conversation(params.get("_session_id"))
        self.seen_live.append(next(m.tool_request_ids for m in reversed(live) if m.role == "assistant"))
        return {"ok": True}


class _LLM:
    """Calls the tool on a run's first call, answers on the next."""

    model = "test/model"

    def __init__(self):
        self.seen = []

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.seen.append([m.model_copy(deep=True) for m in messages])
        if messages[-1].role == "user":
            answer = {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{len(self.seen)}", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]}
        else:
            answer = {"role": "assistant", "content": "done"}
        yield {"type": "final", "assistant": answer}


@pytest.mark.asyncio
async def test_a_call_and_a_runs_first_message_carry_their_ids():
    registry = ToolServerRegistry()
    tool = _Tool()
    registry.register("probe", tool)
    config = AgentConfig(max_steps=3)
    config.tools.allowed = ["*"]
    llm = _LLM()
    agent = Agent("stamp_agent", AgentSystemConfig(), ToolServerConfig(type="agent", enabled=True, agent_config=config),
                  registry, llm=llm)
    tool.agent = agent

    events = [e async for e in agent.run_events("the task", request_id=RUN, session_id="stamps")]
    events += [e async for e in agent.run_events("and more", request_id=NEXT_RUN, session_id="stamps")]

    assert len(tool.ran_under) == 2 and all(tool.ran_under), f"fixture: the tool ran under {tool.ran_under}"
    history = llm.seen[-1]
    # each run's first message, the earlier one kept through the history reloaded for the next
    assert [m.request_id for m in history if m.role == "user"] == [RUN, NEXT_RUN]
    # each call, with the id its tool ran under -- the prefix of any run it started
    calls = [m for m in history if m.tool_calls]
    assert [m.tool_request_ids for m in calls] == [{"call_1": tool.ran_under[0]}, {"call_3": tool.ran_under[1]}]
    assert tool.ran_under[0].startswith(f"{RUN}_") and tool.ran_under[1].startswith(f"{NEXT_RUN}_")
    # ... already while the tool ran: a viewer who joins then finds the runs it started
    assert tool.seen_live == [{"call_1": tool.ran_under[0]}, {"call_3": tool.ran_under[1]}]
    assert all(m.request_id is None for m in history if m.role in ("assistant", "tool"))
    # stored as the session is: declared fields, so a load keeps them (an undeclared one is dropped)
    stored = ChatMessage(**calls[0].model_dump(mode="json"))
    assert stored.tool_request_ids == calls[0].tool_request_ids
    opener = next(m for m in history if m.role == "user")
    assert ChatMessage(**opener.model_dump(mode="json")).request_id == RUN
    # each answer with the step its live events named it by (the last one is not in the
    # history the last call was sent)
    named = [e["step"] for e in events if e.get("type") == "thinking" and e.get("step")]
    steps = [s for i, s in enumerate(named) if not i or named[i - 1] != s]
    answers = [m for m in history if m.role == "assistant"]
    assert steps == [1, 2, 1, 2] and len(answers) == 3, f"fixture: steps {named}, {len(answers)} answers"
    assert [m.step for m in answers] == steps[:3]
    assert ChatMessage(**answers[1].model_dump(mode="json")).step == answers[1].step


@pytest.mark.asyncio
async def test_a_sub_sessions_row_lists_its_runs_by_the_id_each_opened_with(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path))
    parent = await manager.create_session("anonymous", title="coordinator", agent_name="coordinator")
    child = await manager.create_session("anonymous", title="helper", agent_name="helper",
                                         parent_session_id=parent["session_id"])
    first, retry = f"{RUN}_003_async_abc123", f"{RUN}_003_async_abc123_minlen_1"
    woken = f"{RUN}_005_sub_cont_def456"
    child["messages"] = [m.model_dump(mode="json") for m in (
        ChatMessage(role="user", content="research", request_id=first),
        # a call carries the ids its tools ran under: not a run
        ChatMessage(role="assistant", content="", tool_request_ids={"c1": f"{first}_002"}, tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="c1", content="{}"),
        ChatMessage(role="assistant", content="short"),
        ChatMessage(role="user", content="also this"),  # appended mid-run: no run of its own
        ChatMessage(role="user", content="once more", request_id=retry),
        ChatMessage(role="assistant", content="longer"),
        ChatMessage(role="developer", content="woken", request_id=woken),  # a wake opens a run too
        ChatMessage(role="assistant", content="awake"),
    )]
    await manager.save_session(child)
    # a top-level session's row does not carry them: index.json would grow by one id per turn
    parent["messages"] = [ChatMessage(role="user", content="go", request_id=RUN).model_dump(mode="json")]
    await manager.save_session(parent)

    answer = await list_session_children(parent["session_id"], current_user=None, session_manager=manager)
    assert [node["runs"] for node in answer["sessions"]] == [[first, retry, woken]]
    [top] = [row for row in await manager.list_root_sessions("anonymous") if row["session_id"] == parent["session_id"]]
    assert "runs" not in top

    # a sub-index rebuilt from the session files (after an archive pass failed to
    # delete a tree) has the same rows -- rebuilt from nothing: a row still there wins
    sub_index = tmp_path / "anonymous" / f".subs.{parent['session_id']}.index.json"
    assert sub_index.exists(), "fixture: the sub-index is not where the rebuild writes it"
    sub_index.unlink()
    rebuilt = await manager._rebuild_index("anonymous", parent["session_id"])
    assert rebuilt[child["session_id"]]["runs"] == [first, retry, woken]
