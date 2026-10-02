"""A submachine's runs (RunStore.frames): each submachine frame a run starts is recorded, once also through a replay;
a machine's runs list the runs it ran in as a submachine (nested), and get_run names those frames for the panel."""
from __future__ import annotations

import pytest

from plugins.stategraph.tests.stategraph_testkit import FakeBackend, Harness, runnable, settle

FILES = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: a
states:
  a:
    do: {machine: sub}
    transitions: [{target: b}]
  b:
    do: {machine: sub}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": """\
stategraph: 1
id: sub
initial: w
states:
  w:
    do: {tool: work}
    transitions: [{target: done}]
  done: {type: final}
"""}


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def test_a_run_records_its_submachine_frames_and_the_submachine_lists_it(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(FILES), backend=FakeBackend({"a/w": 1, "b/w": 2}))
    assert (await settle(manager, run_id))["status"] == "succeeded"
    other = harness.store
    other.create_run("x1", "sub", {})  # a run of the submachine itself
    other.create_run("x2", "unrelated", {})

    frames = other.frames(run_id)

    assert [(f["machine"], f["path"]) for f in frames] == [("sub", "a"), ("sub", "b")], frames
    assert all(f["prefix"].endswith("/m/") for f in frames) and len({f["prefix"] for f in frames}) == 2
    assert {r["id"] for r in other.list_runs("sub", nested=True)} == {run_id, "x1"}
    assert [r["id"] for r in other.list_runs("sub")] == ["x1"], "without nested: its own runs only"
    assert [r["id"] for r in other.list_runs("m", nested=True)] == [run_id]
    other.add_frame(run_id, frames[0]["prefix"], "sub", "a")  # a replay starts the frame again
    assert len(other.frames(run_id)) == 2


def test_a_runs_db_from_before_the_record_gets_its_ended_frames_from_the_journal(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    store.create_run("r1", "m", {})
    for key, path in (("s1", "a"), ("s3", "c"), ("s5", "e"), ("s5/m/s1", "e/w")):  # the activities, as they started
        store.record("r1", "activity", key, state=path.split("/")[-1], status="done", data={"kind": "machine", "path": path})
    for frame, machine in (("s1/m/", "sub"), ("s3/a2/m/", "sub"),  # a retry's second attempt: its activity is s3
                           ("s5/m/s1/m/", "inner"), ("s5/m/", "sub"),  # a child ends before its parent
                           ("", "m")):  # the root: no submachine
        store.record("r1", "trace", f"{frame}end", state="done", status="end",
                     data={"reason": "finished", "frame": frame, "machine": machine})
    store.record("r1", "trace", "s1/m/s1:enter:w", state="w", status="enter", data={"frame": "s1/m/", "machine": "sub"})
    for seq, data in ((97, '[1]'), (98, '{"frame": 3, "machine": "sub"}'), (99, '{"frame": NaN')):  # rows it cannot read
        store._db().execute("INSERT INTO journal (run_id, seq, ts, kind, key, status, data)"
                            f" VALUES ('r1', {seq}, 't', 'trace', 'x{seq}/m/end', 'end', ?)", (data,))
    store._db().execute("DROP TABLE frames")  # as it was before the table
    store.close()

    reopened = RunStore(tmp_path / "runs.db")

    assert reopened.frames("r1") == [{"prefix": "s1/m/", "machine": "sub", "path": "a"},
                                     {"prefix": "s3/a2/m/", "machine": "sub", "path": "c"},
                                     {"prefix": "s5/m/", "machine": "sub", "path": "e"},
                                     {"prefix": "s5/m/s1/m/", "machine": "inner", "path": "e/w"}], "in the order they started"
    assert [r["id"] for r in reopened.list_runs("sub", nested=True)] == ["r1"]
    reopened._db().execute("DELETE FROM frames")
    reopened.close()
    again = RunStore(tmp_path / "runs.db")
    assert again.frames("r1") == [], "filled once: an open that finds the table fills nothing"
    again.close()


def test_a_journal_the_fill_cannot_read_does_not_keep_the_runs_from_opening(tmp_path, monkeypatch, caplog):
    import sqlite3

    from plugins.stategraph.engine import journal

    def broken(conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(journal, "_frames_from_journal", broken)
    store = journal.RunStore(tmp_path / "runs.db")
    store.create_run("r1", "m", {})

    assert store.get_run("r1")["machine_id"] == "m"
    assert "not filled from the journal" in caplog.text
    store.close()


async def test_get_run_names_the_frames_only_when_asked(tmp_path):
    from agent_system.config.models import AgentSystemConfig

    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    srv = StateGraphServer("stategraph", AgentSystemConfig.model_validate({}), tool_config(tmp_path))
    try:
        srv.run_store.create_run("r1", "m", {})
        srv.run_store.add_frame("r1", "s1/m/", "sub", "a")

        assert srv.service.get_run("r1", frames=True)["frames_started"] == [{"prefix": "s1/m/", "machine": "sub", "path": "a"}]
        assert "frames_started" not in srv.service.get_run("r1"), "the agents' get_run stays as it was"
        assert [r["id"] for r in srv.service.list_runs("sub", nested=True)] == ["r1"]
    finally:
        await srv.stop_plugin()
