"""Agent activities run the registered agent itself -- no SAM, no fixed list of spawnable agents.

The real ``RunManager`` and ``ScarabHiveBackend``; behind them ``AgentHost``: a
registry of ``FakeAgent``\\ s (``run_events`` is the one seam) with a REAL
``SessionTracker`` and ``SessionService``/``SessionManager`` on disk under
``tmp_path``. The runner never runs, as in production -- the SAM path failed
there with "Unknown tool: stategraph_sam_manage_sub_agent" because a runner that
never ran has no tool integration to find the SAM's tool in.

Mutation checks run (each turned the named tests red, then was restored from a copy):
- backend.agent_create: ``parent_session_id`` not passed                     -> test_an_agent_runs_directly_on_a_sub_session_of_the_run
- backend._run_agent: ``open_for_run`` skipped                                -> test_a_continue_after_a_restart_reads_the_conversation_from_disk
- backend._run_agent: ``save_session`` skipped                                -> test_a_continue_after_a_restart_reads_the_conversation_from_disk
- backend._run_agent: the vars not cleared before the call                    -> test_a_continue_holds_exactly_this_calls_vars
- backend._run_agent: the vars set on the run's session instead               -> test_parallel_agents_each_see_exactly_their_own_vars
- backend._instance_of_this_run: the parent check removed                     -> test_a_continue_reaches_only_this_runs_instances[another run]
- backend._instance_of_this_run: the agent check removed                      -> test_a_continue_reaches_only_this_runs_instances[another agent]
- backend._answer: an "error" event read as an answer                          -> test_a_failed_agent_run_fails_the_activity[error]
- backend._answer: the empty-answer check removed                              -> test_a_failed_agent_run_fails_the_activity[empty]
- backend._run_agent: the cancelled-token check without ``_token_for``         -> test_a_finally_activity_runs_its_agent_after_the_run_was_cancelled
- backend._run_agent: the cancelled-token check removed                        -> test_a_finally_activity_runs_its_agent_after_the_run_was_cancelled
- backend._agent: anything the registry returns accepted                       -> test_what_is_not_a_registered_agent_is_a_config_error[not an agent]
- backend._guarded: the sub-run's cancel_request removed                       -> test_a_timeout_or_terminate_stops_an_agent_inside_a_tool_call[both]
- backend._guarded: awaiting the task directly (``await work``)               -> test_a_timeout_or_terminate_stops_an_agent_inside_a_tool_call[both]
- backend._guarded: ``asyncio.shield`` instead of ``asyncio.wait``             -> test_a_timeout_or_terminate_stops_an_agent_inside_a_tool_call[both]
- backend._guarded: the grace loop removed                                     -> test_the_activity_ends_after_the_agent_run_it_stopped[both]
- backend._guarded: a second cancel ends the grace (``raise`` for ``continue``) -> test_the_activity_ends_after_the_agent_run_it_stopped[timeout_then_terminate]
- backend._run_agent: released at once although the run outlived its grace   -> test_an_agent_run_that_outlives_its_grace_keeps_the_instance_busy
- backend._run_agent: the busy check removed                                  -> test_one_instance_takes_one_call_at_a_time
- backend._run_agent: replace_session_context_vars removed                    -> test_the_stored_vars_are_the_last_calls_vars
- backend._agent: the config_check call removed                               -> test_the_runtime_refuses_what_sg007_refuses
- server._agent_names: returns []; no config-check filter; the configured names left out
                                                                               -> test_the_catalog_lists_the_agents_a_machine_may_run[bound]
- server.is_agent: no instance fallback for an unbound registry                -> test_the_catalog_lists_the_agents_a_machine_may_run[unbound]
- server.is_agent: an unbuilt declaration counted                             -> test_the_catalog_lists_the_agents_a_machine_may_run[bound]
- server._catalog: the fnmatch filter ignored                                  -> test_the_catalog_lists_the_agents_a_machine_may_run[both]
"""

from __future__ import annotations

