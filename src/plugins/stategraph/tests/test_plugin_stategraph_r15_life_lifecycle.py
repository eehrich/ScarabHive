"""Review 2026-10-15, cluster lifecycle: what happens to a run around its execution -- its start, resume, fork,
sweep, close, the runs it starts, retention -- through the real StateGraphServer and RunStore under tmp_path.

Each test names the review item it holds. Mutation checks are listed in the report of the round that wrote them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time

import pytest

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.engine.runner import host_identity
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.service import ServiceError, owner_gone
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, settle, tool_config, until, utc_at

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

HOLD = """\
stategraph: 1
id: hold
python: hold.py
params: {topic: {type: string, default: none}}
context: {x: 0, seen: null}
initial: work
states:
  work:
    do: {call: work, args: {x: "{{ ctx.x }}"}}
    transitions: [{target: next, effect: ctx.seen = out}]
  next:
    do: {call: quick, args: {}}
    transitions: [{target: done}]
  done: {type: final, output: {x: "{{ ctx.x }}", seen: "{{ ctx.seen }}"}}
"""

HOLD_PY = """\
import asyncio
import os


async def work(x):
    while not os.path.exists(os.environ["SG_R15_GATE"]):
        await asyncio.sleep(0.01)
    return x


def quick():
    return 2
