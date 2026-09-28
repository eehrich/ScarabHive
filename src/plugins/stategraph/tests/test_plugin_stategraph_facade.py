"""The agent facade (design §10): a machine addressed as an agent.

The machines here use call activities and pure states only, so the StateGraphServer needs no
runner agent; everything else is real -- the server, its service and RunManager, the
MachineAgent's run_events, the SubAgentManagerServer and AgentCaller's checks, and v4's own
story_id extraction on the answer text.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config, until

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

STORY = """\
stategraph: 1
id: m
python: m.py
params: {task: {type: string, required: true}}
context: {story: null}
finally: {call: note_end, args: {reason: "{{ ending.reason }}"}}
initial: design
states:
  design:
    do: {call: design, args: {task: "{{ params.task }}"}}
    transitions:
      - target: gate
        effect: ctx.story = out
  gate:
    transitions:
      - {target: hold, guard: "'hold' in params.task"}
      - {target: broken, guard: "'fail' in params.task"}
      - {target: done, guard: else}
  hold:
    transitions: [{trigger: go, target: done}]
  broken: {type: final, status: failed}
  done: {type: final, output: "{{ ctx.story }}"}
events: {go: {}}
"""

COMPANION = """\
ENDINGS = []


def design(task):
    return {"story_id": 812, "title": task.split("|")[0]}


def note_end(reason):
    ENDINGS.append(reason)
    return reason
"""


class Env:
    """A StateGraphServer with machine m, a registry, and the facade agent over it."""

    def __init__(self, tmp_path, **config: Any):
        from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
        from agent_system.tools.base import ToolServerRegistry
        from plugins.stategraph.facade import MachineAgent
        from plugins.stategraph.server import StateGraphServer

        self.server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
        (tmp_path / "machines" / "m.yaml").write_text(STORY, encoding="utf-8")
        (tmp_path / "machines" / "m.py").write_text(COMPANION, encoding="utf-8")
        self.registry = ToolServerRegistry()
        self.registry.register("stategraph", self.server)
        settings = {"machine": "m", "promote": ["story_id"], **config}
        self.agent = MachineAgent("story_machine", AgentSystemConfig(), ToolServerConfig(
            type="stategraph_machine", enabled=True, agent_config=AgentConfig(llm_profile="normal"), **settings),
            self.registry)

    async def ask(self, task: str, request_id: str = "req1", session_id: str = "sess1") -> list[dict[str, Any]]:
        return [event async for event in self.agent.run_events(task, request_id=request_id, session_id=session_id)]

    def runs(self) -> list[dict[str, Any]]:
        return self.server.run_store.list_runs()

    def fresh_agent(self, **config: Any) -> Any:
        """Another process's agent: same server and runs.db, nothing in memory from this one."""
        from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
        from plugins.stategraph.facade import MachineAgent

        settings = {"machine": "m", "promote": ["story_id"], **config}
        return MachineAgent("story_machine", AgentSystemConfig(), ToolServerConfig(
            type="stategraph_machine", enabled=True, agent_config=AgentConfig(llm_profile="normal"), **settings),
            self.registry)

    async def close(self) -> None:
        await self.server.stop_plugin()


@pytest.fixture
async def env(tmp_path):
    made = Env(tmp_path)
    yield made
    await made.close()


def final_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = [event["type"] for event in events if event["type"] != "status"]
    assert kinds[0] == "start" and kinds[-1] == "end", kinds
    return events[[e["type"] for e in events].index(kinds[-2])]


# ------------------------------------------------------------------ the answer and v4's contract

