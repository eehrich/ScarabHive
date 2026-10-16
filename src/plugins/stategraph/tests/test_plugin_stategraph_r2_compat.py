"""Compatibility of runs started before a contract change (review round 2, compat lens).

COMPAT-1: an explicit null param is no value since journal format 2 (the default applies). A run of format 1 bound it
as the value; its resume and its forks must bind it the same way, or the replay renders other inputs and diverges.
Every run goes through the real RunManager on a RunStore under tmp_path; only FakeBackend stands behind the engine.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.engine.interpreter import bind_params
from plugins.stategraph.model.spec import ParamSpec
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, held, runnable,
                                                          settle, until)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

MACHINE = """\
stategraph: 1
id: m
params: {{p: {{type: string{default}}}}}
initial: a
states:
  a:
    do: {{tool: t, args: {{v: "{{{{ params.p }}}}"}}}}
    transitions: [{{target: b}}]
  b:
    do: {{tool: t2, args: {{}}, idempotent: true}}
    transitions: [{{target: done}}]
  done: {{type: final, output: "{{{{ params.p }}}}"}}
"""
WITHOUT_DEFAULT = {"m.yaml": MACHINE.format(default="")}
WITH_DEFAULT = {"m.yaml": MACHINE.format(default=", default: dflt")}


async def _old_run(h: Harness, journal_format: int) -> str:
    """A run as the engine before format 2 left it: started with {p: null}, ``a`` called with v=None, interrupted in
    ``b``. Recorded on a machine without a default (where both bindings give None), then its row carries the machine
    with the default -- the case where the two bindings differ -- and the given journal format."""
    backend = FakeBackend({"a": "A", "b": held(asyncio.Event())})
    manager = h.manager()
    run_id = await manager.start(runnable(WITHOUT_DEFAULT), backend=backend, params={"p": None})
    await until(lambda: backend.count("b") == 1, what="b in flight")
    await manager.shutdown()
    assert [c["args"] for c in backend.calls if c["path"] == "a"] == [{"v": None}]  # fixture: recorded with None
    h.store.update_run(run_id, definition=runnable(WITH_DEFAULT).snapshot(), journal_format=journal_format)
    assert h.store.get_run(run_id)["status"] == "interrupted"
    return run_id


async def test_format_1_run_resumes_with_null_param_as_recorded(tmp_path):
    h = Harness(tmp_path)
    try:
        run_id = await _old_run(h, journal_format=1)
        backend = FakeBackend({"a": "A", "b": "B"})
        manager = h.manager()
        await manager.resume(run_id, backend=backend)
        row = await settle(manager, run_id)
        assert row["status"] == "succeeded", row["error"]
        assert row["output"] is None  # the null param, not the default
        assert backend.count("a") == 0  # replayed from the journal
    finally:
        await h.close()


async def test_format_2_run_binds_null_param_to_its_default(tmp_path):
    """The control: the same journal under format 2 is bound the new way, so its replay diverges -- what a format-1
    run would hit without its format."""
    h = Harness(tmp_path)
    try:
        run_id = await _old_run(h, journal_format=2)
        manager = h.manager()
        await manager.resume(run_id, backend=FakeBackend({"a": "A", "b": "B"}))
        row = await settle(manager, run_id)
        assert row["status"] == "failed" and row["error"]["type"] == "diverged", row
    finally:
        await h.close()


async def test_fork_of_format_1_run_keeps_its_binding(tmp_path):
    h = Harness(tmp_path)
    try:
        run_id = await _old_run(h, journal_format=1)
        backend = FakeBackend({"a": "A", "b": "B"})
        manager = h.manager()
        fork_id = await manager.fork(run_id, backend=backend)
        row = await settle(manager, fork_id)
        assert row["journal_format"] == 1
        assert row["status"] == "succeeded", row["error"]
        assert backend.count("a") == 0
    finally:
        await h.close()


async def test_new_run_is_format_2_and_binds_null_to_default(tmp_path):
    h = Harness(tmp_path)
    try:
        backend = FakeBackend({"a": "A", "b": "B"})
        row = await h.run(WITH_DEFAULT, backend=backend, params={"p": None})
        assert row["journal_format"] == 2 and row["status"] == "succeeded", row
        assert [c["args"] for c in backend.calls if c["path"] == "a"] == [{"v": "dflt"}]
    finally:
        await h.close()


def test_bind_params_null_is_value_skips_default_required_and_enum():
    declared = {"d": ParamSpec(type="string", default="dflt"), "r": ParamSpec(type="string", required=True),
                "e": ParamSpec(type="string", enum=["x"])}
    given = {"d": None, "r": None, "e": None}
    assert bind_params(declared, given, "m", null_is_value=True) == {"d": None, "r": None, "e": None}
    with pytest.raises(Exception):
        bind_params(declared, given, "m")


def test_set_param_before_resume_checks_a_format_1_run_its_way(tmp_path):
    """``set params.<name>`` on a stopped run binds its params as its resume will: a format-1 run that has a required
    param as null may get another param set."""
    from types import SimpleNamespace

    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.service import ServiceError, StateGraphService

    files = {"m.yaml": "stategraph: 1\nid: m\nparams: {r: {type: string, required: true}, t: {type: string}}\n"
                       "initial: done\nstates:\n  done: {type: final}\n"}
    store = RunStore(tmp_path / "runs.db")
    try:
        for run_id, journal_format in (("old", 1), ("new", 2)):
            store.create_run(run_id, "m", runnable(files).snapshot(), params={"r": None}, mocks={},
                             status="interrupted", journal_format=journal_format)
        service = SimpleNamespace(run_store=store)
        StateGraphService._check_param(service, "old", "t", "x")  # no raise: the null stays its value
        with pytest.raises(ServiceError) as refused:
            StateGraphService._check_param(service, "new", "t", "x")
        assert refused.value.status == 422 and "requires parameter 'r'" in refused.value.message
    finally:
        store.close()