"""

STEPS = """\
stategraph: 1
id: steps
python: steps.py
initial: a
states:
  a:
    do: {call: one, args: {}}
    transitions: [{target: b}]
  b:
    do: {call: one, args: {}}
    transitions: [{target: c}]
  c:
    do: {call: block, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

STEPS_PY = """\
import asyncio
import os


def one():
    return 1


async def block():
    while not os.path.exists(os.environ["SG_R15_GATE"]):
        await asyncio.sleep(0.01)
    return 3
"""

FAILS = """\
stategraph: 1
id: fails
python: fails.py
initial: a
states:
  a:
    do: {call: boom, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

FAILS_PY = """\
def boom(sg):
    if sg.live.get("resumable"):
        raise sg.Error("call_failed", "budget used up", resumable=True)
    raise RuntimeError("broken for good")
"""


def make_server(tmp_path, **config) -> StateGraphServer:
    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path, **config))
    machines = tmp_path / "machines"
    for name, text in (("hold.yaml", HOLD), ("hold.py", HOLD_PY), ("steps.yaml", STEPS), ("steps.py", STEPS_PY),
                       ("fails.yaml", FAILS), ("fails.py", FAILS_PY)):
        if not (machines / name).exists():
            (machines / name).write_text(text, encoding="utf-8")
    return server


@pytest.fixture(autouse=True)
def gate(tmp_path, monkeypatch):
    """The file whose existence lets the held activities (work, block) end."""
    path = tmp_path / "gate"
    monkeypatch.setenv("SG_R15_GATE", str(path))
    return path


@pytest.fixture
async def servers(tmp_path):
    """Two processes on one runs.db: the first one to crash, the second one to go on."""
    made: list[StateGraphServer] = []

    def make(**config):
        made.append(make_server(tmp_path, **config))
        return made[-1]

    yield make
    for server in made:
        await server.stop_plugin()


async def _interrupted(server, machine: str, *, wait_for: str) -> str:
    """Start ``machine`` in ``server``, stop that process while ``wait_for``'s activity runs: an interrupted run."""
    run_id = (await server.service.start_run(machine, mock_only=False))["run_id"]
    await until(lambda: any(r["key"] == wait_for and r["status"] == "started"
                            for r in server.run_store.rows(run_id, kinds=("activity",))), what=f"{wait_for} running")
    await server.run_manager.shutdown()
    assert server.run_store.get_run(run_id)["status"] == "interrupted", "fixture: the stop interrupts the run"
    return run_id


def _open(*_: object) -> None:
    """Let the held activities end."""
    open(os.environ["SG_R15_GATE"], "w").close()


def _lifecycle(server, run_id) -> list[dict]:
    return [r for r in server.run_store.rows(run_id) if r["kind"] == "lifecycle"]


# ------------------------------------------------------------------ RES-3: a replay keeps the recorded times

async def test_res3_a_resume_keeps_the_time_of_every_row_it_replays(servers):
    first, second = servers(), servers()
    run_id = await _interrupted(first, "steps", wait_for="s2")
    # what the replay serves: finished activities and the trace -- not s2, in flight, nor the request count
    before = {(r["kind"], r["key"]): r["ts"] for r in first.run_store.rows(run_id)
              if (r["kind"] == "trace" and r["key"] != "request_seq") or r["status"] in ("done", "error")}
    await asyncio.sleep(0.05)  # a rewritten row would carry a later time

    await second.service.control_run(run_id, "resume")
    _open(second, run_id)
    assert (await settle(second.run_manager, run_id))["status"] == "succeeded"
    after = {(r["kind"], r["key"]): r["ts"] for r in second.run_store.rows(run_id)}

    replayed = {key for key in before if key[0] == "trace"}
    assert replayed, "fixture: the first process journaled trace rows"
    changed = sorted(key for key in before if after.get(key) != before[key])
    assert changed == [], f"rows the replay wrote again got a new time: {changed}"


# ------------------------------------------------------------------ RES-8: a gone process of this host

def test_res8_a_gone_process_of_this_host_counts_as_gone_and_another_hosts_never():
    host = host_identity()  # the hostname, and on Linux its pid namespace (SVC-5)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)
        renewed_now = utc_at(60)  # a lease renewed just now: a process started before that may hold it
        renewed_before = utc_at(-30)  # renewed 90 s ago: a process started since cannot be its owner (pid reused)
        assert owner_gone(f"{host}:{dead.pid}:abc", renewed_now)
        assert not owner_gone(f"{host}:{alive.pid}:abc", renewed_now)
        assert owner_gone(f"{host}:{alive.pid}:abc", renewed_before)
        assert not owner_gone(f"{host}:{os.getpid()}:abc", renewed_before)
        assert not owner_gone(f"elsewhere-{host}:{dead.pid}:abc", renewed_now)
    finally:
        alive.kill()
        alive.wait()


async def test_res8_a_run_of_a_gone_process_is_swept_and_resumed_at_once(servers):
    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    # as a SIGKILL leaves it: running, its lease live for another minute, its owner a process that is gone
    first.run_store.update_run(run_id, status="running", owner=f"{host_identity()}:{dead.pid}:x",
                               lease_until=utc_at(60))

    _open(second, run_id)
    await second.service.control_run(run_id, "resume")
    row = await settle(second.run_manager, run_id)

    assert row["status"] == "succeeded", row
    kinds = [(r["status"], (r["data"] or {}).get("why")) for r in _lifecycle(second, run_id)]
    assert ("interrupted", f"its process is gone ({host_identity()}:{dead.pid}:x)") in kinds, kinds


# ------------------------------------------------------------------ DBG-8: who started, resumed, forked

async def test_dbg8_start_resume_and_fork_leave_a_lifecycle_row_and_a_log_line(servers, caplog):
    caplog.set_level(logging.INFO, logger="plugins.stategraph.service")
    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    _open(second, run_id)
    await second.service.control_run(run_id, "resume", user_id=None)
    await settle(second.run_manager, run_id)
    fork = await second.service.control_run(run_id, "fork", at_step=0)
    await settle(second.run_manager, fork["run_id"])

    rows = [(r["status"], (r["data"] or {}).get("why")) for r in _lifecycle(second, run_id)]
    assert rows[0] == ("started", "start_run") and ("resumed", "control_run") in rows, rows
    resumed = next(r for r in _lifecycle(second, run_id) if r["status"] == "resumed")
    assert resumed["data"]["process"] == second.run_manager.owner and resumed["data"]["was"] == "interrupted"
    assert [r["status"] for r in _lifecycle(second, fork["run_id"])][:1] == ["forked"]
    logged = [r.getMessage() for r in caplog.records]
    assert any(f"run {run_id} of hold resumed" in m for m in logged), logged
    assert any(f"run {run_id} of hold started" in m for m in logged), logged


# ------------------------------------------------------------------ RES-6: a fork whose making broke off

async def test_res6_a_half_made_fork_is_ended_not_resumed(servers):
    server = servers()
    source = (await server.service.start_run("steps"))["run_id"]
    _open(server, source)
    await settle(server.run_manager, source)
    # the fork's row (and its forked row) went in, the copy of the source's journal broke off
    server.run_store.create_run("halffork", "steps", server.run_store.get_run(source)["definition"], params={},
                                mocks={}, parent_run=source, fork_step=2, owner="gone:1:x", lease_until=utc_at(-1),
                                status="interrupted")

    with pytest.raises(ServiceError) as refused:
        await server.service.control_run("halffork", "resume")

    assert refused.value.status == 409 and "making broke off" in refused.value.message
    assert "halffork" not in server.run_manager.live
    assert server.run_store.get_run("halffork")["status"] == "cancelled"


# ------------------------------------------------------------------ API-10 / FEAT-8: failed runs

async def test_api10_a_resume_of_a_run_that_failed_for_good_is_refused(servers):
    server = servers()
    run_id = (await server.service.start_run("fails"))["run_id"]
    row = await settle(server.run_manager, run_id)
    assert row["status"] == "failed" and not row["error"].get("resumable"), "fixture: failed for good"

    with pytest.raises(ServiceError) as refused:
        await server.service.control_run(run_id, "resume")

    assert refused.value.status == 409 and "fork it" in refused.value.message, refused.value.message
    assert run_id not in server.run_manager.live


async def test_feat8_a_terminate_closes_a_run_that_failed_resumably(servers):
    from plugins.stategraph.server import _summary

    server = servers()
    run_id = (await server.service.start_run("fails", live={"resumable": True}))["run_id"]
    row = await settle(server.run_manager, run_id)
    assert row["status"] == "failed" and row["error"]["resumable"], "fixture: a resumable failure"
    assert "resume" in _summary(row)["next"], "fixture: it offers a resume"

    answer = await server.service.control_run(run_id, "terminate", user_id="ops_ada")

    assert answer["status"] == "cancelled", answer
    assert answer["error"]["closed"]["type"] == "call_failed" and "ops_ada" in answer["error"]["message"]
    assert "next" not in _summary(answer), _summary(answer)
    with pytest.raises(ServiceError):
        await server.service.control_run(run_id, "resume")
    assert [r["status"] for r in _lifecycle(server, run_id)][-1] == "closed"


# ------------------------------------------------------------------ FEAT-9: set before a resume

async def test_feat9_ctx_and_params_of_an_interrupted_run_are_set_before_its_resume(servers):
    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")

    assert await second.service.control_run(run_id, "set", path="ctx.x", expr="41", user_id="ops_ada") == {"value": 41}
    # its recorded s0 ran with the old params: the replay keeps them, the new value holds once it goes live
    assert await second.service.control_run(run_id, "set", path="params.topic", expr='"news"') == {"value": "news"}
    _open(second, run_id)
    await second.service.control_run(run_id, "resume")
    row = await settle(second.run_manager, run_id)

    # the activity in flight runs again with the inputs it had (anything else would diverge); the value is there
    # for its transition and every step after it
    assert row["status"] == "succeeded" and row["output"] == {"x": 41, "seen": 0}, row["error"] or row["output"]
    sets = [r["data"] for r in _lifecycle(second, run_id) if r["status"] == "set"]
    assert [(s["path"], s["value"]) for s in sets] == [("ctx.x", 41), ("params.topic", "news")], sets


async def test_lc_r1_params_of_an_interrupted_run_with_no_recorded_step_are_set(servers):
    first, second = servers(), servers()
    source = await _interrupted(first, "hold", wait_for="s0")
    # a run that stopped before its first step: nothing a resume replays read its params
    second.run_store.create_run("fresh", "hold", second.run_store.get_run(source)["definition"], params={}, mocks={},
                                owner="gone:1:x", lease_until=utc_at(-1), status="interrupted")

    assert await second.service.control_run("fresh", "set", path="params.topic", expr='"news"', user_id="ops_ada") == {"value": "news"}
    _open(second, "fresh")
    await second.service.control_run("fresh", "resume")
    row = await settle(second.run_manager, "fresh")

    assert row["status"] == "succeeded" and row["params"] == {"topic": "news"}, row["error"] or row["params"]
    sets = [r["data"] for r in _lifecycle(second, "fresh") if r["status"] == "set"]
    assert [(s["path"], s["value"], s["who"]) for s in sets] == [("params.topic", "news", "ops_ada")]


async def test_feat9_set_of_a_stopped_run_takes_values_not_expressions(servers):
    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    with pytest.raises(ServiceError) as refused:
        await second.service.control_run(run_id, "set", path="ctx.x", expr="ctx.x + 1")
    assert refused.value.status == 422 and "value" in refused.value.message
    with pytest.raises(ServiceError) as unknown:
        await second.service.control_run(run_id, "set", path="params.nope", expr="1")
    assert unknown.value.status == 422 and "nope" in unknown.value.message


# ------------------------------------------------------------------ FEAT-1: runs started from a call activity

PARENT = """\
stategraph: 1
id: parent
python: parent.py
initial: run
states:
  run:
    do: {call: run, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

PARENT_PY = """\
import asyncio


async def run(sg):
    started = await sg.live["service"].start_run("hold")
    await asyncio.sleep(3600)
"""


async def test_feat1_a_run_started_from_a_call_activity_names_its_parent_and_ends_with_its_terminate(servers, tmp_path):
    server = servers()
    (tmp_path / "machines" / "parent.yaml").write_text(PARENT, encoding="utf-8")
    (tmp_path / "machines" / "parent.py").write_text(PARENT_PY, encoding="utf-8")
    parent = (await server.service.start_run("parent", live={"service": server.service}))["run_id"]
    await until(lambda: server.run_store.children(parent), what="the child started")
    [child] = server.run_store.children(parent)

    seen = server.service.get_run(parent)
    assert [c["id"] for c in seen["children"]] == [child["id"]]
    assert server.service.get_run(child["id"])["started_by"]["run_id"] == parent
    assert child["caller_request"].startswith(f"{parent}_"), child

    await server.service.control_run(parent, "terminate")
    await until(lambda: server.run_store.get_run(child["id"])["status"] == "cancelled", what="the child ended")


def test_feat1_a_parents_spend_holds_its_childrens_under_the_calling_state(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    store.create_run("p", "parent", {}, owner="o")
    store.create_run("c", "child", {}, owner="o")
    store.update_run("c", caller_run="p", caller_request="p_001")
    store.record("p", "activity", "s0", state="run", status="done",
                 data={"state": "run", "meta": {"request_ids": ["p_001"]}})
    store.record("p", "activity", "s1", state="other", status="done",
                 data={"state": "other", "meta": {"cost": 0.5, "tokens": {"prompt": 1, "completion": 1}}})
    store.record("c", "activity", "s0", state="w", status="done",
                 data={"state": "w", "meta": {"cost": 2.0, "tokens": {"prompt": 10, "completion": 5}}})

    parent, child, plain = store.spend("p", children=True), store.spend("c"), store.spend("p")
    store.close()

    assert plain["cost"] == 0.5, "a plain spend is the run's own: a sum over parent and children counts each once"

    assert child["cost"] == 2.0, "the child keeps its own count"
    assert parent["cost"] == 2.5 and parent["tokens"]["prompt"] == 11, parent
    assert {s["path"]: s["cost"] for s in parent["by_state"]} == {"other": 0.5, "run": 2.0}


def test_lc_r2_get_run_answers_the_spend_with_the_childrens(tmp_path):
    server = make_server(tmp_path)
    store = server.run_store
    store.create_run("p", "parent", {}, owner="o")
    store.create_run("c", "child", {}, owner="o")
    store.update_run("c", caller_run="p", caller_request="p_001")
    store.record("p", "activity", "s0", status="done", data={"meta": {"cost": 0.5, "request_ids": ["p_001"]}})
    store.record("c", "activity", "s0", status="done", data={"meta": {"cost": 2.0}})
    try:
        seen = server.service.get_run("p")["spend"]
    finally:
        store.close()

    assert seen["cost"] == 2.5, seen


# ------------------------------------------------------------------ FEAT-11: retention

def test_feat11_retention_drops_the_journal_of_old_ended_runs_only(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    for run_id, status, error in (("old", "succeeded", None), ("paused", "failed", {"type": "x", "resumable": True}),
                                  ("stopped", "interrupted", None), ("new", "succeeded", None)):
        store.create_run(run_id, "m", {}, owner="o", status=status, run_key=f"k-{run_id}")
        store.update_run(run_id, error=error, finished_at=utc_at(-40 * 86400 if run_id != "new" else -60))
        store.record(run_id, "activity", "s0", status="done",
                     data={"meta": {"cost": 1.0, "tokens": {"prompt": 3, "completion": 4}}})
        store.lifecycle(run_id, "started", {"who": "x"})

    compacted = store.compact_ended(before=utc_at(-30 * 86400))

    assert compacted == ["old"], compacted
    assert [r["kind"] for r in store.rows("old")] == ["lifecycle", "lifecycle"]
    assert store.get_run("old")["run_key"] == "k-old" and store.latest_by_key("k-old")["status"] == "succeeded"
    assert store.spend("old")["cost"] == 1.0, "what it spent is kept"
    for kept in ("paused", "stopped", "new"):
        assert [r["kind"] for r in store.rows(kept)] == ["activity", "lifecycle"], kept
    assert store.compact_ended(before=utc_at(-30 * 86400)) == [], "a compacted run is not compacted again"
    store.close()


async def test_feat11_a_compacted_run_is_not_forked_and_retention_is_off_by_default(servers):
    server = servers()
    assert server.retention_days is None and await server.compact() == []
    run_id = (await server.service.start_run("steps"))["run_id"]
    _open(server, run_id)
    await settle(server.run_manager, run_id)
    assert server.run_store.compact_ended(before=utc_at(60)) == [run_id]
    with pytest.raises(ServiceError) as refused:
        await server.service.control_run(run_id, "fork")
    assert refused.value.status == 422 and "compacted" in refused.value.message
    # an internal failure stays resumable (API-10) -- but with its journal gone every step would run live again
    server.run_store.update_run(run_id, status="failed", error={"type": "internal", "message": "x"})
    with pytest.raises(ServiceError) as not_resumed:
        await server.service.control_run(run_id, "resume")
    assert not_resumed.value.status == 409 and "compacted" in not_resumed.value.message, not_resumed.value.message
    assert run_id not in server.run_manager.live


def test_lc_r5_a_run_compacted_since_the_selection_is_not_compacted_again(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store, other = RunStore(tmp_path / "runs.db"), RunStore(tmp_path / "runs.db")
    store.create_run("old", "m", {}, owner="o", status="succeeded")
    store.update_run("old", finished_at=utc_at(-40 * 86400))
    store.record("old", "activity", "s0", status="done", data={"meta": {"cost": 1.0}})
    selected = store._compactable

    def raced(before, limit):  # another process compacts between this one's selection and its transaction
        ids = selected(before, limit)
        other.compact_ended(before=before)
        return ids

    store._compactable = raced
    try:
        assert store.compact_ended(before=utc_at(-30 * 86400)) == []
        compacted = [r for r in store.rows("old") if r["status"] == "compacted"]
    finally:
        store.close()
        other.close()

    assert len(compacted) == 1 and compacted[0]["data"]["spend"]["cost"] == 1.0, compacted


def test_retention_leaves_a_failed_run_whose_lease_holds(tmp_path):
    """A transient failure's resume takes the lease, then rewinds: retention must not drop the journal under it."""
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_run("held", "m", {}, owner="o", status="failed", lease_until=utc_at(60))
        store.update_run("held", finished_at=utc_at(-40 * 86400), error={"type": "agent_failed"})
        store.record("held", "activity", "s0", status="error", data={})
        while_held = store.compact_ended(before=utc_at(-30 * 86400))
        store.update_run("held", lease_until=utc_at(-1))
        after = store.compact_ended(before=utc_at(-30 * 86400))
    finally:
        store.close()

    assert (while_held, after) == ([], ["held"])


def test_a_key_counts_its_runs_and_their_resumes_after_a_failure_also_once_compacted(tmp_path):
    """A schedule's slot tries at most ATTEMPTS times; a transient failure resumes the same run, so the resumes of a
    failed run count -- not one after an interrupt -- and retention, which drops the journal, keeps the count."""
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_run("r1", "m", {}, owner="o", status="failed", run_key="slot")
        store.update_run("r1", finished_at=utc_at(-40 * 86400), error={"type": "agent_failed"})
        store.record("r1", "activity", "s0", status="error", data={})
        for was in ("failed", "interrupted", "failed"):
            store.lifecycle("r1", "resumed", {"was": was})
        counted = store.count_by_key("slot")
        store.compact_ended(before=utc_at(-30 * 86400))
        compacted = store.compacted("r1")
        after = store.count_by_key("slot")
    finally:
        store.close()

    assert (counted, compacted, after) == (3, True, 3)


# ------------------------------------------------------------------ PROD-5: run lists read no blobs

def test_prod5_the_run_lists_read_a_covering_index(tmp_path):
    from plugins.stategraph.engine.journal import _LISTED, RunStore

    store = RunStore(tmp_path / "runs.db")
    store.create_run("r", "m", {"files": {"m.yaml": "x" * 10000}}, owner="o")
    db = store._db()
    plans = {}
    for name, where in (("all", ""), ("machine", " WHERE machine_id = 'm'")):
        plan = db.execute(f"EXPLAIN QUERY PLAN SELECT {_LISTED} FROM runs{where} ORDER BY created_at DESC, id DESC"
                          " LIMIT 50").fetchall()
        plans[name] = " ".join(str(row[-1]) for row in plan)
    listed = store.list_runs()
    store.close()

    assert all("COVERING INDEX" in plan and "TEMP B-TREE" not in plan for plan in plans.values()), plans
    assert listed[0]["id"] == "r" and "caller_run" in listed[0]


# ------------------------------------------------------------------ PROD-6: activities that ended without outcome

def test_prod6_started_rows_of_an_ended_run_are_marked_ended_without_outcome(tmp_path):
    server = make_server(tmp_path)
    store = server.run_store
    store.create_run("r", "m", {}, owner="o", status="running")
    store.record("r", "activity", "s0", status="done", data={})
    store.record("r", "activity", "s1", status="started", data={})
    store.record("r", "activity", "s1/b.x", status="started", data={})
    store.record("r", "activity", "s0/b.y", status="started", data={})  # below one with an outcome
    try:
        running = {r["key"]: r.get("ended_without_outcome") for r in server.service.journal("r")}
        store.update_run("r", status="cancelled")
        ended = {r["key"]: r.get("ended_without_outcome") for r in server.service.get_run("r")["journal"]}
    finally:
        store.close()

    assert running == {"s0": None, "s1": None, "s1/b.x": None, "s0/b.y": True}, running
    assert ended == {"s0": None, "s1": True, "s1/b.x": True, "s0/b.y": True}, ended


# ------------------------------------------------------------------ MOD-11: a file that is not UTF-8

async def test_mod11_a_machine_file_that_is_not_utf8_is_a_422_that_says_so(servers, tmp_path):
    server = servers()
    path = tmp_path / "machines" / "latin.yaml"
    path.write_bytes(HOLD.replace("id: hold", "id: latin").replace("python: hold.py\n", "").encode("utf-8")
                     + "# Grüße\n".encode("cp1252"))

    for call in (lambda: server.service.edit_machine("latin", {"op": "rename_state", "old": "next", "new": "n2"},
                                                     "x"),
                 lambda: server.service.delete_machine("latin", "x")):
        with pytest.raises(ServiceError) as refused:
            result = call()
            if asyncio.iscoroutine(result):
                await result
        assert refused.value.status == 422 and "not UTF-8 (byte 0xfc" in refused.value.message, refused.value.message


# ------------------------------------------------------------------ MOD-2: rename_state and the companion's strings

async def test_mod2_a_rename_names_the_companion_lines_that_still_read_the_old_name(servers, tmp_path):
    server = servers()
    (tmp_path / "machines" / "visits.yaml").write_text(HOLD.replace("id: hold", "id: visits").replace(
        "hold.py", "visits.py"), encoding="utf-8")
    (tmp_path / "machines" / "visits.py").write_text(
        HOLD_PY + '\n\ndef again(run):\n    return run.visits.get("next", 0) < 2\n', encoding="utf-8")
    machine = server.service.get_machine("visits")

    answer = await server.service.edit_machine("visits", {"op": "rename_state", "old": "next", "new": "later"},
                                               machine["versions"]["visits.yaml"])
    quiet = await server.service.edit_machine("visits", {"op": "rename_state", "old": "work", "new": "busy"},
                                              answer["versions"]["visits.yaml"])

    line = (tmp_path / "machines" / "visits.py").read_text(encoding="utf-8").splitlines().index(
        '    return run.visits.get("next", 0) < 2') + 1
    assert answer["warnings"] == [f"visits.py still names 'next' at line {line}: code that reads run.visits or "
                                  "run.state by that name needs the new one"], answer.get("warnings")
    assert "warnings" not in quiet


# ------------------------------------------------------------------ FEAT-3 / RES-2: resumed by the scheduler

async def test_feat3_the_scheduler_resumes_an_opted_in_machines_runs_and_an_earlier_slots(servers, monkeypatch):
    from plugins.stategraph.schedules import RESUME_PAUSE, Scheduler

    monkeypatch.setenv("STATEGRAPH_DRIVER", "1")
    first = servers()
    opted = await _interrupted(first, "hold", wait_for="s0")
    first2 = servers()
    manual = await _interrupted(first2, "steps", wait_for="s2")
    first3 = servers()
    with_live = (await first3.service.start_run("hold", live={"thing": object()}))["run_id"]
    await until(lambda: first3.run_store.has_row(with_live, "activity", "s0"), what="the live run started")
    await first3.run_manager.shutdown()
    slot = servers(schedules=[{"name": "hourly", "machine": "steps", "every": "1h"}])
    old_slot = (await slot.service.start_run("steps", run_key="schedule:hourly:5"))["run_id"]
    await until(lambda: slot.run_store.has_row(old_slot, "activity", "s2"), what="the old slot ran")
    await slot.run_manager.shutdown()

    second = servers(auto_resume=["hold"], schedules=[{"name": "hourly", "machine": "steps", "every": "1h"}])
    _open(second, opted)
    scheduler = Scheduler(second, second.schedules)
    now = time.time() + 1
    resumed = await scheduler.resume_interrupted(now)

    assert sorted(resumed) == sorted([opted, old_slot]), (resumed, opted, old_slot)
    assert second.run_store.get_run(manual)["status"] == "interrupted", "a machine not opted in is left alone"
    assert second.run_store.get_run(with_live)["status"] == "interrupted", "a run with live objects is its caller's"
    assert (await settle(second.run_manager, opted))["status"] == "succeeded"
    why = [(r["data"] or {}).get("why") for r in _lifecycle(second, opted) if r["status"] == "resumed"]
    assert why == ["auto_resume"], why

    # a resume that fails is tried again after a pause, not every tick
    second.auto_resume.add("steps")
    calls = []

    async def refuse(run_id, **_):
        calls.append(run_id)
        raise ServiceError(409, "held elsewhere")

    second.service._resume = refuse
    await scheduler.resume_interrupted(now + 1)
    await scheduler.resume_interrupted(now + 2)
    await scheduler.resume_interrupted(now + RESUME_PAUSE + 2)
    assert calls == [manual, manual], calls


async def test_res2_an_earlier_slots_run_whose_machine_is_gone_is_ended(servers, tmp_path, monkeypatch):
    from plugins.stategraph.schedules import Scheduler

    monkeypatch.setenv("STATEGRAPH_DRIVER", "1")
    first = servers()
    old_slot = await _interrupted(first, "steps", wait_for="s2")
    first.run_store.update_run(old_slot, run_key="schedule:hourly:5")
    second = servers(schedules=[{"name": "hourly", "machine": "steps", "every": "1h"}])
    (tmp_path / "machines" / "steps.yaml").unlink()

    assert await Scheduler(second, second.schedules).resume_interrupted(time.time()) == []
    row = second.run_store.get_run(old_slot)
    assert row["status"] == "cancelled" and "gone" in row["error"]["message"], row


async def test_lc_r6_an_opted_in_run_behind_a_full_page_of_others_is_resumed(servers, monkeypatch):
    from plugins.stategraph import schedules
    from plugins.stategraph.schedules import Scheduler

    monkeypatch.setenv("STATEGRAPH_DRIVER", "1")
    first = servers()
    opted = await _interrupted(first, "hold", wait_for="s0")
    first2 = servers()
    await _interrupted(first2, "steps", wait_for="s2")  # newer, of a machine not opted in
    second = servers(auto_resume=["hold"])
    _open(second, opted)
    monkeypatch.setattr(schedules, "RESUME_BATCH", 1)  # the newer run fills the first page

    assert await Scheduler(second, second.schedules).resume_interrupted(time.time()) == [opted]
    assert (await settle(second.run_manager, opted))["status"] == "succeeded"


async def test_feat3_nothing_resumes_by_itself_by_default(servers, monkeypatch):
    from plugins.stategraph.schedules import Scheduler

    monkeypatch.setenv("STATEGRAPH_DRIVER", "1")
    first = servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    second = servers()
    assert await Scheduler(second, second.schedules).resume_interrupted(time.time()) == []
    assert second.run_store.get_run(run_id)["status"] == "interrupted"


# ------------------------------------------------------------------ FEAT-4: the code changed since the snapshot

async def test_feat4_a_resume_says_the_companion_module_changed(servers, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="plugins.stategraph.service")
    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    (tmp_path / "machines" / "hold.py").write_text(HOLD_PY + "\n# changed\n", encoding="utf-8")

    _open(second, run_id)
    await second.service.control_run(run_id, "resume")
    row = await settle(second.run_manager, run_id)

    assert row["status"] == "succeeded", "a warning only: the run goes on as before"
    [changed] = [r["data"] for r in _lifecycle(second, run_id) if r["status"] == "code_changed"]
    assert "companion module hold.py differs from the run's snapshot" in changed["why"], changed
    assert any("code_changed" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
