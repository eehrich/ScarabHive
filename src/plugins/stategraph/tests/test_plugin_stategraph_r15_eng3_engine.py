"""Review 2026-10-15, engine cluster round 3: cross-run semaphores (FEAT-2), what a resume's replay stores and
journals (RES-3/PROD-2, DBG-8, runner side) and a param set on a stopped run (LC-R1).

Every run goes through the real RunManager on a RunStore under tmp_path (or the real StateGraphServer); only the
boundary behind the engine is FakeBackend. A "crash" is a real ``RunManager.shutdown()``, a resume a NEW manager.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_system.config.models import AgentSystemConfig, PluginsConfig
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, held,
                                                         runnable, settle, tool_config, until, validate)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def lifecycle(store, run_id: str) -> list[dict]:
    return [r for r in store.rows(run_id) if r["kind"] == "lifecycle"]


# ------------------------------------------------------------------ RES-3 / DBG-8: a resume's replay

TICKS = 30

LOOP = f"""\
stategraph: 1
id: m
context: {{n: 0}}
initial: tick
states:
  tick:
    do: {{agent: w, task: tick}}
    transitions:
      - target: tick
        guard: ctx.n < {TICKS}
        effect: ctx.n = ctx.n + 1
      - target: block
  block:
    do: {{agent: w, task: block}}
    transitions: [{{target: done}}]
  done: {{type: final, output: "{{{{ ctx.n }}}}"}}
"""


async def _crashed_loop(harness: Harness) -> str:
    backend = FakeBackend({"tick": "ok", "block": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": LOOP}), backend=backend)
    await until(lambda: backend.count("block") == 1, what="block in flight")
    await manager.shutdown()
    assert harness.store.get_run(run_id)["status"] == "interrupted", "fixture: the stop interrupts the run"
    return run_id


async def test_res3_a_resume_stores_the_view_once_it_goes_live_not_once_per_replayed_step(harness):
    run_id = await _crashed_loop(harness)
    views = []
    original = harness.store.update_run

    def counted(run, **fields):
        if run == run_id and "view" in fields:
            views.append([f.get("state") for f in fields["view"]["frames"]])
        return original(run, **fields)

    harness.store.update_run = counted
    manager = harness.manager()
    gate = asyncio.Event()
    await manager.resume(run_id, backend=FakeBackend({"tick": "ok", "block": held(gate)}))
    await until(lambda: views and views[-1] == ["block"], what="the view of the live run")

    # the frame's start and its initial state, then the view where it went live -- not one per replayed step
    assert len(views) <= 4, f"{len(views)} view writes for {TICKS} replayed steps: {views}"
    assert harness.store.get_run(run_id)["view"]["frames"][0]["state"] == "block"
    gate.set()
    assert (await settle(manager, run_id))["output"] == TICKS


async def test_dbg8_a_resume_journals_where_it_went_live_and_how_many_rows_it_replayed(harness):
    run_id = await _crashed_loop(harness)
    journal = len(harness.store.rows(run_id, kinds=("activity", "event", "edit", "timer", "trace")))
    manager = harness.manager()
    await manager.resume(run_id, backend=FakeBackend({"tick": "ok", "block": "done"}))
    assert (await settle(manager, run_id))["status"] == "succeeded"

    live = [r["data"] for r in lifecycle(harness.store, run_id) if r["status"] == "live"]
    assert len(live) == 1, live
    assert live[0]["key"] == f"s{TICKS + 1}" and live[0]["rows"] == journal and live[0]["process"] == manager.owner


# ------------------------------------------------------------------ FEAT-2: semaphores

SLOT = """\
stategraph: 1
id: m
context: {slot: null}
initial: take
states:
  take:
    do: {acquire: p}
    transitions: [{target: work, effect: ctx.slot = out}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.slot }}"}
"""

#: the same, bounded to one second of running time
TIMED = SLOT.replace("context:", "limits: {timeout: 1s}\ncontext:")

RELEASED = """\
stategraph: 1
id: m
context: {slot: null, back: null}
initial: take
states:
  take:
    do: {acquire: p}
    transitions: [{target: give, effect: ctx.slot = out}]
  give:
    do: {release: p}
    transitions: [{target: work, effect: ctx.back = out}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final, output: {slot: "{{ ctx.slot }}", back: "{{ ctx.back }}"}}
