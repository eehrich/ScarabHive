"""A run refuses to start when its machine, or one it imports, calls a package.module:function this
process cannot import -- the code predates the machine files (pulled, not restarted). Without the
check the run failed only in the state that first called it, after the work before it."""
from __future__ import annotations

import importlib

import pytest

from plugins.stategraph.tests.stategraph_testkit import FakeBackend, Harness, runnable, settle

HERE = "plugins.stategraph.tests.test_plugin_stategraph_start_check"
ran: list[str] = []


async def step(sg, **kw):
    ran.append("step")
    return {}


async def step_first(sg, **kw):
    ran.append("first")
    return {}


def _ran() -> list[str]:
    """The list of the module the run imports (pytest may load this file under another name)."""
    return importlib.import_module(HERE).ran


def _files(sub_call: str, first_call: str = f"{HERE}:step") -> dict[str, str]:
    return {
        "m.yaml": f"""
            stategraph: 1
            id: m
            imports:
              sub: ./sub.yaml
            initial: first
            states:
              first:
                do:
                  call: {first_call}
                transitions:
                  - target: nested
              nested:
                do:
                  machine: sub
                transitions:
                  - target: done
              done:
                type: final
        """,
        "sub.yaml": f"""
            stategraph: 1
            id: sub
            initial: work
            states:
              work:
                do:
                  call: {sub_call}
                transitions:
                  - target: done
              done:
                type: final
        """,
    }


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def test_a_missing_function_in_an_imported_machine_stops_the_run_before_any_state(harness):
    _ran().clear()
    manager = harness.manager()
    with pytest.raises(ValueError, match=rf"sub\.states\.work\.do: cannot import {HERE}:no_such_step"):
        await manager.start(runnable(_files(f"{HERE}:no_such_step")), backend=FakeBackend({}))
    assert _ran() == [], "nothing ran"
    assert harness.store.list_runs() == [], "no run row is left behind"


@pytest.mark.parametrize("ref", ["no_such_module_xyz:step", f"{HERE}:HERE"])
async def test_a_missing_module_or_a_non_callable_is_refused_too(harness, ref):
    with pytest.raises(ValueError, match="cannot import|is not callable"):
        await harness.manager().start(runnable(_files(ref)), backend=FakeBackend({}))


async def test_a_machine_whose_calls_all_import_runs(harness):
    _ran().clear()
    manager = harness.manager()
    run_id = await manager.start(runnable(_files(f"{HERE}:step")), backend=FakeBackend({}))
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded", row.get("error")
    assert _ran() == ["step", "step"]


NESTED = """
    stategraph: 1
    id: m
    initial: a
    states:
      a:
        do:
          map: "[1]"
          as: x
          each:
            call: {each}
            args: {{call: "data.not:a_ref"}}
        finally:
          call: {final}
        transitions:
          - target: done
      done:
        type: final
"""


@pytest.mark.parametrize("each,final,where", [
    (f"{HERE}:nope", f"{HERE}:step", r"m\.states\.a\.do\.each"),
    (f"{HERE}:step", f"{HERE}:nope", r"m\.states\.a\.finally"),
])
async def test_calls_inside_a_map_or_a_finally_are_checked_too_but_not_argument_data(harness, each, final, where):
    files = {"m.yaml": NESTED.format(each=each, final=final)}
    with pytest.raises(ValueError, match=rf"{where}: cannot import {HERE}:nope") as raised:
        await harness.manager().start(runnable(files), backend=FakeBackend({}))
    assert "data.not" not in str(raised.value), "an argument named call is data"


async def test_a_module_that_breaks_on_import_is_refused_like_a_missing_one(harness, tmp_path, monkeypatch):
    (tmp_path / "broken_steps_xyz.py").write_text("def step(:\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ValueError, match="cannot import broken_steps_xyz:step \\(SyntaxError"):
        await harness.manager().start(runnable(_files("broken_steps_xyz:step")), backend=FakeBackend({}))


async def test_a_run_with_mocks_may_name_a_function_not_written_yet(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(_files(f"{HERE}:step", first_call=f"{HERE}:not_written_yet")),
                                 backend=FakeBackend({}), mocks={"first": {}})
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded", row.get("error")


async def test_a_fork_replays_a_step_whose_function_is_gone_since(harness, monkeypatch):
    _ran().clear()
    manager = harness.manager()
    source = await manager.start(runnable(_files(f"{HERE}:step", first_call=f"{HERE}:step_first")),
                                 backend=FakeBackend({}))
    assert (await settle(manager, source))["status"] == "succeeded"
    monkeypatch.delattr(importlib.import_module(HERE), "step_first")
    fork = await manager.fork(source, at_step=1, backend=FakeBackend({}))
    row = await settle(manager, fork)
    assert row["status"] == "succeeded", row.get("error")
    assert _ran() == ["first", "step", "step"], "the fork replayed first and ran the rest live"