import asyncio
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.stategraph.engine.backend import ScarabHiveBackend
from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, AgentHost, FakeAgent, Harness,
                                                         inside_a_tool_call, runnable, settle, until)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def machine(states: str, *, head: str = "") -> dict[str, str]:
    return {"m.yaml": "stategraph: 1\nid: m\n" + textwrap.dedent(head) + "initial: a\nstates:\n"
                      + textwrap.indent(textwrap.dedent(states), "  ")}


def backend_for(host: AgentHost, **options: Any):
    def make(run_id: str) -> ScarabHiveBackend:
        return ScarabHiveBackend(runner=host, system_config=None, session_id=f"sg_{run_id}", user_id="ann",
                                 **options)
    return make


async def run(harness: Harness, host: AgentHost, files: dict[str, str], **start: Any) -> tuple[str, dict]:
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend_factory=backend_for(host), **start)
    return run_id, await settle(manager, run_id)


CREATE_THEN_CONTINUE = """\
a:
  do: {agent: writer, task: draft it, vars: {phase: draft, strict: true}}
  transitions: [{target: b, effect: "ctx.inst = activity.instance_id"}]
b:
  do: {agent: writer, continue: "{{ ctx.inst }}", task: shorten it}
  transitions: [{target: done}]
done: {type: final}
"""


async def test_an_agent_runs_directly_on_a_sub_session_of_the_run(harness, tmp_path):
    """No SAM anywhere in the configuration, and the runner never ran: the agent answers all the same."""
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    run_id, row = await run(harness, host, machine("""\
        a:
          do: {agent: writer, task: say hello}
          transitions: [{target: done, effect: "ctx.answer = out"}]
        done: {type: final, output: "{{ ctx.answer }}"}
        """, head="context: {answer: null}\n"))

    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == "writer answers say hello"
    [call] = host.calls()
    stored = await host.sessions.load_session("ann", call["session"])
    assert stored["parent_session"]["session_id"] == f"sg_{run_id}", "a sub-session: kept out of the session list"
    assert [m["content"] for m in stored["messages"]] == ["say hello", "writer answers say hello"]
    assert call["request_id"].startswith(f"{run_id}_"), "a terminate cancels the run's sub-requests by this prefix"


async def test_an_agent_activity_carries_what_its_calls_and_its_sub_agents_cost_over_its_feedback_rounds(
        harness, tmp_path, monkeypatch):
    from agent_system.llm import pricing

    monkeypatch.setattr(pricing, "estimate_cost",
                        lambda model, *a, is_batch=False, **k: (0.005 if is_batch else 0.01) if model == "m-table" else None)

    def used(prompt, completion, cost=None):
        return {"prompt_tokens": prompt, "completion_tokens": completion, **({"cost": cost} if cost is not None else {})}

    last = used(200, 20, 0.25)
    agent = FakeAgent("writer", lambda call: '{"ok": true}' if len(agent.calls) > 1 else "not json", spent=[
        {"type": "thinking_complete", "usage": used(100, 10, 0.5), "model": "m-billed"},
        {"type": "sub_run", "run_id": "r1", "event": {"type": "thinking_complete", "usage": used(50, 5), "model": "m-table",
                                                "batch": True}},
        {"type": "sub_run", "run_id": "r1", "event": {"type": "final", "usage": used(50, 5)}},  # its last call again
        {"type": "sub_run", "run_id": "r2", "event": {"type": "thinking_complete", "usage": used(7, 1), "model": "m-none"}},
        {"type": "sub_run", "run_id": "r3", "event": {"type": "thinking_complete", "usage": used(10, 1), "model": "m-table",
                                                    "batch": True}},
        {"type": "sub_run", "run_id": "r3", "event": {"type": "final", "usage": used(20, 2)}},  # a call of its own
        {"type": "thinking_complete", "usage": last, "model": "m-billed"}], final_usage=last)  # the last call again
    host = AgentHost(tmp_path / "sessions", agent)

    run_id, row = await run(harness, host, machine("""\
        a:
          do: {agent: writer, task: rate it, schema: {type: object}}
          transitions: [{target: done}]
        done: {type: final}
        """))

    assert row["status"] == "succeeded", row["error"]
    meta = next(r for r in harness.store.rows(run_id, kinds=("activity",)) if r["state"] == "a")["data"]["meta"]
    assert meta["feedback_rounds"] == 1, "fixture: two runs of the agent"
    assert meta["tokens"] == {"prompt": 2 * 387, "completion": 2 * 39, "cached": 0}, meta
    assert (meta["cost"], meta["cost_is_estimate"], meta["cost_unpriced_calls"], meta["model"]) == (
        2 * 0.765, True, 2, "m-billed"), meta  # r3's final priced as its run's calls: batch


