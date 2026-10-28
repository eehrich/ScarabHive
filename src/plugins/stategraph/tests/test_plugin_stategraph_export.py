"""A run exported from one runs.db and imported into another forks there: the export keeps the row and journal as
stored, an import refuses what it cannot keep, and a fork may be given its id and the points that hold it."""
from __future__ import annotations

import re

import pytest

from plugins.stategraph.tests.stategraph_testkit import Harness, activity_rows, settle

MACHINE = """
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: x, task: one}
    transitions: [{target: b}]
  b:
    do: {agent: x, task: two}
    transitions: [{target: c}]
  c:
    do: {agent: x, task: three}
    transitions: [{target: done}]
  done: {type: final}
"""
MOCKS = {"a": "1", "b": "2", "c": "3"}


@pytest.fixture
async def source(tmp_path):
    harness = Harness(tmp_path / "source")
    row = await harness.run({"m.yaml": MACHINE}, mock_only=True, mocks=MOCKS)
    assert row["status"] == "succeeded", row  # fixture
    yield harness, row["id"]
    await harness.close()


def entered(journal, state):
    return sorted({int(m.group(1)) for r in journal if r["kind"] == "trace" and r["status"] == "enter"
                   and r["state"] == state for m in [re.match(r"s(\d+):enter:", r["key"])] if m})


async def test_an_imported_run_forks_as_the_id_given_and_holds_at_its_breakpoint(source, tmp_path):
    harness, run_id = source
    exported = harness.store.export_run(run_id)
    here = Harness(tmp_path / "here")
    try:
        here.store.import_run(exported, user_id="eval", owner=None, lease_until=None)
        again = here.store.export_run(run_id)
        assert again["journal"] == exported["journal"]
        assert (again["run"]["user_id"], again["run"]["owner"]) == ("eval", None)
        assert {k: v for k, v in again["run"].items() if k not in ("user_id", "owner", "lease_until")} == {
            k: v for k, v in exported["run"].items() if k not in ("user_id", "owner", "lease_until")}

        manager = here.manager()
        at_b = entered(exported["journal"], "b")[0]
        fork_id = await manager.fork(run_id, at_step=at_b, fork_id="ev-1-t_sg1", breakpoints=[{"state": "c"}])
        assert fork_id == "ev-1-t_sg1"
        await manager.wait(fork_id, timeout=5)
        assert manager.live[fork_id].ctx.status == "paused", "the breakpoint at c did not hold the fork"
        done = {key for key, row in activity_rows(here.store, fork_id).items() if row["status"] == "done"}
        assert f"s{at_b}" in done, done  # b ran in the fork
        assert f"s{entered(exported['journal'], 'c')[0]}" not in done, done
        manager.control(fork_id, "terminate")
        assert (await settle(manager, fork_id))["status"] == "cancelled"
    finally:
        await here.close()


async def test_an_import_refuses_a_taken_id_and_columns_it_does_not_know(source, tmp_path):
    harness, run_id = source
    exported = harness.store.export_run(run_id)
    here = Harness(tmp_path / "here")
    try:
        newer = {**exported, "run": {**exported["run"], "from_a_newer_stategraph": 1}}
        with pytest.raises(ValueError, match="from_a_newer_stategraph"):
            here.store.import_run(newer)
        assert here.store.get_run(run_id) is None, "a refused import left a row"
        here.store.import_run(exported)
        with pytest.raises(ValueError, match="here already"):
            here.store.import_run(exported)
        assert harness.store.export_run("no-such-run") is None
    finally:
        await here.close()