"""


def slots(manager, capacity: int = 1, hold: float = 300.0):
    manager.semaphores = {"p": capacity}
    manager.semaphore_hold = hold
    return manager


async def test_feat2_a_full_semaphore_makes_a_run_wait_without_running_time_until_the_holder_ends(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    first = await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(gate)}))
    backend = FakeBackend({"work": "done"})
    second = await manager.start(runnable({"m.yaml": TIMED}), backend=backend)
    await until(lambda: manager.live[second].ctx.status == "waiting", what="the second run waits")

    waits = harness.store.get_run(second)["view"]["slot_waits"]
    assert [w["semaphore"] for w in waits] == ["p"], waits
    await asyncio.sleep(1.2)  # past its limits.timeout of 1s: a wait for a slot is no running time
    assert manager.live[second].ctx.status == "waiting" and backend.count("work") == 0
    gate.set()  # the holder's frame ends: its slot is free

    assert (await settle(manager, first))["output"] == 0
    row = await settle(manager, second)
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]
    assert harness.store.semaphore_holders("p") == []  # both frames ended: nothing held


async def test_feat2_release_gives_the_slot_back_before_the_frame_ends(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    first = await manager.start(runnable({"m.yaml": RELEASED}), backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, first).get("s2", {}).get("status") == "started",
                what="the first run works after its release")

    second = await manager.start(runnable({"m.yaml": RELEASED}), backend=FakeBackend({"work": "done"}))
    row = await settle(manager, second)  # the first one still runs: the slot is free all the same

    assert row["status"] == "succeeded" and row["output"] == {"slot": 0, "back": 0}, row["error"] or row["output"]
    gate.set()
    assert (await settle(manager, first))["status"] == "succeeded"


async def test_feat2_an_unknown_semaphore_fails_the_run_with_config_and_warns_in_validation(harness, tmp_path):
    manager = slots(harness.manager())
    manager.semaphores = {"other": 1}
    row = await settle(manager, await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend()))
    assert row["status"] == "failed" and row["error"]["type"] == "config", row["error"]
    assert "semaphore 'p' is not configured" in row["error"]["message"]

    server = StateGraphServer("stategraph", AgentSystemConfig(plugins=PluginsConfig(servers={})),
                              tool_config(tmp_path, semaphores={"other": 2}))
    try:
        checked = validate({"m.yaml": SLOT}, config_check=server.service.config_check())
    finally:
        await server.stop_plugin()
    found = [(p.level, p.code, p.message) for p in checked.problems if "semaphore" in p.message]
    assert [(level, code) for level, code, _ in found] == [("warning", "SG007")], found
    assert "'p' is not in stategraph's semaphores setting (known: other)" in found[0][2]


async def test_feat2_a_pause_takes_effect_while_a_run_waits_for_a_slot(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    first = await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(gate)}))
    second = await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": "done"}))
    await until(lambda: manager.live[second].ctx.status == "waiting", what="the second run waits")

    manager.control(second, "pause")
    await until(lambda: manager.live[second].ctx.status == "paused", what="the waiting run pauses")
    paused = harness.store.get_run(second)["debug"]["paused"]
    assert (paused["state"], paused["hook"]) == ("take", "wait"), paused
    with pytest.raises(ValueError, match="semaphore slot"):  # no hook a replay could apply an edit at
        manager.assign(second, "ctx.slot", "5")
    gate.set()
    assert (await settle(manager, first))["status"] == "succeeded"
    await asyncio.sleep(SLOT_POLL)
    assert manager.live[second].ctx.status == "paused"  # the free slot does not end the pause

    manager.control(second, "continue")
    row = await settle(manager, second)
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]


SLOT_POLL = 1.2  # a little over runner.SLOT_POLL_SECONDS: one more try of a waiting acquire


async def test_feat2_an_interrupted_holder_keeps_its_slot_for_the_hold_then_waits_to_take_it_back(harness):
    crashed = slots(harness.manager())
    holder = await crashed.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(asyncio.Event())}))
    await until(lambda: activity_rows(harness.store, holder).get("s1", {}).get("status") == "started",
                what="the holder works with its slot")
    await crashed.shutdown()
    assert harness.store.get_run(holder)["status"] == "interrupted"

    other = slots(harness.manager())  # another process: within the hold the interrupted holder keeps the slot
    gate = asyncio.Event()
    taker = await other.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(gate)}))
    await asyncio.sleep(SLOT_POLL)
    assert other.live[taker].ctx.status == "waiting"
    other.semaphore_hold = 0  # read at every try: the hold has run out now
    await until(lambda: activity_rows(harness.store, taker).get("s1", {}).get("status") == "started",
                timeout=3, what="the other run takes the slot")

    back = slots(harness.manager(), hold=0)
    await back.resume(holder, backend=FakeBackend({"work": "done"}))
    await until(lambda: back.live[holder].ctx.status == "waiting", what="the resumed holder waits for its slot")
    waits = harness.store.get_run(holder)["view"]["slot_waits"]
    assert [(w["semaphore"], w["slot"]) for w in waits] == [("p", 0)], waits
    gate.set()

    assert (await settle(other, taker))["output"] == 0
    row = await settle(back, holder)
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]
    held_again = [r["data"] for r in lifecycle(harness.store, holder) if r["status"] == "slots"]
    assert [[(h["name"], h["slot"]) for h in r["held"]] for r in held_again] == [[("p", 0)]], held_again


async def test_feat2_a_fork_past_an_acquire_waits_for_the_slot_its_prefix_chose(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    source = await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, source).get("s1", {}).get("status") == "started",
                what="the source works with slot 0")

    backend = FakeBackend({"work": "done"})
    fork = await manager.fork(source, at_step=1, backend=backend)  # its replayed acquire chose slot 0
    await until(lambda: manager.live[fork].ctx.status == "waiting", what="the fork waits for slot 0")
    assert backend.count("work") == 0  # not beside the source on the same slot
    gate.set()

    assert (await settle(manager, source))["status"] == "succeeded"
    row = await settle(manager, fork)
    assert row["status"] == "succeeded" and row["output"] == 0 and backend.count("work") == 1, row["error"]


async def test_feat2_a_fork_past_a_release_holds_no_slot(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    source = await manager.start(runnable({"m.yaml": RELEASED}), backend=FakeBackend({"work": "done"}))
    assert (await settle(manager, source))["status"] == "succeeded"
    fork = await manager.fork(source, at_step=2, backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, fork).get("s2", {}).get("status") == "started",
                what="the fork works past its replayed release")

    other = await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": "done"}))
    row = await settle(manager, other)  # the slot the fork's prefix took and gave back is free

    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]
    gate.set()
    assert (await settle(manager, fork))["status"] == "succeeded"


# ------------------------------------------------------------------ server: config, reload, LC-R1

TOPIC = """\
stategraph: 1
id: topic
python: topic.py
params: {topic: {type: string, default: none}}
context: {first: null, after: null}
initial: first
states:
  first:
    do: {call: echo, args: {text: "{{ params.topic }}"}}
    transitions: [{target: work, effect: ctx.first = out}]
  work:
    do: {call: block, args: {}}
    transitions: [{target: after}]
  after:
    do: {call: echo, args: {text: "{{ params.topic }}"}}
    transitions: [{target: done, effect: ctx.after = out}]
  done: {type: final, output: {first: "{{ ctx.first }}", after: "{{ ctx.after }}"}}
