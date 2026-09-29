"""An agent called as a tool runs on a sub-session of its own, below its caller's -- not on the caller's.

It ran on its caller's session id: it saved its own transcript into the caller's
session file (a new one it created with its own agent name and a title from the
sub-task; a stored one it replaced until the caller's next save), and its tracker
kept every caller session for the life of the process, throwaway ones included.
Now it runs on tool_session_id(caller session, its own name): its own
conversation with that caller -- the next call in the same caller session sees
the earlier ones -- stored under the call's owner, below the caller's session like
a sub-agent manager's sub-session, and gone from its tracker with the caller's.

The production dispatch (ToolExecutionManager, which injects the caller's session
and user and gives the call a request id of its own below the caller's) on a
real Agent / BasicAgent with a scripted LLM; sessions in a real SessionManager
under tmp_path.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.core.request_context import register_request_user, release_request_user_tree
from agent_system.servers.agent.components.session_tracking import SessionTracker
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.servers.agent.server import Agent, tool_session_id
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import EPHEMERAL_SESSION_PREFIX, SessionService
from agent_system.tools.base import ToolServerRegistry
from test_agent_finish_reason_transport import _llm_system


@pytest.fixture
def world(tmp_path):
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    service = SessionService(manager, checkpoint_interval_seconds=0)
    registry = ToolServerRegistry()
    counter = {"n": 0}

    def called(kind="agent", name="helper", first_call=None, allowed=None, calls=None):
        """The agent called as a tool; its LLM records every call's messages and answers 'noted: <task>'.
        ``first_call`` (name, arguments): the first step of each run makes that tool call first -- in the
        first ``calls`` runs only, when given (``agent.made`` counts them)."""
        seen = []
        made = []

        async def stream(messages, tools, cancellation_token=None, status_scope=None):
            seen.append([getattr(message, "content", None) for message in messages])
            if (first_call is not None and getattr(messages[-1], "role", None) == "user"
                    and (calls is None or len(made) < calls)):
                made.append(len(seen))
                tool, arguments = first_call
                yield {"type": "final", "finish_reason": "tool_calls", "usage": {},
                       "assistant": {"role": "assistant", "content": "", "tool_calls": [
                           {"id": f"c{len(seen)}", "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}
                return
            task = next((m.content for m in reversed(messages) if getattr(m, "role", None) == "user"), "")
            yield {"type": "final", "finish_reason": "stop", "usage": {},
                   "assistant": {"role": "assistant", "content": f"noted: {task}"}}

        from unittest.mock import AsyncMock

        from agent_system.config.models import ToolConfig
        config = ToolServerConfig(type=kind, enabled=True,
                                  agent_config=AgentConfig(max_steps=3, llm_profile="normal",
                                                           tools=ToolConfig(allowed=allowed or [])))
        if kind == "basic_agent":
            from plugins.basic_agent.server import BasicAgent
            agent = BasicAgent(name, AgentSystemConfig(llm_system=_llm_system()), config, registry)
        else:
            agent = Agent(name, AgentSystemConfig(llm_system=_llm_system()), config, registry)
        llm = AsyncMock()
        llm.supports_streaming = lambda: True
        llm.chat_tools_streaming = stream
        agent.llm = llm
        agent._session_service = service
        registry.register(name, agent)
        agent.seen = seen
        agent.made = made
        agent.tool = name if kind == "agent" else f"{name}_execute_task"
        agent._tool_visible = True  # callable as a tool by the other agents here
        return agent

    async def call(agent, caller_session, user="alice", task="look it up", **model_arguments):
        """One tool call from a run of *user* in *caller_session*: the answer, and the request id it ran under."""
        counter["n"] += 1
        caller_request = f"rq-{user}-{counter['n']}"
        register_request_user(caller_request, user)
        ran_under = []
        real = agent.run_events

        async def recording(*args, **kwargs):
            ran_under.append(kwargs.get("request_id"))
            async for event in real(*args, **kwargs):
                yield event

        agent.run_events = recording
        try:
            items = [item async for item in ToolExecutionManager(registry).execute_tools_streaming(
                tool_calls=[{"id": "t1", "function": {"name": agent.tool,
                                                      "arguments": json.dumps({"task": task, **model_arguments})}}],
                tool_name_mapping={agent.tool: agent.name}, available_tools=[agent.name], step=1,
                request_id=caller_request, session_id=caller_session, user_id=user)]
        finally:
            agent.run_events = real
            release_request_user_tree(caller_request)
        [complete] = [item for item in items if item["type"] == "complete"]
        return json.loads(complete["messages"][0].content), caller_request, ran_under

    def files():
        return sorted(p.stem for p in (tmp_path / "sessions").rglob("*.json")
                      if not p.name.startswith(".") and "index" not in p.name)

    def sam():
        """A sub-agent manager and a worker it may start, beside the called agent."""
        from plugins.sub_agent_manager.server import SubAgentManagerServer

        registry.register("sam", SubAgentManagerServer("sam", AgentSystemConfig(), ToolServerConfig(
            type="sub_agent_manager", enabled=True, allowed_agents=["*"], max_nesting_depth=5)))
        return called(name="worker")

    return SimpleNamespace(manager=manager, service=service, called=called, call=call, files=files, tmp=tmp_path,
                           sam=sam, registry=registry)


def _file(world, session_id):
    [path] = list((world.tmp / "sessions").rglob(f"{session_id}.json"))
    return path


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_the_callers_stored_session_is_untouched(world, kind):
    """Checked right after the tool call returns, before the caller saves: the caller's file as it was."""
    stored = await world.manager.create_session(user_id="alice", session_id="S", title="the caller's own title",
                                                agent_name="coordinator")
    stored["messages"] = [{"role": "user", "content": "the caller's question"}]
    await world.manager.save_session(stored)
    before = _file(world, "S").read_bytes()
    helper = world.called(kind)

    answer, _, _ = await world.call(helper, "S")

    assert answer["status"] == "success", answer
    assert _file(world, "S").read_bytes() == before, "the called agent wrote the caller's session"
    below = json.loads(_file(world, tool_session_id("S", helper.name)).read_text())
    assert below["parent_session"]["session_id"] == "S"
    assert [m["content"] for m in below["messages"]] == ["look it up", "noted: look it up"]


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_a_new_callers_session_is_not_made_by_the_called_agent(world, kind):
    """The caller's session is new (not saved yet): the called agent created it with its own agent name and a
    title from the sub-task, and that title stayed."""
    helper = world.called(kind)

    await world.call(helper, "N")

    assert "N" not in world.files(), "the called agent created the caller's session"
    assert world.files() == [tool_session_id("N", helper.name)]


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_the_called_agent_remembers_its_calls_in_one_caller_session_only(world, kind):
    helper = world.called(kind)

    await world.call(helper, "S", task="first question")
    await world.call(helper, "S", task="second question")
    await world.call(helper, "T", task="third question")

    second, third = helper.seen[1], helper.seen[2]
    assert "first question" in second and "noted: first question" in second, second
    assert "first question" not in third and "second question" not in third, third


async def test_a_session_id_the_model_passes_is_not_the_one_it_runs_on(world):
    """Tool execution strips ``_*`` keys from a model's arguments; a plain ``session_id`` goes through to the
    agent and names nothing: the session comes from the caller's injected one and the agent's name."""
    helper = world.called()

    await world.call(helper, "S", session_id="S-other", _session_id="S-injected-by-the-model")

    held = set(helper._session_tracker._session_metadata)
    assert held == {tool_session_id("S", helper.name)}, held


async def test_the_call_runs_under_a_request_id_below_the_callers(world):
    """tool_approval's inheritance walks the request id chain by its ``_`` levels (policy.inherited)."""
    helper = world.called()

    _, caller_request, ran_under = await world.call(helper, "S")

    [request_id] = ran_under
    assert request_id.startswith(f"{caller_request}_"), (caller_request, request_id)