async def test_a_continue_carries_the_instance_s_conversation(harness, tmp_path):
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    _, row = await run(harness, host, machine(CREATE_THEN_CONTINUE, head="context: {inst: null}\n"))

    assert row["status"] == "succeeded", row["error"]
    first, second = host.calls()
    assert second["session"] == first["session"]
    assert second["history"] == ["draft it", "writer answers draft it"]


async def test_a_continue_holds_exactly_this_calls_vars(harness, tmp_path):
    """§3.9: an instance session holds the call's effective vars -- replaced, never accumulated."""
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    _, row = await run(harness, host, machine(CREATE_THEN_CONTINUE, head="context: {inst: null}\n"))

    assert row["status"] == "succeeded", row["error"]
    assert [call["vars"] for call in host.calls()] == [{"phase": "draft", "strict": True}, {}]


async def test_parallel_agents_each_see_exactly_their_own_vars(harness, tmp_path):
    """Each instance has its own session: concurrent calls with different vars do not race (SG109 is gone)."""
    both_in = asyncio.Event()
    entered: list[str] = []

    async def answer(call: dict) -> str:
        entered.append(call["task"])
        if len(entered) == 2:
            both_in.set()
        await both_in.wait()
        return f"done {call['task']}"

    host = AgentHost(tmp_path / "sessions", FakeAgent("writer", answer))
    _, row = await run(harness, host, machine("""\
        a:
          do:
            parallel:
              one: {agent: writer, task: one, vars: {phase: synopsis}}
              two: {agent: writer, task: two, vars: {phase: outline, extra: 1}}
          transitions: [{target: done}]
        done: {type: final}
        """, head="vars: {genre: thriller}\n"))

    assert row["status"] == "succeeded", row["error"]
    seen = {call["task"]: call["vars"] for call in host.calls()}
    assert seen == {"one": {"genre": "thriller", "phase": "synopsis"},
                    "two": {"genre": "thriller", "phase": "outline", "extra": 1}}


def activity(path: str = "a", *, finalizer: bool = False) -> Any:
    counter = iter(range(1, 100))
    return SimpleNamespace(path=path, finalizer=finalizer, meta={},
                           request_id=lambda: f"r1_{next(counter):03d}")


async def test_a_continue_after_a_restart_reads_the_conversation_from_disk(tmp_path):
    """Another process (a resumed run) continues the instance: its conversation comes from the stored session."""
    first = ScarabHiveBackend(runner=AgentHost(tmp_path / "s", FakeAgent("writer")), system_config=None,
                              session_id="sg_r1", user_id="ann")
    _, instance = await first.agent_create(activity(), agent="writer", task="draft it", advanced=False, vars={})

    restarted = AgentHost(tmp_path / "s", FakeAgent("writer"))
    second = ScarabHiveBackend(runner=restarted, system_config=None, session_id="sg_r1", user_id="ann")
    await second.agent_continue(activity("b"), agent="writer", instance_id=instance, message="shorten it",
                                advanced=False, vars={})

    [call] = restarted.calls()
    assert call["history"] == ["draft it", "writer answers draft it"]