async def test_the_answer_is_the_output_as_json_and_v4_finds_the_story_id(env):
    from plugins_writer.writer_pipeline_v4.phase_0_analyze import Phase0AnalyzeMixin
    from plugins_writer.writer_pipeline_v4.phase_1_design import Phase1DesignMixin

    answer = final_of(await env.ask("Nachtzug|a thriller"))

    assert answer["type"] == "final", answer
    assert json.loads(answer["summary"]) == {"story_id": 812, "title": "Nachtzug"}
    assert answer["story_id"] == 812, "promoted onto the final event for writer_jobs"
    assert answer["run_id"].startswith("req1_sg"), "cost, status and cancel stay under the caller's request id"
    assert Phase0AnalyzeMixin._extract_story_id(answer["summary"]) == 812
    assert Phase1DesignMixin._inspect_designer_result(answer["summary"])[0] not in ("error", "cancelled")


async def test_a_failed_run_is_an_error_event_never_a_final(env):
    answer = final_of(await env.ask("fail please"))

    assert answer["type"] == "error", answer
    assert answer["run_id"] in answer["message"] and "failed in broken" in answer["message"]


async def test_the_same_request_again_answers_the_succeeded_run_without_a_second_one(env):
    first = final_of(await env.ask("Nachtzug", request_id="job-7", session_id="s1"))
    again = final_of(await env.ask("Nachtzug", request_id="job-7", session_id="s2"))

    assert again["summary"] == first["summary"] and again["run_id"] == first["run_id"]
    assert len(env.runs()) == 1


async def test_a_continue_answers_the_stored_output_and_starts_nothing(env):
    first = final_of(await env.ask("Nachtzug", request_id="r1", session_id="s1"))
    follow = final_of(await env.ask("Wie lautet die story_id? Antworte NUR mit: story_id=<ZAHL>",
                                    request_id="r2", session_id="s1"))

    assert follow["summary"] == first["summary"], follow
    assert len(env.runs()) == 1


async def test_a_continue_of_an_interrupted_run_resumes_it(env):
    task = asyncio.ensure_future(env.ask("hold on", request_id="r1", session_id="s1"))
    await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the run waits")
    run_id = env.runs()[0]["id"]
    task.cancel()  # the client went away: the run stays
    await asyncio.gather(task, return_exceptions=True)
    await env.server.run_manager.shutdown()  # the process stops
    env.server.run_manager._stopping = False
    assert env.server.run_store.get_run(run_id)["status"] == "interrupted"

    follow = asyncio.ensure_future(env.ask("again", request_id="r2", session_id="s1"))
    await until(lambda: run_id in env.server.run_manager.live
                and env.server.run_manager.live[run_id].ctx.status == "waiting", what="the resumed wait")
    env.server.service.send_event(run_id, "go")
    answer = final_of(await follow)

    assert answer["type"] == "final" and answer["run_id"] == run_id, answer
    assert len(env.runs()) == 1


# ------------------------------------------------------------------ cancel and the process

async def test_a_cancel_of_the_request_terminates_the_run_and_its_finally_runs(env):
    from agent_system.core.cancellation import get_cancellation_manager

    task = asyncio.ensure_future(env.ask("hold on", request_id="book-9_sub_ab12", session_id="s1"))
    await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the run waits")
    run_id = env.runs()[0]["id"]

    get_cancellation_manager().cancel_request("book-9")  # the book above the designer is cancelled
    answer = final_of(await asyncio.wait_for(task, 10))

    assert answer["type"] == "cancelled", answer
    row = env.server.run_store.get_run(run_id)
    assert row["status"] == "cancelled"
    finals = [r for r in env.server.run_store.rows(run_id, kinds=("activity",)) if r["key"] == "end.cancelled.finally"]
    assert [r["data"]["out"] for r in finals] == ["cancelled"], "the machine's finally ran"


async def test_a_bare_task_cancel_leaves_the_run_running(env):
    task = asyncio.ensure_future(env.ask("hold on", request_id="r1", session_id="s1"))
    await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the run waits")
    run_id = env.runs()[0]["id"]

    task.cancel()  # a lost SSE client, a process shutdown -- not a cancel of the request
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.1)

    assert run_id in env.server.run_manager.live
    assert env.server.run_store.get_run(run_id)["status"] == "waiting"


# ------------------------------------------------------------------ input and refusals