async def test_a_throwaway_caller_leaves_nothing_on_disk_or_in_memory(world):
    """Within the caller's throwaway session the called agent remembers its calls; nothing is saved, and once
    the caller's session is dropped (discard_session in the caller agent's tracker) the called agent holds
    nothing of it either. Two runs of a real caller agent: the call names it (``_agent_name``), and only its
    own discard takes the session along."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    session = f"{EPHEMERAL_SESSION_PREFIX}oai-turn"
    below = tool_session_id(session, helper.name)

    async def run(n):
        register_request_user(f"rq-throwaway-{n}", "alice")
        try:
            [_ async for _ in caller.run_events(task=f"question {n}", request_id=f"rq-throwaway-{n}",
                                                session_id=session)]
        finally:
            release_request_user_tree(f"rq-throwaway-{n}")

    await run(1)
    await run(2)
    remembered = "noted: look it up" in helper.seen[1]
    held_before = dict(helper._session_tracker._session_metadata)
    caller._session_tracker.discard_session(session)
    left_after_two = dict(helper._session_tracker._session_metadata)
    await run(3)  # one call only: its session was opened by that call, and named by it alone
    caller._session_tracker.discard_session(session)

    assert remembered, helper.seen[1]
    assert world.files() == [], "a throwaway caller's call was saved"
    assert set(held_before) == {below}, held_before
    assert left_after_two == {} and helper._session_tracker._session_metadata == {}
    assert helper._session_tracker.get_session_messages(below) == []


async def test_it_is_stored_under_the_owner_hidden_and_refused_to_another_user(world):
    from agent_system.api.session_endpoints import list_sessions

    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    helper = world.called()
    below = tool_session_id("S", helper.name)
    await world.call(helper, "S")

    listed = [s.session_id for s in await list_sessions(current_user=SimpleNamespace(username="alice"),
                                                         session_manager=world.manager)]
    bob_in_memory, _, _ = await world.call(helper, "S", user="bob")
    helper._session_tracker.discard_session(below)  # a later process: read back from disk
    bob_from_disk, _, _ = await world.call(helper, "S", user="bob")

    assert _file(world, below).parent.name == "alice"
    assert listed == ["S"], f"the called agent's session is listed: {listed}"
    for answer in (bob_in_memory, bob_from_disk):
        assert (answer["status"], answer.get("error_type")) == ("error", "foreign_session"), answer
    assert len(helper.seen) == 1, "bob's calls ran"


async def test_two_agents_called_in_one_caller_session_keep_apart(world):
    helper, scout = world.called(name="helper"), world.called(name="scout")

    await world.call(helper, "S", task="for the helper")
    await world.call(scout, "S", task="for the scout")

    assert tool_session_id("S", "helper") != tool_session_id("S", "scout")
    assert "for the helper" not in scout.seen[0], scout.seen[0]


@pytest.mark.parametrize("first, second", [("my.agent", "my agent"), ("a/b", "a:b"), ("é", "è")])
def test_names_that_cut_to_the_same_text_get_sessions_of_their_own(first, second):
    """A session id holds ``[A-Za-z0-9_-]`` only; the name is cut to that, and a digest of the pair keeps
    two names apart that cut to the same text."""
    manager = SessionManager.__new__(SessionManager)
    for name in (first, second):
        assert manager._validate_session_id(tool_session_id("S", name))
    assert tool_session_id("S", first) != tool_session_id("S", second)


def test_a_session_below_a_session_below_another_belongs_to_the_first_and_goes_with_it():
    """An agent called as a tool that calls another: two levels, in two agents' trackers. Whose session it
    is (callers_session: where a person's session grants are kept) and what goes when the first is dropped."""
    from agent_system.servers.agent.components.session_tracking import callers_session

    caller, middle, inner = SessionTracker(), SessionTracker(), SessionTracker()
    caller.set_session_metadata("S", {"user_id": "alice"})
    middle.set_session_metadata("S--m", {"user_id": "alice", "parent_session_id": "S"})
    inner.set_session_metadata("S--m--i", {"user_id": "alice", "parent_session_id": "S--m"})

    assert [callers_session(sid) for sid in ("S--m--i", "S--m", "S", "other")] == ["S", "S", "S", "other"]
    caller.discard_session("S")
    assert middle._session_metadata == {} and inner._session_metadata == {}


SPAWN = ("sam_manage_sub_agent", {"operation": "create", "agent_type": "worker", "task": "dig"})


async def test_a_spent_nesting_budget_holds_across_the_call(world):
    """The caller is a sub-agent manager's sub-session with no level left below it. The agent it calls as a
    tool ran on that session and could spawn nothing; on a session of its own, filed with no budget, a
    manager restarted the count at its own maximum -- sub-agent, tool agent, sub-agent, ... without end."""
    world.sam()
    helper = world.called(first_call=SPAWN, allowed=["sam/*"])
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    await world.manager.create_session(user_id="alice", session_id="I", agent_name="worker", parent_session_id="S")
    stored = await world.manager.load_session("alice", "I")
    stored["depth"], stored["depth_budget"] = 2, 0
    await world.manager.save_session(stored)

    await world.call(helper, "I")

    assert any("Maximum nesting depth exceeded" in str(content) for content in helper.seen[1]), helper.seen[1]


async def test_the_call_costs_no_nesting_level(world):
    """One level left below the caller: the agent it calls may still start a sub-agent, as it could when it
    ran on the caller's session. The call is no sub-agent level of its own."""
    world.sam()
    helper = world.called(first_call=SPAWN, allowed=["sam/*"])
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    await world.manager.create_session(user_id="alice", session_id="I", agent_name="worker", parent_session_id="S")
    stored = await world.manager.load_session("alice", "I")
    stored["depth"], stored["depth_budget"] = 2, 1
    await world.manager.save_session(stored)

    await world.call(helper, "I")

    below = await world.manager.load_session("alice", tool_session_id("I", helper.name))
    assert (below["depth"], below["depth_budget"]) == (3, 1), below
    assert not any("Maximum nesting depth" in str(content) for content in helper.seen[1]), helper.seen[1]
    assert list((below.get("metadata") or {}).get("sub_agents") or {}), "the sub-agent was not started"


async def test_its_session_is_filed_below_the_caller_before_its_first_step_spawns(world):
    """A sub-agent manager called in the first step files the sub-agent in its caller's record; there was
    none yet (the run's first save follows a completed tool turn), so it made one: a top-level "Coordinator
    Session" -- listed, and woken like one."""
    from agent_system.core.session_presence import SessionPresence

    world.sam()
    helper = world.called(first_call=SPAWN, allowed=["sam/*"])
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    below = tool_session_id("S", helper.name)

    await world.call(helper, "S")

    stored = await world.manager.load_session("alice", below)
    roots = [s["session_id"] for s in await world.manager.list_root_sessions("alice")]
    woken = SessionPresence(world.manager.storage_path).notify(below, "alice")
    assert stored["parent_session"]["session_id"] == "S" and stored["title"] != "Coordinator Session", stored
    assert list((stored.get("metadata") or {}).get("sub_agents") or {}), "fixture: no sub-agent was started"
    assert roots == ["S"], roots
    assert woken[0] == "queued" and "sub-agent" in woken[1], woken


async def test_a_throwaway_turn_whose_tool_agent_spawns_leaves_no_record(world):
    """openai_api's stateless turn: a sub-agent manager the called agent uses writes a record for the called
    agent's session (throwaway, so never filed before); the turn's end deletes it with its own."""
    from plugins.openai_api.turns import AgentTurn

    world.sam()
    helper = world.called(first_call=SPAWN, allowed=["sam/*"])
    helper._tool_visible = True
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    session = f"{EPHEMERAL_SESSION_PREFIX}oai-turn"
    turn = AgentTurn(caller, world.service, user="alice", session_id=session, request_id="oai_t1", persist=False)

    await turn.open([])
    [_ async for _ in turn.events("go")]
    spawned = list(world.files())
    await turn.close()

    below = tool_session_id(session, helper.name)
    assert below in spawned, f"fixture: the called agent's session got no record: {spawned}"
    assert session not in world.files() and below not in world.files(), world.files()
    assert helper._session_tracker.get_session_metadata(below) is None


async def test_a_running_call_keeps_its_session_until_it_lets_go(world):
    """The caller's run was cancelled, its tool call runs on; its session is dropped meanwhile. Taken from
    under the running call, the call's final save found no metadata. It goes once the call lets go -- unless
    the caller's session is back by then."""
    helper = world.called()
    below = tool_session_id("S", helper.name)
    callers_tracker = SessionTracker()
    callers_tracker.agent_name = "caller"
    for parent_back in (False, True):
        callers_tracker.set_session_metadata("S", {"user_id": "alice"})
        helper._session_tracker.set_session_metadata(below, {"user_id": "alice", "parent_session_id": "S",
                                                             "parent_agent": "caller"})
        assert await helper._session_tracker.acquire_session_lock(below, "rq-running")

        callers_tracker.discard_session("S")
        kept = helper._session_tracker.get_session_metadata(below)
        if parent_back:
            callers_tracker.set_session_metadata("S", {"user_id": "alice"})
        await helper._session_tracker.release_session_lock(below, "rq-running")

        assert kept is not None, "the running call's session was taken from under it"
        assert (helper._session_tracker.get_session_metadata(below) is not None) is parent_back, parent_back


def test_only_the_callers_own_discard_takes_the_session_along(world):
    """The same session id in another agent's tracker (it opened the id for a run of its own and found
    nothing) is not the caller's session: its discard leaves the caller's tool sessions alone."""
    helper = world.called()
    below = tool_session_id("S", helper.name)
    callers, others = SessionTracker(), SessionTracker()
    callers.agent_name, others.agent_name = "caller", "other"
    helper._session_tracker.set_session_metadata(below, {"user_id": "alice", "parent_session_id": "S",
                                                         "parent_agent": "caller"})

    others.discard_session("S")
    kept = helper._session_tracker.get_session_metadata(below) is not None
    callers.discard_session("S")

    assert kept, "another agent's discard of the same id took the caller's tool session"
    assert helper._session_tracker.get_session_metadata(below) is None


async def test_a_custom_execute_task_is_refused_another_users_session_held_in_memory(world):
    """The documented shape (plugin_authoring.md): ``tool_session`` and then ``collect_final_result``. The
    session held for bob in this process: refused, not run -- a run refused there answered "success"."""
    from agent_system.servers.agent.result_utils import collect_final_result

    helper = world.called()
    below = tool_session_id("T", helper.name)
    helper._session_tracker.set_session_metadata(below, {"user_id": "bob", "agent_name": helper.name})

    async def execute_task(params):
        session_id, refusal = await helper.tool_session(params)
        if refusal:
            return {"status": "error", **refusal}
        result = await collect_final_result(helper, params["task"], request_id=params.get("_request_id"),
                                             session_id=session_id)
        return {"status": "success", "result": result}

    register_request_user("rq-alice-custom_001", "alice")
    try:
        answer = await execute_task({"task": "x", "_session_id": "T", "_request_id": "rq-alice-custom_001"})
    finally:
        release_request_user_tree("rq-alice-custom_001")

    assert (answer["status"], answer.get("error_type")) == ("error", "foreign_session"), answer
    assert helper.seen == []



async def test_a_session_its_run_saves_first_is_filed_below_the_caller_too(world, monkeypatch):
    """Filing it before the run is best effort; when that did not happen, the run's first save makes the
    record -- below the caller as well (SessionService, from the parent its metadata names)."""
    helper = world.called()

    async def not_filed(*args, **kwargs):
        return None

    monkeypatch.setattr(helper, "_file_tool_session", not_filed)
    await world.call(helper, "S")

    stored = await world.manager.load_session("alice", tool_session_id("S", helper.name))
    assert stored["parent_session"]["session_id"] == "S", stored


def _tool_results(agent) -> list[str]:
    """What the agent's LLM was shown of its tool calls' answers, in its last call."""
    return [str(content) for content in agent.seen[-1] if isinstance(content, str) and '"status"' in content]


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_an_agent_that_calls_itself_is_refused(world, kind):
    """On its caller's session such a call waited at that session's lock and was refused; on a session of its
    own below it, every level got a new one -- and a new lock -- and nothing stopped it."""
    tool = "again" if kind == "agent" else "again_execute_task"
    agent = world.called(kind, name="again", first_call=(tool, {"task": "once more"}), allowed=["again"], calls=4)

    answer, _, _ = await world.call(agent, "S")

    assert answer["status"] == "success", answer
    assert len(agent.made) == 1, f"it ran {len(agent.made)} levels deep"
    [refused] = _tool_results(agent)
    assert json.loads(refused)["error_type"] == "recursive_call", refused
    assert tool_session_id(tool_session_id("S", "again"), "again") not in world.files(), world.files()


async def test_an_agent_that_calls_itself_through_another_is_refused(world):
    first = world.called(name="first", first_call=("second", {"task": "ask back"}), allowed=["second"], calls=3)
    second = world.called(name="second", first_call=("first", {"task": "and back"}), allowed=["first"], calls=3)

    await world.call(first, "S")

    assert (len(first.made), len(second.made)) == (1, 1), (first.made, second.made)
    [refused] = _tool_results(second)
    assert json.loads(refused)["error_type"] == "recursive_call", refused


async def test_agents_that_call_one_another_down_a_chain_run(world):
    first = world.called(name="first", first_call=("second", {"task": "pass it on"}), allowed=["second"])
    second = world.called(name="second", first_call=("third", {"task": "and on"}), allowed=["third"])
    third = world.called(name="third")

    answer, _, _ = await world.call(first, "S")

    level1 = tool_session_id("S", "first")
    level2 = tool_session_id(level1, "second")
    assert answer["status"] == "success", answer
    assert [len(agent.seen) for agent in (first, second, third)] == [2, 2, 1]
    assert {level1, level2, tool_session_id(level2, "third")} <= set(world.files()), world.files()


async def test_an_agent_that_ran_above_before_and_runs_no_more_may_be_called(world):
    """The session's metadata names the agent that called there (``parent_agent``) -- here one whose run is
    over, and which holds the session it ran on still. Not running, it is no call to itself."""
    first = world.called(name="first")
    second = world.called(name="second", first_call=("first", {"task": "ask the first"}), allowed=["first"])
    first._session_tracker.set_session_metadata("S", {"user_id": "alice", "agent_name": "first"})
    second._session_tracker.set_session_metadata(tool_session_id("S", "second"), {
        "user_id": "alice", "agent_name": "second", "parent_session_id": "S", "parent_agent": "first"})

    await world.call(second, "S")

    [answered] = _tool_results(second)
    assert json.loads(answered)["status"] == "success", answered
    assert len(first.seen) == 1


async def test_a_session_read_back_meanwhile_is_still_below_its_caller(world):
    """Read back from disk while no run had it (SessionService.load_and_restore_session: an append, an undo),
    the called agent's session got metadata that names no caller; the next call ran on it as it was -- and
    the chain above it ended there: an agent it called could call its own caller again."""
    first = world.called(name="first", first_call=("second", {"task": "ask back"}), allowed=["second"], calls=3)
    second = world.called(name="second", first_call=("first", {"task": "and back"}), allowed=["first"], calls=3)
    below = tool_session_id(tool_session_id("S", "first"), "second")
    second._session_tracker.set_session_metadata(below, {"user_id": "alice", "agent_name": "second",
                                                         "llm_profile": "normal"})

    await world.call(first, "S")

    assert (len(first.made), len(second.made)) == (1, 1), (first.made, second.made)
    assert second._session_tracker.get_session_metadata(below)["parent_agent"] == "first"


LONG = "an-agent-whose-name-is-as-long-as-it-may-be"  # cut to 40 in the id


async def test_a_long_chain_of_agents_keeps_ids_a_file_may_be_named_by(world):
    """Every level adds the agent's name and a digest to the id; a few levels of long names made file names
    the file system refuses -- every save failed, and the run went on."""
    from agent_system.servers.agent.server import TOOL_SESSION_ID_MAX

    names = [f"{LONG}-{n}" for n in range(6)]
    agents = [world.called(name=name, first_call=(after, {"task": "on"}), allowed=[after])
              for name, after in zip(names, names[1:])] + [world.called(name=names[-1])]

    answer, _, _ = await world.call(agents[0], "S")

    ids, caller = [], "S"
    for name in names:
        caller = tool_session_id(caller, name)
        ids.append(caller)
    manager = SessionManager.__new__(SessionManager)
    assert answer["status"] == "success", answer
    assert all(len(sid) <= TOOL_SESSION_ID_MAX and manager._validate_session_id(sid) for sid in ids), ids
    assert set(ids) <= set(world.files()), sorted(set(ids) - set(world.files()))
    assert len(agents[-1].seen) == 1


def test_the_id_below_a_long_one_is_cut_and_stays_apart_and_throwaway():
    from agent_system.servers.agent.server import TOOL_SESSION_ID_MAX
    from agent_system.services.session_service import is_ephemeral_session

    caller, seen = f"{EPHEMERAL_SESSION_PREFIX}0123456789abcdef0123456789abcdef", set()
    for level in range(30):
        below, beside = tool_session_id(caller, f"{LONG}-a"), tool_session_id(caller, f"{LONG}-b")
        assert len(below) <= TOOL_SESSION_ID_MAX and is_ephemeral_session(below), (level, below)
        assert below == tool_session_id(caller, f"{LONG}-a") and below != beside and below not in seen, level
        seen.add(below)
        caller = below


async def _turn(world, caller, session, n, *, deliver, continues=True, meanwhile=None):
    """One stored openai_api turn of *caller* on *session*; ``deliver``: its answer went out."""
    from plugins.openai_api.turns import AgentTurn

    turn = AgentTurn(caller, world.service, user="alice", session_id=session, request_id=f"oai_{session}_{n}",
                     persist=True, continues=continues, title=None if continues else "t")
    await turn.open([])
    [_ async for _ in turn.events(f"turn {n}")]
    if meanwhile is not None:
        await meanwhile()
    await turn.close(deliver=(lambda: None) if deliver else None)


async def test_a_new_stored_conversation_put_back_takes_its_tool_sessions_along(world):
    """The client left before the answer went out: the conversation goes, and the session of the agent it
    called -- filed below it before its run -- with it; it stayed with its parent gone."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    below = tool_session_id("conv", helper.name)
    made = []

    async def look():
        made.extend(world.files())

    await _turn(world, caller, "conv", 1, deliver=False, continues=False, meanwhile=look)

    assert below in made, f"fixture: no tool session was stored: {made}"
    assert world.files() == [], world.files()
    assert helper._session_tracker.get_session_metadata(below) is None


async def test_a_stored_conversation_put_back_puts_its_tool_sessions_back(world):
    """A turn that is not delivered: its conversation is put back -- and so is the session of the agent it
    called, which it wrote as well; with the undelivered exchange there, the next call read it back. A web
    chat opening the conversation meanwhile keeps it in memory (_forget leaves it), and the called agent's
    session must not stay behind in memory with the exchange either."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)

    async def a_web_chat_opens_it():
        await world.service.open_for_run(caller, "alice", "conv", "normal")

    await _turn(world, caller, "conv", 1, deliver=True)
    before = [m["content"] for m in (await world.manager.load_session("alice", below))["messages"]]
    await _turn(world, caller, "conv", 2, deliver=False, meanwhile=a_web_chat_opens_it)
    after = [m["content"] for m in (await world.manager.load_session("alice", below))["messages"]]
    in_memory = helper._session_tracker.get_session_metadata(below)
    await _turn(world, caller, "conv", 3, deliver=True)

    assert before == ["look it up", "noted: look it up"], before
    assert after == before, after
    assert in_memory is None, "the undelivered exchange stayed in the called agent's memory"
    assert helper.seen[-1].count("noted: look it up") == 1, helper.seen[-1]


async def test_a_stored_conversation_put_back_drops_a_tool_session_the_turn_made(world):
    world.called()  # the helper
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")

    await _turn(world, caller, "conv", 1, deliver=False)

    assert world.files() == ["conv"], world.files()


async def test_a_stored_conversation_put_back_leaves_a_tool_session_it_did_not_write(world):
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"],
                          calls=1)
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)

    await _turn(world, caller, "conv", 1, deliver=True)
    before = _file(world, below).read_bytes()
    await _turn(world, caller, "conv", 2, deliver=False)  # no call this time

    assert _file(world, below).read_bytes() == before