@pytest.mark.parametrize("case", ["another run", "another agent", "no such session"])
async def test_a_continue_reaches_only_this_runs_instances(tmp_path, case):
    host = AgentHost(tmp_path / "s", FakeAgent("writer"), FakeAgent("critic"))
    other = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_other", user_id="ann")
    mine = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann")
    owner = other if case == "another run" else mine
    _, instance = await owner.agent_create(activity(), agent="writer", task="t", advanced=False, vars={})
    if case == "no such session":
        instance = "sub_nothing_here"

    with pytest.raises(ActivityError) as refused:
        await mine.agent_continue(activity("b"), agent="critic" if case == "another agent" else "writer",
                                  instance_id=instance, message="m", advanced=False, vars={})
    assert refused.value.type == "config", refused.value
    assert len(host.calls()) == 1, "nothing ran for the refused continue"


@pytest.mark.parametrize("case", ["error", "empty"])
async def test_a_failed_agent_run_fails_the_activity(harness, tmp_path, case):
    answer = RuntimeError("model down") if case == "error" else "   "
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer", answer))
    _, row = await run(harness, host, machine("""\
        a:
          do: {agent: writer, task: t}
          transitions: [{target: done}]
        done: {type: final}
        """))

    assert row["status"] == "failed"
    assert row["error"]["type"] == "agent_failed", row["error"]
    assert ("model down" if case == "error" else "without an answer") in row["error"]["message"]


async def test_a_finally_activity_runs_its_agent_after_the_run_was_cancelled(tmp_path):
    """A cancel from outside cancels the run's token; an agent a finally calls must still be reached (§3.10)."""
    host = AgentHost(tmp_path / "s", FakeAgent("writer"))
    token = SimpleNamespace(is_cancelled=True)
    backend = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann", token=token)

    with pytest.raises(asyncio.CancelledError):
        await backend.agent_create(activity(), agent="writer", task="t", advanced=False, vars={})
    text, _ = await backend.agent_create(activity("fin", finalizer=True), agent="writer", task="clean up",
                                         advanced=False, vars={})
    assert text == "writer answers clean up"
    assert [call["task"] for call in host.calls()] == ["clean up"]


@pytest.mark.parametrize("case", ["unknown", "not an agent"])
async def test_what_is_not_a_registered_agent_is_a_config_error(tmp_path, case):
    host = AgentHost(tmp_path / "s")
    if case == "not an agent":
        host.agents["writer_json"] = SimpleNamespace(name="writer_json")  # a tool server under that name
    backend = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann")

    with pytest.raises(ActivityError) as refused:
        await backend.agent_create(activity(), agent="writer_json", task="t", advanced=False, vars={})
    assert refused.value.type == "config"
    assert "not configured, not enabled, or not an agent" in str(refused.value)


@pytest.fixture
def short_grace(monkeypatch):
    """A run that ignores its token would hold a test for the stop grace (12 s by default)."""
    from plugins.stategraph.engine import backend

    monkeypatch.setattr(backend, "AGENT_STOP_GRACE", 1.0)


@pytest.mark.parametrize("how", ["terminate", "timeout"])
async def test_a_timeout_or_terminate_stops_an_agent_inside_a_tool_call(harness, tmp_path, short_grace, how,
                                                                       caplog):
    """A cancel that lands in an agent's tool call is swallowed there; the agent run's token stops it."""
    loop = asyncio.get_running_loop()
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer", inside_a_tool_call))
    timeout = ", timeout: 0.3" if how == "timeout" else ""
    manager = harness.manager()
    run_id = await manager.start(runnable(machine(f"""\
        a:
          do: {{agent: writer, task: long{timeout}}}
          transitions: [{{target: done}}]
        done: {{type: final}}
        """)), backend_factory=backend_for(host))
    await until(lambda: host.calls(), what="the agent run")
    started = loop.time()
    if how == "terminate":
        manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    [call] = host.calls()
    assert call["token"].is_cancelled, "the agent run's own token was never cancelled"
    assert loop.time() - started < 0.9, "the activity waited out the grace instead of stopping the agent"
    if how == "terminate":
        assert row["status"] == "cancelled", row
    else:
        assert row["status"] == "failed" and row["error"]["type"] == "timeout", row
        assert call.get("swallowed"), "fixture: the cancel landed inside the tool call"
    assert not [r for r in caplog.records if r.levelname == "ERROR"], "a stopped agent run is no error to log"