async def test_json_input_is_the_params_object(tmp_path):
    made = Env(tmp_path, input="json")
    try:
        answer = final_of(await made.ask(json.dumps({"task": "Nachtzug"})))
        refused = final_of(await made.ask("[1, 2]", request_id="req2", session_id="sess2"))
    finally:
        await made.close()

    assert json.loads(answer["summary"])["title"] == "Nachtzug"
    assert refused["type"] == "error" and "JSON object of params" in refused["message"], refused


async def test_an_agent_without_a_machine_answers_the_config_error(tmp_path):
    made = Env(tmp_path, machine="")
    try:
        answer = final_of(await made.ask("x"))
    finally:
        await made.close()

    assert answer["type"] == "error" and "no machine configured" in answer["message"], answer
    assert made.runs() == []


async def test_an_unknown_machine_is_an_error_event(tmp_path):
    made = Env(tmp_path, machine="nope")
    try:
        answer = final_of(await made.ask("x"))
    finally:
        await made.close()

    assert answer["type"] == "error" and "did not start" in answer["message"], answer


# ------------------------------------------------------------------ through a real SAM and AgentCaller's checks

async def test_a_sam_spawn_gets_the_json_answer(env):
    from agent_system.config.models import ToolServerConfig
    from plugins.sub_agent_manager.manager import SubAgentManager
    from plugins.sub_agent_manager.server import SubAgentManagerServer

    config = Mock(spec=ToolServerConfig)
    config.max_sub_agents_per_session, config.max_nesting_depth, config.max_sub_agents_per_type = 10, 5, 3
    config.allowed_agents, config.blocked_agents = ["story_machine"], []
    sam = SubAgentManagerServer(name="v4_sam", system_config=Mock(), server_config=config)
    session_service = Mock(save_session=AsyncMock(), session_manager=Mock())

    async def load_session(user_id, session_id, *args, **kwargs):
        if session_id == "parent1":
            return {"agent_name": "coordinator", "context_vars": {}}
        return {"agent_name": "story_machine", "parent_session": {"session_id": "parent1"}, "context_vars": {}}

    session_service.session_manager.load_session = AsyncMock(side_effect=load_session)
    manager = Mock(_extract_user_id=Mock(return_value="u1"), _session_service=session_service,
                   create_sub_session=AsyncMock(return_value="sub_story_machine_1"),
                   update_sub_session_metadata=AsyncMock(), refresh_sub_context_vars=AsyncMock(return_value={}),
                   update_sub_agent_activity=AsyncMock())
    manager.reopen_sub_session = partial(SubAgentManager.reopen_sub_session, manager)
    manager._write_sub_agent = manager.update_sub_session_metadata
    sam._extract_registry = Mock(return_value=Mock(get=Mock(return_value=env.agent)))
    sam._extract_session_service = Mock(return_value=session_service)
    sam._get_manager = Mock(return_value=manager)

    result = await sam.manage_sub_agent({"operation": "create", "agent_type": "story_machine",
                                          "task": "Nachtzug", "blocking": True, "_session_id": "parent1",
                                          "_agent": Mock(), "_request_id": "v4book"})

    assert result["status"] == "completed", result
    text = result["result"]
    assert isinstance(text, str) and not text.startswith(("Error: ", "Cancelled: ")), text  # AgentCaller's checks
    assert json.loads(text)["story_id"] == 812
    assert env.runs()[0]["id"].startswith("v4book_sub_"), "the run sits under the v4 request's prefix"


# ------------------------------------------------------------------ review of the facade (findings B2-B9)

async def ask_agent(agent: Any, task: str, request_id: str, session_id: str) -> dict[str, Any]:
    return final_of([event async for event in agent.run_events(task, request_id=request_id, session_id=session_id)])