async def test_a_tool_session_the_opening_could_not_list_is_left_as_it_is(world, monkeypatch):
    """What was stored below before the turn is not the turn's to delete -- also when the opening could not
    list it (and so kept nothing of it to put back), and no run wrote it: filed by an earlier call whose run
    saved nothing. Only its creation says it is older than the turn."""
    world.called()  # the helper
    caller = world.called(name="caller")  # calls nothing this time
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", "helper")
    await world.manager.create_session(user_id="alice", session_id=below, agent_name="helper",
                                       parent_session_id="conv")
    real, failed = world.manager.list_child_sessions, []

    async def fails_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("unreadable")
        return await real(*args, **kwargs)

    monkeypatch.setattr(world.manager, "list_child_sessions", fails_once)
    await _turn(world, caller, "conv", 1, deliver=False)

    assert failed, "fixture: the opening listed the sessions below"
    assert below in world.files(), world.files()


async def _run_on(world, agent, session, task, request_id):
    """A run of alice's on *session*, opened as /run opens one -- the web chat, say."""
    register_request_user(request_id, "alice")
    try:
        await world.service.open_for_run(agent, "alice", session, "normal")
        [_ async for _ in agent.run_events(task=task, request_id=request_id, session_id=session)]
    finally:
        release_request_user_tree(request_id)


