"""The reading helpers for checks: a run's products read through the record, as the real writers left them -- the
JSON store's own files, a real stategraph run's journal, stategraph's own spend."""
from __future__ import annotations

import pytest

from agent_system.config.models import ToolServerConfig
from plugins.agent_eval import evidence
from plugins.json_store.server import JsonStoreServer
from plugins.stategraph.engine.journal import RunStore
from plugins.stategraph.tests.stategraph_testkit import Harness, runnable, settle

MACHINE = """
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: x, task: one}
    transitions: [{target: stage}]
  stage:
    initial: inner
    states:
      inner:
        do: {agent: x, task: two}
        transitions: [{target: end}]
      end: {type: final}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_store_doc_reads_what_the_json_store_wrote(tmp_path, mock_system_config):
    store = JsonStoreServer("notes_json", mock_system_config, ToolServerConfig(
        type="json_store", enabled=True, config={"storage_path": str(tmp_path / "json_store")}))
    for ns, value in (("Run-7", {"items": [1, 2]}), ("other", {"items": []})):
        result = await store.write({"_session_id": "s", "namespace": ns, "doc": "Plan", "data": value})
        assert result["status"] == "ok", result  # fixture
    await store.write({"_session_id": "s", "namespace": "Run-7", "doc": "notes", "data": "x \ud800"})
    run = {"data_dir": str(tmp_path)}
    assert evidence.store_doc(run, "Plan", namespace="Run-7", server="notes_json") == {"items": [1, 2]}
    # in one namespace only: no need to name it; a lone surrogate, as a model's JSON may carry, reads back
    assert evidence.store_doc(run, "notes", server="notes_json") == "x \ud800"
    assert evidence.store_doc(run, "absent", server="notes_json") is None
    with pytest.raises(ValueError, match="Run-7.*other"):
        evidence.store_doc(run, "Plan", server="notes_json")


async def test_entered_and_machine_run_follow_a_real_run(tmp_path):
    harness = Harness(tmp_path / "stategraph")
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": MACHINE}), mock_only=True,
                                     mocks={"a": "1", "inner": "2"})
        assert (await settle(manager, run_id))["status"] == "succeeded"  # fixture
    finally:
        await harness.close()
    run = {"data_dir": str(tmp_path), "stategraph_runs": [{"id": run_id, "caller_run": None},
                                                          {"id": "called", "caller_run": run_id}]}
    states = evidence.entered(run)
    # a composite and its first state inside enter in the same step
    assert [(s["state"], s["key"]) for s in states] == [
        ("a", "s0:enter:a"), ("stage", "s1:enter:stage"), ("inner", "s1:enter:inner"), ("end", "s2:enter:end"),
        ("done", "s3:enter:done")], states
    assert all(s["ts"] for s in states), states
    assert evidence.entered({**run, "fork": {"run_id": run_id}, "stategraph_runs": []}) == states
    two = {**run, "stategraph_runs": [{"id": run_id}, {"id": "another"}]}
    with pytest.raises(ValueError, match="2 machine runs"):
        evidence.entered(two)
    with pytest.raises(ValueError, match="no machine run nowhere"):
        evidence.entered(run, run_id="nowhere")


def test_spend_is_stategraphs_own_by_state(tmp_path):
    db = RunStore(tmp_path / "stategraph" / "runs.db")
    db.create_run("r", "m", {}, status="succeeded")
    for key, state, cost in (("s1", "a", 0.5), ("s2", "b", 0.25)):
        db.record("r", "activity", key, state=state, status="done",
                  data={"kind": "agent", "path": state, "state": state, "meta": {"cost": cost}})
    db.close()
    spent = evidence.spend({"data_dir": str(tmp_path), "stategraph_runs": [{"id": "r"}]})
    assert spent["cost"] == 0.75 and [(s["path"], s["cost"]) for s in spent["by_state"]] == [("a", 0.5), ("b", 0.25)]
    with pytest.raises(ValueError, match="started no machine"):
        evidence.spend({"data_dir": str(tmp_path / "empty"), "stategraph_runs": [{"id": "r"}]})