async def test_a_continue_in_a_fresh_process_finds_the_failed_run_and_starts_none(env):
    """B2: the session's run is in runs.db, not only in this process's memory."""
    first = final_of(await env.ask("fail please", request_id="r1", session_id="s1"))
    follow = await ask_agent(env.fresh_agent(), "Wie lautet die story_id?", "r2", "s1")

    assert first["type"] == "error" and follow["type"] == "error", follow
    assert "does not start a new run" in follow["message"]
    assert len(env.runs()) == 1


async def test_a_continue_in_a_fresh_process_resumes_the_interrupted_run(env):
    task = asyncio.ensure_future(env.ask("hold on", request_id="r1", session_id="s1"))
    await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the run waits")
    run_id = env.runs()[0]["id"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await env.server.run_manager.shutdown()
    env.server.run_manager._stopping = False

    follow = asyncio.ensure_future(ask_agent(env.fresh_agent(), "again", "r2", "s1"))
    await until(lambda: run_id in env.server.run_manager.live
                and env.server.run_manager.live[run_id].ctx.status == "waiting", what="the resumed wait")
    env.server.service.send_event(run_id, "go")
    answer = await follow

    assert answer["type"] == "final" and answer["run_id"] == run_id and len(env.runs()) == 1, answer


async def test_as_another_agent_s_tool_every_call_is_a_run_of_its_own(env):
    """B3: two different tasks from one caller session get two answers."""
    first = await env.agent.call("story_machine", {"task": "Nachtzug", "_session_id": "caller",
                                                   "_request_id": "parent"})
    second = await env.agent.call("story_machine", {"task": "Morgenrot", "_session_id": "caller",
                                                    "_request_id": "parent"})

    titles = {env.server.run_store.get_run(r["id"])["output"]["title"] for r in env.runs()}
    assert titles == {"Nachtzug", "Morgenrot"}, (first, second)
    assert all(r["id"].startswith("parent_") for r in env.runs()), "still under the caller's prefix"


async def test_a_request_cancelled_before_its_run_starts_starts_none(env):
    """B4: the SAM's early cancel reaches the facade before the start."""
    from agent_system.core.cancellation import get_cancellation_manager

    agen = env.agent.run_events("Nachtzug", request_id="late", session_id="s9")
    started = await agen.__anext__()
    assert started["type"] == "start"
    get_cancellation_manager().cancel_request("late")
    rest = [event async for event in agen]

    assert [e["type"] for e in rest if e["type"] != "status"] == ["cancelled", "end"], rest
    assert env.runs() == []


async def test_another_user_s_request_id_gets_nothing_of_the_run(env):
    """B5: a known request id is no key to someone else's run."""
    env.agent._session_tracker.set_session_metadata("alice_s", {"user_id": "alice"})
    await env.ask("Nachtzug", request_id="shared", session_id="alice_s")
    env.agent._session_tracker.set_session_metadata("bob_s", {"user_id": "bob"})
    answer = final_of(await env.ask("Nachtzug", request_id="shared", session_id="bob_s"))

    assert answer["type"] == "error" and "another user" in answer["message"], answer
    assert len(env.runs()) == 1


async def test_an_error_answer_has_an_error_status_line(tmp_path):
    """B6: not the scope's default 'completed' -- and the line reaches the stream."""
    made = Env(tmp_path, machine="")
    try:
        events = await made.ask("x")
    finally:
        await made.close()

    lines = [e for e in events if e["type"] == "status" and e.get("level") == "error"]
    assert lines and "no machine configured" in lines[0]["message"], events


async def test_a_failed_final_s_output_is_in_the_error_message(env):
    """B7: a machine's failed final says why in its output; the caller sees it."""
    from pathlib import Path

    path = next(Path(d) for d in env.server.machine_dirs if (Path(d) / "m.yaml").exists()) / "m.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "  broken: {type: final, status: failed}", '  broken: {type: final, status: failed, output: {reason: "no brief"}}'),
        encoding="utf-8")
    answer = final_of(await env.ask("fail please"))

    assert answer["type"] == "error" and '"reason": "no brief"' in answer["message"], answer