async def _contents(world, session_id):
    return [m["content"] for m in (await world.manager.load_session("alice", session_id))["messages"]]


async def test_a_retry_after_a_put_back_stores_the_tool_session_again(world):
    """The put back deleted the session the turn made; the delete's tombstone kept its id -- the same for
    every call in the conversation -- from being stored again for the life of the process: the retry's call
    and every later one ran unsaved, and a sub-agent it started was refused ("has been deleted")."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)

    await _turn(world, caller, "conv", 1, deliver=False)
    put_back = list(world.files())
    await _turn(world, caller, "conv", 2, deliver=True)
    await _turn(world, caller, "conv", 3, deliver=True)

    assert put_back == ["conv"], put_back
    assert await _contents(world, below) == ["look it up", "noted: look it up"] * 2
    assert helper.seen[-1].count("noted: look it up") == 1, helper.seen[-1]


@pytest.mark.parametrize("theirs", ["rq-direct", "oai_conv_10"])
async def test_a_put_back_leaves_a_tool_session_a_person_ran_on_meanwhile(world, theirs):
    """The session of the agent the conversation calls, run on directly by its person while the turn ran --
    another request's write, as the conversation's own put back leaves another request's turn in place. The
    turn's runs are its own id and the ids below it (``oai_conv_1_...``), not every id it starts
    (``oai_conv_10``)."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)

    async def the_person_asks_the_helper():
        await _run_on(world, helper, below, "a question of mine", theirs)

    await _turn(world, caller, "conv", 0, deliver=True)
    await _turn(world, caller, "conv", 1, deliver=False, meanwhile=the_person_asks_the_helper)

    assert "a question of mine" in await _contents(world, below)