@pytest.mark.parametrize("second_cancel", [False, True], ids=["timeout", "timeout_then_terminate"])
async def test_the_activity_ends_after_the_agent_run_it_stopped(harness, tmp_path, short_grace, second_cancel):
    """An agent that needs a moment to stop gets it -- also when a terminate comes meanwhile: the run does
    not end while its agent still works."""
    async def answer(call: dict) -> str:
        return await inside_a_tool_call(call, wind_down=0.4)

    host = AgentHost(tmp_path / "sessions", FakeAgent("writer", answer))
    manager = harness.manager()
    run_id = await manager.start(runnable(machine("""\
        a:
          do: {agent: writer, task: long, timeout: 0.2}
          transitions: [{target: done}]
        done: {type: final}
        """)), backend_factory=backend_for(host))
    await until(lambda: host.calls() and host.calls()[0]["token"].is_cancelled, what="the timeout")
    if second_cancel:
        manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    [call] = host.calls()
    assert call.get("ended"), "the activity ended while its agent run still worked"
    assert row["status"] == ("cancelled" if second_cancel else "failed"), row


async def test_an_agent_run_that_outlives_its_grace_keeps_the_instance_busy(tmp_path, monkeypatch):
    from plugins.stategraph.engine import backend as backend_module

    monkeypatch.setattr(backend_module, "AGENT_STOP_GRACE", 0.1)

    async def answer(call: dict) -> str:
        if call["task"] == "stubborn":
            return await inside_a_tool_call(call, ignore_token_for=0.5)
        return "ok"

    host = AgentHost(tmp_path / "s", FakeAgent("writer", answer))
    backend = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann")
    _, instance = await backend.agent_create(activity(), agent="writer", task="t", advanced=False, vars={})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend.agent_continue(activity("b"), agent="writer", instance_id=instance,
                                                      message="stubborn", advanced=False, vars={}), 0.05)
    stubborn = host.calls()[-1]
    assert not stubborn.get("ended"), "fixture: the agent run outlived its grace"

    with pytest.raises(ActivityError) as refused:
        await backend.agent_continue(activity("c"), agent="writer", instance_id=instance, message="meanwhile",
                                     advanced=False, vars={})
    assert "already running" in str(refused.value)
    await until(lambda: stubborn.get("ended"), what="the stubborn run's end")
    await asyncio.sleep(0)  # its done callback
    assert await backend.agent_continue(activity("d"), agent="writer", instance_id=instance, message="after",
                                        advanced=False, vars={}) == "ok"


async def test_one_instance_takes_one_call_at_a_time(tmp_path):
    gate = asyncio.Event()

    async def answer(call: dict) -> str:
        if call["task"] == "first":
            await gate.wait()
        return "ok"

    host = AgentHost(tmp_path / "s", FakeAgent("writer", answer))
    backend = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann")
    _, instance = await backend.agent_create(activity(), agent="writer", task="t", advanced=False, vars={})
    first = asyncio.ensure_future(backend.agent_continue(activity("b"), agent="writer", instance_id=instance,
                                                         message="first", advanced=False, vars={}))
    await until(lambda: len(host.calls()) == 2, what="the first continue")
    try:
        with pytest.raises(ActivityError) as refused:
            await backend.agent_continue(activity("c"), agent="writer", instance_id=instance, message="second",
                                         advanced=False, vars={})
        assert refused.value.type == "config" and "already running" in str(refused.value)
    finally:
        gate.set()
        await first
    assert [call["task"] for call in host.calls()] == ["t", "first"]


async def test_the_stored_vars_are_the_last_calls_vars(tmp_path):
    """A save merges into the stored vars; the instance's own SAM hands the stored ones to its sub-agents."""
    host = AgentHost(tmp_path / "s", FakeAgent("writer"))
    backend = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann")
    _, instance = await backend.agent_create(activity(), agent="writer", task="t", advanced=False,
                                             vars={"phase": "draft", "strict": True})
    await backend.agent_continue(activity("b"), agent="writer", instance_id=instance, message="m",
                                 advanced=False, vars={"genre": "thriller"})

    stored = await host.sessions.load_session("ann", instance)
    assert stored["context_vars"] == {"genre": "thriller"}