async def test_a_sweep_of_the_live_run_is_not_taken_for_its_end(env):
    """B8: the owner's heartbeat undoes a sweep; the facade waits for the task, not for the row."""
    task = asyncio.ensure_future(env.ask("hold on", request_id="r1", session_id="s1"))
    await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the run waits")
    run_id = env.runs()[0]["id"]
    env.server.run_store.update_run(run_id, status="interrupted")  # what a sweep writes
    await asyncio.sleep(1.5)
    assert not task.done(), "the facade answered on a sweep of a run that still lives"

    env.server.service.send_event(run_id, "go")
    answer = final_of(await asyncio.wait_for(task, 10))
    assert answer["type"] == "final", answer


@pytest.mark.parametrize("config", [{"params": ["a", "b"]}, {"promote": "story_id"}])
async def test_a_config_of_the_wrong_type_is_the_answer_not_a_crash(tmp_path, config):
    """B9: the lazy build succeeds; every request is told what is wrong."""
    made = Env(tmp_path, **config)
    try:
        answer = final_of(await made.ask("x"))
    finally:
        await made.close()

    assert answer["type"] == "error" and next(iter(config)) in answer["message"], answer


async def test_the_service_refuses_a_run_key_of_another_user_s_live_run(env):
    """B5, service side: attaching by run_key is for the user who started the run."""
    from plugins.stategraph.service import ServiceError

    started = await env.server.service.start_run("m", params={"task": "hold on"}, user_id="alice", run_key="k1")
    await until(lambda: env.server.run_manager.live[started["run_id"]].ctx.status == "waiting", what="the wait")

    with pytest.raises(ServiceError) as refused:
        await env.server.service.start_run("m", params={"task": "hold on"}, user_id="bob", run_key="k1")
    assert refused.value.status == 409 and len(env.runs()) == 1


async def test_a_call_as_another_agent_s_tool_runs_under_the_caller_s_user(env):
    """C10: the suffixed request id is registered under the caller's user, so the run is theirs."""
    from agent_system.core.request_context import register_request_user, release_request_user_tree

    register_request_user("parent_007", "alice")
    try:
        result = await env.agent.call("story_machine", {"task": "Nachtzug", "_request_id": "parent_007"})
    finally:
        release_request_user_tree("parent_007")

    assert result["status"] == "success", result
    assert [row["user_id"] for row in env.runs()] == ["alice"]


async def test_a_cancelled_request_whose_run_no_process_runs_ends_it_with_its_finally(env):
    """C11: the run's owner died mid-run (its lease ran out, no sweep yet) and the caller cancels: the facade
    resumes the run into its termination -- the panel's path -- instead of marking it cancelled unfinished."""
    from agent_system.core.cancellation import get_cancellation_manager
    from plugins.stategraph.tests.stategraph_testkit import runnable, utc_at

    tree = runnable({"m.yaml": STORY, "m.py": COMPANION})
    env.server.run_store.create_run("r9_sgdead1", "m", tree.snapshot(), params={"task": "hold on"}, mocks={},
                                    owner="gone:1:abc", lease_until=utc_at(-5), run_key="story_machine:r9",
                                    session_id="sg_r9_sgdead1")
    env.server.run_store.set_caller("story_machine:s9", "r9_sgdead1")
    task = asyncio.ensure_future(env.ask("hold on", request_id="r9", session_id="s9"))
    await asyncio.sleep(0.2)
    get_cancellation_manager().cancel_request("r9")
    answer = final_of(await asyncio.wait_for(task, 15))

    assert answer["type"] == "cancelled", answer
    finals = [r for r in env.server.run_store.rows("r9_sgdead1", kinds=("activity",))
              if r["key"] == "end.cancelled.finally"]
    assert [r["data"]["out"] for r in finals] == ["cancelled"], "the machine's finally ran"