async def test_a_put_back_leaves_a_tool_session_another_agents_run_made(world):
    """Another agent ran on the same conversation meanwhile and called the same agent: the session it made
    below it is that run's, though made after the turn opened."""
    helper = world.called()
    caller = world.called(name="caller")  # calls nothing this time
    other = world.called(name="other", first_call=("helper", {"task": "for the other"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)

    async def the_other_agent_runs():
        await _run_on(world, other, "conv", "the other's turn", "rq-other")

    await _turn(world, caller, "conv", 1, deliver=False, meanwhile=the_other_agent_runs)

    assert below in world.files(), world.files()


async def _append_beside_the_runs(world, agent, session_id, content):
    """What the append endpoint does with a session no run has (app._append_and_persist): under the writer lock,
    appended to what the tracker holds, and saved."""
    tracker = agent._session_tracker
    assert await tracker.acquire_session_lock(session_id, "rq-append", timeout=5.0, writer=True)
    try:
        if not tracker.has_session(session_id):
            await world.service.load_and_restore_session(agent, "alice", session_id)
        assert await agent.append_to_session(session_id, content)
        assert await world.service.save_session(agent, "alice", session_id, agent.name, "normal",
                                                was_new_session=False)
    finally:
        await tracker.release_session_lock(session_id, "rq-append")


async def test_a_put_back_leaves_a_tool_session_a_person_appended_to_beside_its_runs(world):
    """An appended message is no run: the index row names none for it, and the put back took it for the
    turn's own writing -- the person's message went with the undelivered exchange."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)

    async def the_person_appends():
        await _append_beside_the_runs(world, helper, below, "a note of mine")

    await _turn(world, caller, "conv", 1, deliver=True)
    await _turn(world, caller, "conv", 2, deliver=False, meanwhile=the_person_appends)

    assert "a note of mine" in await _contents(world, below)


async def test_a_put_back_leaves_a_tool_session_a_person_appended_to_during_its_run(world):
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)
    await _turn(world, caller, "conv", 1, deliver=True)
    real, appended = helper.llm.chat_tools_streaming, []

    async def the_person_appends_into_the_run(messages, tools, cancellation_token=None, status_scope=None):
        if not appended:
            [running] = [request for request, session in helper._session_tracker._request_to_session.items()
                         if session == below and request in helper._session_tracker._active_requests]
            appended.append(await helper.append_user_message(running, "into the run"))
        async for event in real(messages, tools, cancellation_token=cancellation_token, status_scope=status_scope):
            yield event

    helper.llm.chat_tools_streaming = the_person_appends_into_the_run
    await _turn(world, caller, "conv", 2, deliver=False)

    assert appended == [True], "fixture: nothing was appended into the run"
    assert "into the run" in await _contents(world, below)


def _two_levels(world, **helper_args):
    scout = world.called(name="scout")
    helper = world.called(name="helper", first_call=("scout", {"task": "deeper"}), allowed=["scout"], **helper_args)
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    level1 = tool_session_id("conv", "helper")
    return scout, helper, caller, level1, tool_session_id(level1, "scout")


async def test_a_put_back_puts_back_the_sessions_two_levels_down(world):
    _, _, caller, level1, level2 = _two_levels(world)
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    await _turn(world, caller, "conv", 1, deliver=True)
    before = (await _contents(world, level1), await _contents(world, level2))

    await _turn(world, caller, "conv", 2, deliver=False)

    assert (await _contents(world, level1), await _contents(world, level2)) == before


async def test_a_put_back_deletes_the_sessions_two_levels_down_the_turn_made(world):
    _, _, caller, level1, level2 = _two_levels(world)

    made = []

    async def look():
        made.extend(world.files())

    await _turn(world, caller, "conv", 1, deliver=False, continues=False, meanwhile=look)

    assert {level1, level2} <= set(made), f"fixture: {made}"
    assert world.files() == [], world.files()


async def test_a_put_back_leaves_a_session_a_run_has_and_all_below_it(world):
    """The called agent's run went on after the turn's was stopped: what it and the agents below it wrote
    is that run's to write on -- the session it runs on, and the one below it the turn made."""
    _, helper, caller, level1, level2 = _two_levels(world)
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")

    async def the_helper_runs_on():
        helper._session_tracker.register_request("oai_conv_1_001_still", level1, {})
        assert await helper._session_tracker.acquire_session_lock(level1, "oai_conv_1_001_still")

    await _turn(world, caller, "conv", 1, deliver=False, meanwhile=the_helper_runs_on)
    left = set(world.files())
    await helper._session_tracker.release_session_lock(level1, "oai_conv_1_001_still")

    assert {level1, level2} <= left, left


async def test_a_session_deleted_or_cleared_is_no_orphan_any_more():
    """A session below one discarded while its run had it waits to be dropped once that run lets go. Deleted
    from the tracker meanwhile (or the tracker cleared), it waited on: the next run on the same id -- a new
    call, opened anew -- dropped it when it let go."""
    for how in ("delete_session", "clear"):
        parent, child = SessionTracker(), SessionTracker()
        parent.agent_name, child.agent_name = "p", "c"
        parent.set_session_metadata("S", {"user_id": "alice"})
        metadata = {"user_id": "alice", "parent_session_id": "S", "parent_agent": "p"}
        child.set_session_metadata("S--c", dict(metadata))
        assert await child.acquire_session_lock("S--c", "rq1")

        parent.discard_session("S")
        getattr(child, how)(*(("S--c",) if how == "delete_session" else ()))
        orphans = set(child._orphans)
        child.set_session_metadata("S--c", dict(metadata))
        assert await child.acquire_session_lock("S--c", "rq2")
        await child.release_session_lock("S--c", "rq2")

        assert orphans == set(), (how, orphans)
        assert child.get_session_metadata("S--c") is not None, how


@pytest.mark.parametrize("budget", [3, None])
async def test_a_tool_session_that_cannot_be_stored_with_its_callers_budget_does_not_run(world, monkeypatch, budget):
    """The record carries the caller's sub-agent budget from its first write. Failing that write, the call
    ran: a record made later -- by the run's first save, or by a sub-agent manager that found no parent --
    had no budget, and a manager below counted from its own maximum. With no budget above there is nothing
    to lose: the call runs, and its run's save files the session below the caller."""
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    if budget is not None:
        stored = await world.manager.load_session("alice", "S")
        stored["depth"], stored["depth_budget"] = 1, budget
        await world.manager.save_session(stored)
    helper = world.called()
    below = tool_session_id("S", helper.name)
    real = world.manager.create_session

    async def failing(**kwargs):  # the first write of the session fails, a later one (the run's save) not
        if kwargs.get("session_id") == below:
            failing.calls += 1
            if failing.calls == 1:
                raise OSError("disk full")
        return await real(**kwargs)

    failing.calls = 0
    monkeypatch.setattr(world.manager, "create_session", failing)
    answer, _, _ = await world.call(helper, "S")

    if budget is not None:
        assert (answer["status"], answer.get("error_type")) == ("error", "tool_session_unavailable"), answer
        assert "budget" in answer["error"], answer
        assert helper.seen == [] and below not in world.files(), world.files()
        assert helper._session_tracker.get_session_metadata(below) is None
    else:
        assert answer["status"] == "success", answer
        assert (await world.manager.load_session("alice", below))["parent_session"]["session_id"] == "S"


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_a_tool_session_whose_callers_budget_cannot_be_read_does_not_run(world, monkeypatch, kind):
    """Not knowing whether the caller has a budget is not knowing it has none. The calling model is told so,
    with an error_type: nothing ran."""
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    helper = world.called(kind)
    real = world.manager.load_session

    async def unreadable(user_id, session_id, *args, **kwargs):
        if session_id == "S":
            raise OSError("unreadable")
        return await real(user_id, session_id, *args, **kwargs)

    monkeypatch.setattr(world.manager, "load_session", unreadable)
    answer, _, _ = await world.call(helper, "S")

    assert (answer["status"], answer.get("error_type")) == ("error", "tool_session_unavailable"), answer
    assert "budget" in answer["error"], answer
    assert helper.seen == [], helper.seen


async def test_a_call_from_another_users_session_is_refused_before_its_session_is_filed(world):
    """The call's user is not the caller's session's: filed, its session hung below another user's."""
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    helper = world.called()

    answer, _, _ = await world.call(helper, "S", user="bob")

    assert (answer["status"], answer.get("error_type")) == ("error", "foreign_session"), answer
    assert world.files() == ["S"] and helper.seen == [], world.files()
    assert helper._session_tracker.get_session_metadata(tool_session_id("S", helper.name)) is None


async def test_a_tool_session_the_person_deleted_is_made_afresh_by_the_next_call(world):
    """DELETE /sessions/<id> on it tombstones the id -- the same for every call in the caller's session -- and
    leaves the agent's tracker as it was. The next call ran on the deleted history and saved nothing; with a
    caller's budget it did not run at all, and a sub-agent it started was refused. Deleted means forgotten:
    the next call starts it afresh, stored, with the caller's budget."""
    world.sam()
    helper = world.called(first_call=SPAWN, allowed=["sam/*"])
    await world.manager.create_session(user_id="alice", session_id="I", agent_name="worker")
    stored = await world.manager.load_session("alice", "I")
    stored["depth"], stored["depth_budget"] = 2, 3
    await world.manager.save_session(stored)
    below = tool_session_id("I", helper.name)
    await world.call(helper, "I", task="first question")

    await world.manager.delete_session("alice", below, create_backup=False)  # what the endpoint does
    again, _, _ = await world.call(helper, "I", task="second question")

    afresh = await world.manager.load_session("alice", below)
    assert again["status"] == "success", again
    assert afresh["messages"][0]["content"] == "second question", afresh["messages"]
    assert not any("first question" in str(m.get("content")) for m in afresh["messages"]), afresh["messages"]
    assert (afresh["depth_budget"], afresh["parent_session"]["session_id"]) == (3, "I"), afresh
    assert list((afresh.get("metadata") or {}).get("sub_agents") or {}), "the sub-agent was not started"


async def test_a_deleted_tool_session_a_run_has_stays_deleted(world):
    """While a run has it, the delete stands: lifted, that run's late saves would bring the deleted history
    back."""
    helper = world.called()
    helper.timeouts.session_lock_timeout = 0.1
    below = tool_session_id("S", helper.name)
    await world.call(helper, "S")
    await world.manager.delete_session("alice", below, create_backup=False)
    helper._session_tracker.register_request("rq-still-running", below, {})
    assert await helper._session_tracker.acquire_session_lock(below, "rq-still-running")
    try:
        await world.call(helper, "S", task="again")
        tombstoned = world.manager.is_deleted(below)
    finally:
        await helper._session_tracker.release_session_lock(below, "rq-still-running")

    assert tombstoned


async def test_a_checkpoint_that_makes_the_record_files_it_below_the_caller(world):
    """Filing before the run failed, and a long first step let the checkpoint write first: the record it made
    had no parent -- listed at the top of the session list, a woken session of its own."""
    from agent_system.llm.models import ChatMessage

    helper = world.called()
    below = tool_session_id("S", helper.name)
    helper._session_tracker.set_session_metadata(below, {"user_id": "alice", "agent_name": helper.name,
                                                         "llm_profile": "normal", "parent_session_id": "S"})
    helper._session_tracker.set_session_messages(below, [ChatMessage(role="user", content="look it up")])

    assert await world.service.checkpoint_session(helper, "alice", below)

    assert (await world.manager.load_session("alice", below))["parent_session"]["session_id"] == "S"


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_a_second_call_while_the_first_runs_is_answered_as_refused(world, kind):
    """Two calls of one caller session to the same agent at once (parallel tool calls): the second finds the
    session held by the first, and its run is refused at the lock. Agent.call answered "success" with the
    refusal inside."""
    import asyncio

    helper = world.called(kind)
    helper.timeouts.session_lock_timeout = 0.1
    real, first_in, go_on = helper.llm.chat_tools_streaming, asyncio.Event(), asyncio.Event()

    async def the_first_takes_its_time(messages, tools, cancellation_token=None, status_scope=None):
        if not first_in.is_set():
            first_in.set()
            await go_on.wait()
        async for event in real(messages, tools, cancellation_token=cancellation_token, status_scope=status_scope):
            yield event

    helper.llm.chat_tools_streaming = the_first_takes_its_time
    for request_id in ("rq-par_001", "rq-par_002"):
        register_request_user(request_id, "alice")
    try:
        first = asyncio.ensure_future(helper.call(helper.tool, {"task": "one", "_session_id": "S",
                                                                "_request_id": "rq-par_001"}))
        await asyncio.wait_for(first_in.wait(), 10)
        second = await helper.call(helper.tool, {"task": "two", "_session_id": "S", "_request_id": "rq-par_002"})
        go_on.set()
        first = await first
    finally:
        release_request_user_tree("rq-par_001")
        release_request_user_tree("rq-par_002")

    assert first["status"] == "success", first
    assert (second["status"], second.get("error_type")) == ("error", "session_locked"), second


async def test_another_users_call_is_refused_the_session_stored_below_an_unstored_caller(world):
    """The caller's session is not stored (a new one), so only the stored tool session itself says whose it
    is -- read back from disk in a later process (SessionService.open_for_run)."""
    helper = world.called()
    below = tool_session_id("N", helper.name)
    await world.call(helper, "N")
    helper._session_tracker.discard_session(below)  # a later process

    answer, _, _ = await world.call(helper, "N", user="bob")

    assert (answer["status"], answer.get("error_type")) == ("error", "foreign_session"), answer
    assert len(helper.seen) == 1, "bob's call ran"


async def test_a_put_back_leaves_a_tool_session_a_person_appended_to_while_the_turn_opened(world, monkeypatch):
    """The turn read the session below (_keep_below) before it watched for appends: one in between was put
    back away with the rest."""
    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)
    await _turn(world, caller, "conv", 1, deliver=True)
    real, state = world.manager.load_session, {"armed": True, "inside": False}

    async def read_then_the_person_appends(user_id, session_id, *args, **kwargs):
        record = await real(user_id, session_id, *args, **kwargs)
        if state["armed"] and not state["inside"] and session_id == below:
            state.update(armed=False, inside=True)
            try:
                await _append_beside_the_runs(world, helper, below, "a note of mine")
            finally:
                state["inside"] = False
        return record

    monkeypatch.setattr(world.manager, "load_session", read_then_the_person_appends)
    await _turn(world, caller, "conv", 2, deliver=False)

    assert not state["armed"], "fixture: the opening did not read the session below"
    assert "a note of mine" in await _contents(world, below)


async def test_a_put_back_keeps_what_is_appended_while_it_writes_the_session_back(world, monkeypatch):
    """Asked whether anything was appended, then written back -- with nothing that kept an append out in
    between: it landed before the write, and the write took it away."""
    import asyncio

    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)
    await _turn(world, caller, "conv", 1, deliver=True)
    real, state = world.manager.save_session, {"armed": False, "appending": None}

    async def the_person_appends_meanwhile(record, *args, **kwargs):
        if state["armed"] and record.get("session_id") == below and len(record.get("messages") or []) == 2:
            state["armed"] = False
            state["appending"] = asyncio.ensure_future(
                _append_beside_the_runs(world, helper, below, "a note of mine"))
            await asyncio.sleep(0.05)  # the append's turn, before the write goes on
        return await real(record, *args, **kwargs)

    async def arm():
        state["armed"] = True

    monkeypatch.setattr(world.manager, "save_session", the_person_appends_meanwhile)
    await _turn(world, caller, "conv", 2, deliver=False, meanwhile=arm)
    assert state["appending"] is not None, "fixture: the session below was not written back"
    await state["appending"]

    assert "a note of mine" in await _contents(world, below)