"""

TOPIC_PY = """\
import asyncio
import os


def echo(text):
    return text


async def block():
    while not os.path.exists(os.environ["SG_ENG3_GATE"]):
        await asyncio.sleep(0.01)
    return 1
"""

SERVER_SLOT = """\
stategraph: 1
id: slot
python: topic.py
initial: take
context: {slot: null}
states:
  take:
    do: {acquire: p}
    transitions: [{target: work, effect: ctx.slot = out}]
  work:
    do: {call: block, args: {}}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.slot }}"}
"""


@pytest.fixture
def gate(tmp_path, monkeypatch):
    path = tmp_path / "gate"
    monkeypatch.setenv("SG_ENG3_GATE", str(path))
    return path


@pytest.fixture
async def servers(tmp_path, gate):
    made: list[StateGraphServer] = []
    machines = tmp_path / "machines"
    machines.mkdir(exist_ok=True)
    for name, text in (("topic.yaml", TOPIC), ("topic.py", TOPIC_PY), ("slot.yaml", SERVER_SLOT)):
        (machines / name).write_text(text, encoding="utf-8")

    def make(**config):
        made.append(StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path, **config)))
        return made[-1]

    yield make
    for server in made:
        await server.stop_plugin()


async def test_lc_r1_a_param_set_on_a_stopped_run_holds_from_where_it_stands(servers, gate):
    first, second = servers(), servers()
    run_id = (await first.service.start_run("topic"))["run_id"]
    await until(lambda: activity_rows(first.run_store, run_id).get("s1", {}).get("status") == "started",
                what="work running")
    await first.run_manager.shutdown()
    assert first.run_store.get_run(run_id)["status"] == "interrupted"

    # s0 read the param before the stop: the resume replays it with the value it ran with
    assert await second.service.control_run(run_id, "set", path="params.topic", expr='"news"', user_id="ops") \
        == {"value": "news"}
    gate.touch()
    await second.service.control_run(run_id, "resume")
    row = await settle(second.run_manager, run_id)

    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == {"first": "none", "after": "news"}, row["output"]
    assert row["params"] == {} or row["params"].get("topic") != "news"  # the start's: the steps before read them


async def test_feat2_a_config_reload_changes_a_semaphores_capacity_for_a_waiting_run(servers, gate, tmp_path):
    server = servers(semaphores={"p": 1})
    one = (await server.service.start_run("slot"))["run_id"]
    await until(lambda: activity_rows(server.run_store, one).get("s1", {}).get("status") == "started",
                what="the first run holds the slot")
    two = (await server.service.start_run("slot"))["run_id"]
    await until(lambda: server.run_manager.live[two].ctx.status == "waiting", what="the second run waits")

    changed = server.reload_config(tool_config(tmp_path, semaphores={"p": 2}))

    assert changed["semaphores"] == {"old": {"p": 1}, "new": {"p": 2}}, changed
    await until(lambda: activity_rows(server.run_store, two).get("s1", {}).get("status") == "started",
                timeout=3, what="the second run takes the new slot")
    gate.touch()
    assert [(await settle(server.run_manager, r))["output"] for r in (one, two)] == [0, 1]


# ------------------------------------------------------------------ fix round: semaphores

FINALLY_SLOT = SLOT.replace("initial: take", "initial: take\nfinally: {agent: w, task: fin}")


async def test_feat2_a_terminated_holder_whose_slot_was_taken_ends_at_once_with_its_finally(harness):
    crashed = slots(harness.manager())
    holder = await crashed.start(runnable({"m.yaml": FINALLY_SLOT}),
                                 backend=FakeBackend({"work": held(asyncio.Event()), "m.finally": "ok"}))
    await until(lambda: activity_rows(harness.store, holder).get("s1", {}).get("status") == "started",
                what="the holder works with its slot")
    await crashed.shutdown()
    other = slots(harness.manager(), hold=0)  # the hold ran out: another run takes the slot and keeps it
    gate = asyncio.Event()
    taker = await other.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, taker).get("s1", {}).get("status") == "started",
                timeout=3, what="the other run takes the slot")

    back = slots(harness.manager(), hold=0)
    backend = FakeBackend({"work": "done", "m.finally": "ok"})
    await back.resume(holder, backend=backend, cancel=True)  # a terminate of the interrupted holder
    row = await asyncio.wait_for(settle(back, holder), 5)  # not after the taker: it still holds the slot

    assert row["status"] == "cancelled" and backend.count("m.finally") == 1, (row["status"], backend.count("m.finally"))
    gate.set()
    assert (await settle(other, taker))["status"] == "succeeded"


REACQUIRED = """\
stategraph: 1
id: m
context: {n: 0, slots: []}
initial: take
states:
  take:
    do: {acquire: p}
    transitions: [{target: work, effect: "ctx.slots = ctx.slots + [out]"}]
  work:
    do: {agent: w, task: go}
    transitions:
      - {target: take, guard: ctx.n < 2, effect: ctx.n = ctx.n + 1}
      - {target: done}
  done: {type: final, output: "{{ ctx.slots }}"}