def test_the_runtime_refuses_what_sg007_refuses(tmp_path):
    """A force-saved machine never met the validator: the backend asks the same check before running."""
    host = AgentHost(tmp_path / "s", FakeAgent("stategraph_author"))
    check = lambda what, name, extra: "controls machines" if (what, name) == ("agent", "stategraph_author") else None  # noqa: E731
    backend = ScarabHiveBackend(runner=host, system_config=None, session_id="sg_r1", user_id="ann",
                                config_check=check)

    with pytest.raises(ActivityError) as refused:
        asyncio.run(backend.agent_create(activity(), agent="stategraph_author", task="t", advanced=False,
                                         vars={}))
    assert refused.value.type == "config" and "controls machines" in str(refused.value)
    assert host.calls() == []


# ------------------------------------------------------------------ the catalog: the agents there are

CATALOG_SERVERS = {
    "stategraph": {"type": "stategraph", "enabled": True},
    "stategraph_runner": {"type": "basic_agent", "enabled": True, "description": "hosts runs"},
    "writer": {"type": "basic_agent", "enabled": True, "description": "writes"},
    "v6_critic": {"type": "basic_agent", "enabled": True, "description": "criticises"},
    "author": {"type": "basic_agent", "enabled": True,
               "agent_config": {"tools": {"allowed": ["stategraph/stategraph_save_machine"]}}},
    "story_machine": {"type": "stategraph_machine", "enabled": True, "machine": "m"},
    "notes": {"type": "json_store", "enabled": True},
    "lazy_one": {"type": "basic_agent", "enabled": True},
    "lazy_built": {"type": "basic_agent", "enabled": True},
}


class CatalogRegistry:
    """A registry as the runtime binds it: built servers in list(), describe() from instance or declaration."""

    def __init__(self, bound: bool):
        from agent_system.servers.agent.server import Agent

        self.bound = bound
        agent = Agent.__new__(Agent)  # an Agent for isinstance; nothing of it runs here
        self.servers = {"stategraph_runner": agent, "writer": agent, "v6_critic": agent, "author": agent,
                        "story_machine": agent, "notes": object()}

    def list(self) -> list[str]:
        return list(self.servers)

    def get(self, name: str) -> Any:
        return self.servers[name]

    def describe(self, name: str) -> Any:
        from agent_system.runtime import ServerView

        if not self.bound:
            return None  # unbound: the caller asks the instance
        if name == "lazy_one":  # declared lazy, its build failed at start: nothing to run
            return ServerView(name=name, is_agent=True, tool_public=False, tool_visible=False, built=False)
        if name == "lazy_built":  # a lazy agent the runtime has built
            return ServerView(name=name, is_agent=True, tool_public=False, tool_visible=False, built=True)
        if name not in self.servers:
            return None
        return ServerView(name=name, is_agent=name != "notes", tool_public=True, tool_visible=True, built=True)


@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
async def test_the_catalog_lists_the_agents_a_machine_may_run(tmp_path, bound):
    from agent_system.config.models import AgentSystemConfig

    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    config = AgentSystemConfig.model_validate({"plugins": {"servers": CATALOG_SERVERS}})
    server = StateGraphServer("stategraph", config, tool_config(tmp_path))
    server.use_registry(CatalogRegistry(bound))
    try:
        every = {agent["name"]: agent["description"] for agent in server._catalog()["agents"]}
        narrowed = [agent["name"] for agent in server._catalog("v6_*")["agents"]]
    finally:
        await server.stop_plugin()

    expected = {"writer": "writes", "v6_critic": "criticises"}
    if bound:
        expected["lazy_built"] = ""  # from its declaration; lazy_one's build failed: not listed
    assert every == expected, "not the runner, a facade, an agent that controls machines or a tool server"
    assert narrowed == ["v6_critic"]