async def test_two_calls_at_once_open_the_session_one_after_the_other(world, monkeypatch):
    """Both calls of the caller's session find it new. The second found the first's half-opened copy held,
    ran on it and saved it -- without the caller's budget -- while the first still filed it."""
    import asyncio

    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    stored = await world.manager.load_session("alice", "S")
    stored["depth"], stored["depth_budget"] = 1, 3
    await world.manager.save_session(stored)
    helper = world.called()
    helper.timeouts.session_lock_timeout = 0.1
    below = tool_session_id("S", helper.name)
    real_create, real_llm, budgets = world.manager.create_session, helper.llm.chat_tools_streaming, []

    async def slow_filing(**kwargs):
        if kwargs.get("session_id") == below and "depth_budget" in kwargs:
            await asyncio.sleep(1.0)
        return await real_create(**kwargs)

    async def seen_with_its_budget(messages, tools, cancellation_token=None, status_scope=None):
        try:
            budgets.append((await world.manager.load_session("alice", below)).get("depth_budget"))
        except Exception as exc:  # noqa: BLE001 - not filed yet
            budgets.append(type(exc).__name__)
        async for event in real_llm(messages, tools, cancellation_token=cancellation_token,
                                    status_scope=status_scope):
            yield event

    monkeypatch.setattr(world.manager, "create_session", slow_filing)
    helper.llm.chat_tools_streaming = seen_with_its_budget
    for request_id in ("rq-par_001", "rq-par_002"):
        register_request_user(request_id, "alice")
    try:
        first = asyncio.ensure_future(helper.call(helper.tool, {"task": "one", "_session_id": "S",
                                                                "_request_id": "rq-par_001"}))
        await asyncio.sleep(0.05)
        second = await helper.call(helper.tool, {"task": "two", "_session_id": "S", "_request_id": "rq-par_002"})
        first = await first
    finally:
        release_request_user_tree("rq-par_001")
        release_request_user_tree("rq-par_002")

    assert budgets and all(budget == 3 for budget in budgets), budgets
    assert (await world.manager.load_session("alice", below))["depth_budget"] == 3
    assert "success" in (first["status"], second["status"]), (first, second)


