"""Final cross-check: an internal failure is resumable everywhere a reader asks (F1), a slot a replayed acquire
claims after its run went live is checked like went_live's (F2), and the scheduler's opted set stays bounded (F3)."""

from __future__ import annotations

import asyncio
import time

import pytest

from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, held, runnable,
                                                         settle, until)
from plugins.stategraph.tests.test_plugin_stategraph_js import JSC, NODE, prepare_panel, run_case
from plugins.stategraph.tests.test_plugin_stategraph_r15_life_lifecycle import (  # noqa: F401 -- fixtures
    _interrupted, _open, gate, make_server, servers)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


# ------------------------------------------------------------------ F2
PY = """\
async def work(sg):
    if await sg.tool("budget", {}) == "full":
        raise sg.Error("call_failed", "the budget is used up", resumable=True)
    return "ok"
"""

BRANCHES = """\
stategraph: 1
id: m
python: m.py
initial: take
states:
  take:
    do:
      parallel:
        a: {call: work}
        b: {acquire: p}
    transitions: [{target: work}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""

TAKER = """\
stategraph: 1
id: t
initial: take
states:
  take:
    do: {acquire: p}
    transitions: [{target: work}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_slot_a_branch_claims_after_another_branch_went_live_waits_for_its_holder(harness):
    """Branch b took slot 0, branch a stopped the run resumably: its end gave the slot back and another run took
    it. The resume runs a again -- it goes live first -- and b replays its acquire after that: the run must wait
    for the other run to give slot 0 back, not work beside it on the same slot."""
    manager = harness.manager()
    manager.semaphores = {"p": 1}
    gate = asyncio.Event()
    run_id = await manager.start(runnable({"m.yaml": BRANCHES, "m.py": PY}),
                                 backend=FakeBackend({"take/a/budget": held(gate, "full")}))
    await until(lambda: harness.store.semaphore_holders("p"), what="branch b holds slot 0")
    gate.set()
    row = await settle(manager, run_id)
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert harness.store.semaphore_holders("p") == [], "fixture: the failed run's end gave the slot back"

    other = asyncio.Event()
    taker = await manager.start(runnable({"t.yaml": TAKER}, root="t.yaml"),
                                backend=FakeBackend({"work": held(other)}))
    await until(lambda: [h["run_id"] for h in harness.store.semaphore_holders("p")] == [taker],
                what="another run holds slot 0")

    backend = FakeBackend({"take/a/budget": "ok", "work": "done"})
    back = harness.manager()
    back.semaphores = {"p": 1}
    await back.resume(run_id, backend=backend)
    await until(lambda: backend.count("take/a/budget") == 1, what="branch a runs again")
    await asyncio.sleep(1.0)
    ran = backend.count("work")
    other.set()
    assert (await settle(manager, taker))["status"] == "succeeded"
    row = await settle(back, run_id)
    assert ran == 0, "the resumed run worked on slot 0 while another run held it"
    assert row["status"] == "succeeded" and backend.count("work") == 1, row["error"]


async def test_every_branch_waits_while_one_holds_a_late_claimed_slot_again():
    """Whichever branch's went_live takes up a late claim, the others wait at theirs until the slots are back --
    the claiming branch may be one of them. Only went_live's own fields are set: its live path reads no other."""
    from plugins.stategraph.engine.runner import RunContext

    run = object.__new__(RunContext)
    run.live, run._slots_back, run._late_claim = True, None, False
    back, reclaims = asyncio.Event(), []

    async def reclaim_slots():
        reclaims.append(1)
        await back.wait()

    run.reclaim_slots = reclaim_slots
    run.claimed(True)
    first = asyncio.create_task(run.went_live("s0/c"))
    await until(lambda: reclaims, what="the first branch holds the slots again")
    second = asyncio.create_task(run.went_live("s0/b"))
    await asyncio.sleep(0.05)
    assert not second.done(), "a branch went on while the late claim was still being held again"
    back.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert reclaims == [1], "held again once"


# ------------------------------------------------------------------ F1
def _failed_internally(server, run_id: str) -> None:
    """The run stopped as the engine's own failure leaves it (runner: an exception out of the interpreter): failed,
    error type internal, its journal as it stood -- the activity in flight stays started."""
    server.run_store.update_run(run_id, status="failed", error={"type": "internal", "message": "RuntimeError: boom"})


async def test_an_internally_failed_run_is_called_resumable_in_its_answer_and_its_hint(servers):
    from plugins.stategraph.server import NEXT_INTERNAL

    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    _failed_internally(second, run_id)

    assert second.service.get_run(run_id)["resumable"] is True
    answer = await second.get_run({"run_id": run_id})
    assert answer.get("next") == NEXT_INTERNAL, answer.get("next")

    second.run_store.update_run(run_id, error={"type": "call_failed", "message": "for good"})
    assert second.service.get_run(run_id)["resumable"] is False
    assert "next" not in await second.get_run({"run_id": run_id})


async def test_set_before_the_resume_of_an_internally_failed_run_applies_where_it_stopped(servers):
    """No rewind for an internal failure (runner.resume): the edit lands where the run stood, like an
    interrupted run's -- after the activity in flight, which runs again with the x it had."""
    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    _failed_internally(second, run_id)

    assert await second.service.control_run(run_id, "set", path="ctx.x", expr="7", user_id="ops_ada") == {"value": 7}
    [edit] = second.run_store.rows(run_id, kinds=("edit",))
    assert edit["data"]["at"] == "s0:exit", edit
    _open()
    await second.service.control_run(run_id, "resume")
    row = await settle(second.run_manager, run_id)
    assert row["status"] == "succeeded" and row["output"] == {"x": 7, "seen": 0}, row["error"] or row["output"]


# ------------------------------------------------------------------ F3
async def test_the_scheduler_forgets_a_run_that_is_no_longer_interrupted(servers, tmp_path, monkeypatch):
    from plugins.stategraph.schedules import Scheduler
    from plugins.stategraph.tests.test_plugin_stategraph_r15_life_lifecycle import HOLD

    monkeypatch.setenv("STATEGRAPH_DRIVER", "1")
    first = servers()
    (tmp_path / "machines" / "autohold.yaml").write_text(
        HOLD.replace("id: hold", "id: autohold\nresume: auto"), encoding="utf-8")
    # resume: auto, but it holds its caller's live objects: the scheduler looks at it, resumes it never
    run_id = (await first.service.start_run("autohold", live={"thing": object()}))["run_id"]
    await until(lambda: first.run_store.has_row(run_id, "activity", "s0"), what="the run started")
    await first.run_manager.shutdown()
    second = servers()
    scheduler = Scheduler(second, [])
    scheduler.paused["gone"] = (1, 0.0)  # a run whose resume failed once, ended since
    now = time.time() + 1
    assert await scheduler.resume_interrupted(now) == []
    assert set(scheduler.opted) == {run_id} and "gone" not in scheduler.paused, "fixture: looked at, once"

    second.run_store.update_run(run_id, status="cancelled")
    assert await scheduler.resume_interrupted(now + 30) == []
    assert scheduler.opted == {}, scheduler.opted


# ------------------------------------------------------------------ F1, the panel
needs_js = pytest.mark.skipif(JSC is None and NODE is None, reason="neither JavaScriptCore (jsc) nor node is installed")

XCHECK_CASES = [
    "xcheck_a_run_that_failed_internally_offers_resume_and_close",
    "xcheck_a_failure_the_server_does_not_call_resumable_offers_neither",
    "xcheck_a_machine_that_takes_a_while_says_it_is_opening",
]


@needs_js
def test_every_xcheck_panel_case_is_run(tmp_path):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    listed = next(line for line in run_case(tmp_path, "xcheck:*") if line.startswith("CASES ")).split()[1:]

    assert sorted(listed) == sorted(XCHECK_CASES)


@needs_js
@pytest.mark.parametrize("case", XCHECK_CASES)
def test_xcheck_panel_case(tmp_path, case):
    prepare_panel(tmp_path, "fake_kit_held.js", ("fake_kit_held.js", "panel_cases.js"))

    lines = run_case(tmp_path, case)

    assert f"PASS {case}" in lines and "ERRORS []" in lines, "\n".join(lines)