"""


async def test_feat2_a_frame_that_reaches_its_acquire_again_gets_the_slot_it_holds(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    blocker = await manager.start(runnable({"m.yaml": RELEASED}), backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, blocker).get("s2", {}).get("status") == "started",
                what="a run that took slot 0 and gave it back")
    row = await asyncio.wait_for(settle(manager, await manager.start(
        runnable({"m.yaml": REACQUIRED}), backend=FakeBackend({"work": "ok"}))), 5)  # no wait on itself

    assert row["status"] == "succeeded" and row["output"] == [0, 0, 0], row["error"] or row["output"]
    assert harness.store.semaphore_holders("p") == []
    gate.set()
    await settle(manager, blocker)


async def test_feat2_a_mock_only_run_takes_no_real_slot(harness):
    manager = slots(harness.manager())
    gate = asyncio.Event()
    holder = await manager.start(runnable({"m.yaml": SLOT}), backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, holder).get("s1", {}).get("status") == "started",
                what="a production run holds the only slot")
    before = harness.store.semaphore_holders("p")

    unmocked = await settle(manager, await manager.start(runnable({"m.yaml": SLOT}), mocks={"work": "x"},
                                                          mock_only=True))
    mocked = await asyncio.wait_for(settle(manager, await manager.start(
        runnable({"m.yaml": SLOT}), mocks={"take": 3, "work": "x"}, mock_only=True)), 5)

    assert unmocked["status"] == "failed" and unmocked["error"]["type"] == "unmocked", unmocked["error"]
    assert mocked["status"] == "succeeded" and mocked["output"] == 3, mocked["error"]
    assert harness.store.semaphore_holders("p") == before
    gate.set()
    await settle(manager, holder)


async def test_feat2_a_reload_names_this_configs_semaphore_problem_once(servers, tmp_path):
    server = servers(semaphores={"p": "four"})
    assert "semaphores must map" in (server.semaphore_problem or "")
    server.reload_config(tool_config(tmp_path, semaphores={"p": "four"}))
    server.reload_config(tool_config(tmp_path, semaphores={"p": "four"}))
    assert not [p for p in server.schedule_problems if "semaphores" in p], server.schedule_problems
    assert "semaphores must map" in (server.semaphore_problem or "")

    server.reload_config(tool_config(tmp_path, semaphores={"p": 4}))  # fixed: the problem is gone
    assert server.semaphore_problem is None and server.run_manager.semaphores == {"p": 4}