@pytest.mark.parametrize("writable", [True, False])
async def test_a_session_made_meanwhile_gets_the_callers_budget_or_the_call_does_not_run(world, monkeypatch,
                                                                                          writable):
    """Its filing met "already exists" -- another process made the record meanwhile, without the budget -- and
    the call ran on it as it was."""
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    stored = await world.manager.load_session("alice", "S")
    stored["depth"], stored["depth_budget"] = 1, 3
    await world.manager.save_session(stored)
    helper = world.called()
    below = tool_session_id("S", helper.name)
    real_create, real_place = world.manager.create_session, world.manager.set_session_place

    async def made_meanwhile(**kwargs):
        if kwargs.get("session_id") == below and "depth_budget" in kwargs:
            await real_create(**{k: v for k, v in kwargs.items() if k not in ("depth", "depth_budget")})
            raise ValueError(f"Session {below} already exists")
        return await real_create(**kwargs)

    async def place(user_id, session_id, **kwargs):
        if not writable and session_id == below:
            raise OSError("disk full")
        return await real_place(user_id, session_id, **kwargs)

    monkeypatch.setattr(world.manager, "create_session", made_meanwhile)
    monkeypatch.setattr(world.manager, "set_session_place", place)
    answer, _, _ = await world.call(helper, "S")

    if writable:
        assert answer["status"] == "success", answer
        assert (await world.manager.load_session("alice", below))["depth_budget"] == 3
    else:
        assert (answer["status"], answer.get("error_type")) == ("error", "tool_session_unavailable"), answer
        assert helper.seen == []


async def test_a_session_a_run_has_is_dropped_only_once_the_run_lets_go():
    """Discarded while a run has it (a failed filing of a second opening, a put back), the run went on without
    its metadata and its final save found nothing to save."""
    tracker = SessionTracker()
    tracker.set_session_metadata("D", {"user_id": "alice"})
    tracker._active_requests["rq-run"] = {}
    tracker.register_request("rq-run", "D", {})
    assert await tracker.acquire_session_lock("D", "rq-run")

    tracker.discard_session("D")
    kept = tracker.get_session_metadata("D")
    await tracker.release_session_lock("D", "rq-run")

    assert kept is not None, "the running session was taken from under its run"
    assert tracker.get_session_metadata("D") is None, "it stayed after its run let go"


async def test_a_deleted_session_is_forgotten_only_after_a_save_that_read_it_before(world, monkeypatch):
    """A save no run lock covers (the API's save after a run on the session, a compaction's) read the history,
    the person deleted the session, and the next call lifted the delete's tombstone: the save then wrote the
    deleted history back -- at the top of the session list, and the call ran on it."""
    import asyncio

    helper = world.called()
    below = tool_session_id("S", helper.name)
    await world.manager.create_session(user_id="alice", session_id="S", agent_name="coordinator")
    await world.call(helper, "S", task="first question")
    gate, blocked, real_find = asyncio.Event(), [], world.manager._find_session_owner_async
    real_open = world.service.open_for_run

    async def held_up(session_id):
        if session_id == below and not blocked:
            blocked.append(True)
            await gate.wait()
        return await real_find(session_id)

    monkeypatch.setattr(world.manager, "_find_session_owner_async", held_up)
    late = asyncio.ensure_future(world.service.save_session(helper, "alice", below, helper.name, "normal",
                                                            was_new_session=False, after_run=True))

    async def opened_once_the_late_save_is_done(agent, user_id, session_id, *args, **kwargs):
        if session_id == below:
            await late
        return await real_open(agent, user_id, session_id, *args, **kwargs)

    await asyncio.sleep(0.05)
    assert blocked, "fixture: the late save did not start"
    await world.manager.delete_session("alice", below, create_backup=False)
    monkeypatch.setattr(world.service, "open_for_run", opened_once_the_late_save_is_done)
    asyncio.get_running_loop().call_later(0.2, gate.set)
    again, _, _ = await world.call(helper, "S", task="second question")

    record = await world.manager.load_session("alice", below)
    contents = [str(m["content"]) for m in record["messages"]]
    assert again["status"] == "success", again
    assert not any("first question" in content for content in contents), contents
    assert record["parent_session"]["session_id"] == "S", record


async def test_a_turns_own_runs_are_no_appends(world):
    """Two levels below the conversation and a sub-agent: nothing of the turn's own runs counts as appended,
    and the put back puts back what they wrote."""
    from agent_system.servers.agent.components import session_tracking

    world.sam()
    helper = world.called(first_call=SPAWN, allowed=["sam/*"])
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)
    await _turn(world, caller, "conv", 1, deliver=True)
    before = await _contents(world, below)

    await _turn(world, caller, "conv", 2, deliver=False)

    assert await _contents(world, below) == before
    assert session_tracking._append_watches == [], "a turn's watch outlived it"


