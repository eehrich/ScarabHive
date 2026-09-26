"""A run is a session: the session list shows it, with its agents' conversations below it.

A run's agent instances were sub-sessions of ``sg_<run id>`` -- a session nobody created. So a running machine was
nowhere in the session list, and its agents' conversations hung from a parent that did not exist. The run now
creates that session: at the top of its user's list, or below the session of the agent that started it; it says
what was asked and, at the end, how the run ended.
"""
from __future__ import annotations

import pytest

from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, AgentHost, FakeAgent, Harness, runnable, settle
from plugins.stategraph.tests.test_plugin_stategraph_agents import backend_for, machine

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

WRITE = machine("""\
    a:
      do: {agent: writer, task: "{{ params.topic }}"}
      transitions: [{target: done, effect: "ctx.answer = out"}]
    done: {type: final, output: "{{ ctx.answer }}"}
    """, head="title: Write it\nparams: {topic: {type: string}}\ncontext: {answer: null}\n")


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def start(harness: Harness, host: AgentHost, files: dict[str, str], nesting=None, **options):
    """A run with a backend over ``host``; ``nesting`` goes to the backend as service.backend_factory takes it
    from the run's row."""
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend_factory=backend_for(host, nesting=nesting), nesting=nesting,
                                 **options)
    return run_id, await settle(manager, run_id)


async def test_a_run_is_a_session_of_its_user_with_its_agents_below_it(harness, tmp_path):
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))

    run_id, row = await start(harness, host, WRITE, params={"topic": "a storm"})

    assert row["status"] == "succeeded", row["error"]
    session = f"sg_{run_id}"
    roots = {s["session_id"]: s for s in await host.sessions.list_root_sessions("ann")}
    assert session in roots and roots[session]["title"] == f"Write it · {run_id}"
    [child] = await host.sessions.list_child_sessions("ann", session)
    assert child["agent_name"] == "writer"
    asked, ended = (await host.sessions.load_session("ann", session))["messages"]
    assert (asked["role"], asked["injected_by"]) == ("user", "stategraph") and '"topic": "a storm"' in asked["content"]
    assert ended["role"] == "assistant" and "succeeded" in ended["content"] and "writer answers a storm" in ended["content"]


async def test_a_run_an_agent_started_hangs_below_that_agents_session(harness, tmp_path):
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    caller = await host.sessions.create_session(user_id="ann", title="the coordinator", agent_name="writer")

    run_id, row = await start(harness, host, WRITE, params={"topic": "x"},
                              nesting={"depth": 1, "depth_budget": None, "session": caller["session_id"]})

    assert row["status"] == "succeeded", row["error"]
    stored = await host.sessions.load_session("ann", f"sg_{run_id}")
    assert stored["parent_session"]["session_id"] == caller["session_id"]
    assert f"sg_{run_id}" not in {s["session_id"] for s in await host.sessions.list_root_sessions("ann")}


async def test_a_failed_run_says_why_in_its_session(harness, tmp_path):
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer", RuntimeError("provider down")))

    run_id, row = await start(harness, host, WRITE, params={"topic": "x"})

    assert row["status"] == "failed"
    ended = (await host.sessions.load_session("ann", f"sg_{run_id}"))["messages"][-1]
    assert "failed" in ended["content"] and "agent_failed" in ended["content"], ended["content"]


async def test_a_run_without_a_backend_keeps_no_session(harness, tmp_path):
    """Mock-only runs -- the author's test runs -- have no backend: nothing reaches the session list."""
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    manager = harness.manager()

    run_id = await manager.start(runnable(WRITE), params={"topic": "x"}, mocks={"a": "mocked"}, mock_only=True)
    row = await settle(manager, run_id)

    assert row["status"] == "succeeded", row["error"]
    assert await host.sessions.list_root_sessions("ann") == []


async def test_a_shutdown_right_after_the_end_was_stored_still_tells_the_session(harness, tmp_path, monkeypatch):
    """The end is stored, then the session hears of it: a shutdown between the two cancelled the second, and a
    run that ended cannot be resumed -- nothing would ever write it."""
    import asyncio

    from plugins.stategraph.engine.backend import ScarabHiveBackend

    told = ScarabHiveBackend.run_ended
    stored = asyncio.Event()

    async def slow(self, **ending):
        stored.set()
        await asyncio.sleep(0.2)
        await told(self, **ending)

    monkeypatch.setattr(ScarabHiveBackend, "run_ended", slow)
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    manager = harness.manager()
    run_id = await manager.start(runnable(WRITE), params={"topic": "x"}, backend_factory=backend_for(host))
    await asyncio.wait_for(stored.wait(), 5)

    await manager.shutdown()

    ended = (await host.sessions.load_session("ann", f"sg_{run_id}"))["messages"][-1]
    assert ended["role"] == "assistant" and "succeeded" in ended["content"], ended