async def test_a_gated_machine_refuses_a_plain_users_run_before_it_starts(env, tmp_path, monkeypatch):
    """The role gate (metadata.min_role) -- asked by this run_events itself, which does not call
    Agent.run_events: a plain user's request starts no run, an admin's does."""
    from agent_system.auth import database
    from agent_system.auth.models import UserCreate, UserRole
    from agent_system.config.models import AgentMetadata, AuthConfig
    from agent_system.core.request_context import register_request_user, release_request_user_tree

    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role in (("root", UserRole.ADMIN), ("bob", UserRole.USER)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    agent = env.fresh_agent(metadata=AgentMetadata(min_role="admin"))
    agent.system_config.auth = AuthConfig(enabled=True, database_path=str(tmp_path / "absent.db"))

    register_request_user("rq_bob", "bob")
    register_request_user("rq_root", "root")
    try:
        refused = [event async for event in agent.run_events("Nachtzug", request_id="rq_bob", session_id="s_bob")]
        assert [event["type"] for event in refused] == ["error", "end"], refused
        assert env.runs() == [], "a refused request started a machine run"
        assert "rq_bob" not in agent._request_manager._active_requests, "the refused request stayed registered"

        answer = final_of([event async for event in agent.run_events("Nachtzug", request_id="rq_root",
                                                                      session_id="s_root")])
    finally:
        release_request_user_tree("rq_bob")
        release_request_user_tree("rq_root")
    assert answer["type"] == "final", answer
    assert len(env.runs()) == 1


async def test_a_run_in_a_session_held_for_another_user_starts_no_machine_run(env):
    """The session metadata can be what an earlier run under the same id left: a run whose registered owner
    is not that user is refused before it starts anything (Agent._foreign_session). The check looks at no
    role -- this env runs with auth off, so "root" is just another name here."""
    from agent_system.core.request_context import register_request_user, release_request_user_tree

    agent = env.fresh_agent()
    agent._session_tracker.set_session_metadata("s_held", {"user_id": "alice", "agent_name": agent.name})
    register_request_user("rq_owner", "root")
    try:
        events = [event async for event in agent.run_events("Nachtzug", request_id="rq_owner", session_id="s_held")]
    finally:
        release_request_user_tree("rq_owner")

    assert [event["type"] for event in events] == ["error", "end"], events
    assert env.runs() == []


async def test_a_registered_owners_run_in_a_session_held_for_anonymous_starts_no_machine_run(env):
    """"anonymous" is what a run with no registered owner wrote: another user's session like any other."""
    from agent_system.core.request_context import register_request_user, release_request_user_tree

    agent = env.fresh_agent()
    agent._session_tracker.set_session_metadata("s_unowned", {"user_id": "anonymous", "agent_name": agent.name})
    register_request_user("rq_unowned", "root")
    try:
        events = [event async for event in agent.run_events("Nachtzug", request_id="rq_unowned",
                                                            session_id="s_unowned")]
    finally:
        release_request_user_tree("rq_unowned")

    assert [event["type"] for event in events] == ["error", "end"], events
    assert env.runs() == []


async def test_the_machine_run_stays_the_owners_when_the_sessions_metadata_is_rewritten(env):
    """The session's metadata is state of the session id -- a second POST /run on the same id rewrites it
    (SessionService.open_for_run) -- and the run asks for its user after its start event."""
    from agent_system.core.request_context import register_request_user, release_request_user_tree

    agent = env.fresh_agent()
    agent._session_tracker.set_session_metadata("s_run", {"user_id": "root", "agent_name": agent.name})
    register_request_user("rq_run", "root")
    events = []
    try:
        async for event in agent.run_events("Nachtzug", request_id="rq_run", session_id="s_run"):
            events.append(event)
            if event["type"] == "start":
                agent._session_tracker.set_session_metadata("s_run", {"user_id": "alice", "agent_name": agent.name})
    finally:
        release_request_user_tree("rq_run")

    assert final_of(events)["type"] == "final", events
    assert [row["user_id"] for row in env.runs()] == ["root"]