async def test_a_turn_lets_go_of_its_watch_however_it_ends(world):
    import asyncio

    from agent_system.servers.agent.components import session_tracking
    from plugins.openai_api.turns import AgentTurn, ConversationGone

    caller = world.called(name="caller")
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")

    async def down(*args, **kwargs):
        raise RuntimeError("the model is down")
        yield  # pragma: no cover

    real = caller.llm.chat_tools_streaming
    caller.llm.chat_tools_streaming = down
    turn = AgentTurn(caller, world.service, user="alice", session_id="conv", request_id="oai_w1", persist=True,
                     continues=True)
    await turn.open([])
    with pytest.raises(Exception):
        [_ async for _ in turn.events("go")]
    await turn.close()
    caller.llm.chat_tools_streaming = real
    never_ran = AgentTurn(caller, world.service, user="alice", session_id="conv", request_id="oai_w2",
                          persist=True, continues=True)
    await never_ran.open([])
    await asyncio.ensure_future(never_ran.close())
    gone = AgentTurn(caller, world.service, user="alice", session_id="gone", request_id="oai_w3", persist=True,
                     continues=True)
    with pytest.raises(ConversationGone):
        await gone.open([])
    await gone.close()

    assert session_tracking._append_watches == [], session_tracking._append_watches


async def test_a_refusal_inside_the_called_agents_run_is_no_refusal_of_the_call(world):
    """The called agent's own tool call was refused (the session below it held by another run): its run went
    on and answered -- the call succeeded."""
    scout = world.called(name="scout")
    scout.timeouts.session_lock_timeout = 0.1
    helper = world.called(name="helper", first_call=("scout", {"task": "deeper"}), allowed=["scout"])
    level2 = tool_session_id(tool_session_id("S", "helper"), "scout")
    assert await scout._session_tracker.acquire_session_lock(level2, "rq-other")
    try:
        answer, _, _ = await world.call(helper, "S")
    finally:
        await scout._session_tracker.release_session_lock(level2, "rq-other")

    assert answer["status"] == "success", answer


async def test_a_call_is_no_call_to_itself_where_only_a_write_has_a_session_above(world):
    """A write of a session above (its session lock, as a writer) is no run of the agent -- also one by a
    request that ran on it before (openai_api's turn puts back what its run wrote): the call was refused as
    one to itself."""
    helper = world.called()
    tracker = helper._session_tracker
    tracker.register_request("oai_earlier", "S", {})  # a run of the agent on S, over
    tracker._active_requests.pop("oai_earlier", None)
    assert await tracker.acquire_session_lock("S", "oai_earlier", writer=True)
    try:
        answer, _, _ = await world.call(helper, "S")
    finally:
        await tracker.release_session_lock("S", "oai_earlier")

    assert answer["status"] == "success", answer


async def test_a_put_back_waits_for_a_write_of_a_tool_session_and_puts_it_back(world):
    """A write that has the session below when the put back comes to it (an /undo saving, say) is no run: it is
    waited for, and the session put back after it -- taken for a run, it was left with the undelivered
    exchange."""
    import asyncio

    helper = world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    await world.manager.create_session(user_id="alice", session_id="conv", agent_name="caller")
    below = tool_session_id("conv", helper.name)
    await _turn(world, caller, "conv", 1, deliver=True)
    before = await _contents(world, below)
    writes = []

    async def a_short_write():
        assert await helper._session_tracker.acquire_session_lock(below, "rq-write", writer=True)
        await asyncio.sleep(0.2)
        await helper._session_tracker.release_session_lock(below, "rq-write")

    async def a_write_starts():
        writes.append(asyncio.ensure_future(a_short_write()))
        await asyncio.sleep(0)

    await _turn(world, caller, "conv", 2, deliver=False, meanwhile=a_write_starts)
    await writes[0]

    assert await _contents(world, below) == before


async def test_a_session_whose_run_is_saving_its_last_turn_is_dropped_only_once_it_lets_go():
    """A run's finalize lets go of its entry among the active requests before its last save, and of the lock
    only after it: a discard in between took its metadata, and the save found nothing to save."""
    tracker = SessionTracker()
    tracker.set_session_metadata("D", {"user_id": "alice"})
    tracker.register_request("rq-run", "D", {})
    assert await tracker.acquire_session_lock("D", "rq-run")
    tracker._active_requests.pop("rq-run", None)  # AgentRequestManager.unregister_active_request

    tracker.discard_session("D")
    kept = tracker.get_session_metadata("D")
    await tracker.release_session_lock("D", "rq-run")

    assert kept is not None, "the session was taken from under its last save"
    assert tracker.get_session_metadata("D") is None


async def test_a_budget_written_on_a_record_made_meanwhile_keeps_what_was_written_since_it_was_read(world,
                                                                                                    monkeypatch):
    """The whole record written back with the budget on it (SessionManager.save_session merges only the
    metadata on disk) took with it a top-level write that landed after it was read -- the variables, here."""
    import asyncio

    helper = world.called()
    below = tool_session_id("S", helper.name)
    await world.manager.create_session(user_id="alice", session_id=below, agent_name="helper",
                                       parent_session_id="S")
    real_load, written = world.manager.load_session, []

    async def read_then_somebody_writes(user_id, session_id, *args, **kwargs):
        record = await real_load(user_id, session_id, *args, **kwargs)
        if session_id == below and not written:
            written.append(asyncio.ensure_future(
                world.manager.replace_session_context_vars("alice", below, {"phase": "written meanwhile"})))
            await asyncio.sleep(0.05)
        return record

    monkeypatch.setattr(world.manager, "load_session", read_then_somebody_writes)
    await helper._budget_on_record(world.manager, "alice", below, {"depth": 2, "depth_budget": 3})
    await written[0]
    monkeypatch.setattr(world.manager, "load_session", real_load)

    record = await world.manager.load_session("alice", below, bypass_cache=True)
    assert (record.get("depth"), record.get("depth_budget")) == (2, 3), record
    assert record.get("context_vars") == {"phase": "written meanwhile"}, record


async def test_a_budget_written_on_a_record_made_meanwhile_waits_for_a_save_of_it(world, monkeypatch):
    """A save of the session (SessionService.save_session: under its save lock) had read the record; the budget
    set meanwhile was written over by the save's own copy."""
    import asyncio
    from agent_system.llm.models import ChatMessage

    helper = world.called()
    below = tool_session_id("S", helper.name)
    await world.manager.create_session(user_id="alice", session_id=below, agent_name="helper",
                                       parent_session_id="S")
    helper._session_tracker.set_session_metadata(below, {"user_id": "alice", "agent_name": helper.name,
                                                         "llm_profile": "normal", "parent_session_id": "S"})
    helper._session_tracker.set_session_messages(below, [ChatMessage(role="user", content="look it up")])
    real_load, paused, go_on = world.manager.load_session, asyncio.Event(), asyncio.Event()

    async def the_save_reads_and_waits(user_id, session_id, *args, **kwargs):
        record = await real_load(user_id, session_id, *args, **kwargs)
        if session_id == below and not paused.is_set():
            paused.set()
            await go_on.wait()
        return record

    monkeypatch.setattr(world.manager, "load_session", the_save_reads_and_waits)
    saving = asyncio.ensure_future(world.service.save_session(helper, "alice", below, helper.name, "normal",
                                                              was_new_session=False))
    await asyncio.wait_for(paused.wait(), 5)
    placing = asyncio.ensure_future(helper._budget_on_record(world.manager, "alice", below,
                                                             {"depth": 2, "depth_budget": 3}))
    await asyncio.sleep(0.05)
    go_on.set()
    assert await saving
    await placing
    monkeypatch.setattr(world.manager, "load_session", real_load)

    record = await world.manager.load_session("alice", below, bypass_cache=True)
    assert (record.get("depth"), record.get("depth_budget")) == (2, 3), record
    assert [m["content"] for m in record["messages"]] == ["look it up"], record["messages"]
