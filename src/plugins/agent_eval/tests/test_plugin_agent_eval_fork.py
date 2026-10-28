"""Fork tasks: a stategraph run frozen by export_run, forked in every eval run's own data directory at a state of
the source, run on by the machine until it enters ``until``. The source is a mock-only run, so the fork needs no
LLM -- what is exercised is the export, the suite, the runner's copy and the real child around stategraph."""
from __future__ import annotations

import asyncio
import json
import os
import textwrap
import time
from pathlib import Path

import pytest

from agent_system.paths import DATA_DIR_ENV
from plugins.agent_eval import runner
from plugins.agent_eval.suite import fork_steps, load_suite
from plugins.stategraph.tests.stategraph_testkit import Harness, runnable, settle

#: Between b and c a timer: it goes on by itself, so a fork has to wait it out, not stop there.
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
    transitions: [{target: breather}]
  breather:
    after: 1s
    transitions: [{target: c}]
  c:
    do: {agent: x, task: three}
    transitions: [{target: done}]
  done: {type: final}
"""
#: Between b and c a wait for an event that nobody sends in an eval run.
ASKING = """
stategraph: 1
id: m
events: {go: {}}
initial: a
states:
  a:
    do: {agent: x, task: one}
    transitions: [{target: b}]
  b:
    do: {agent: x, task: two}
    transitions: [{target: ask}]
  ask:
    transitions: [{trigger: go, target: c}]
  c:
    do: {agent: x, task: three}
    transitions: [{target: done}]
  done: {type: final}
"""
#: After b a composite stage: a fork until it stops at the first state inside it (the composite has no enter).
STAGED = """
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: x, task: one}
    transitions: [{target: b}]
  b:
    do: {agent: x, task: two}
    transitions: [{target: stage}]
  stage:
    initial: draft
    states:
      draft:
        do: {agent: x, task: four}
        transitions: [{target: edit}]
      edit:
        do: {agent: x, task: five}
        transitions: [{target: end}]
      end: {type: final}
    transitions: [{target: done}]
  done: {type: final}
"""
#: The composite's first state is a choice that leads straight out: none of its states is entered.
PASSING = STAGED.replace("""    initial: draft
    states:
      draft:""", """    initial: route
    states:
      route:
        type: choice
        transitions: [{guard: else, target: done}]
      draft:""")
#: o holds x, then the composite cc; after o, mid -- and o once more. Entering o again starts the visits of the
#: states inside it anew: what counts cc's entry must not take that for one.
NESTED = """
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: x, task: one}
    transitions: [{target: o}]
  o:
    initial: x
    states:
      x:
        do: {agent: x, task: two}
        transitions: [{target: cc}]
      cc:
        initial: y
        states:
          y:
            do: {agent: x, task: three}
            transitions: [{target: yend}]
          yend: {type: final}
        transitions: [{target: oend}]
      oend: {type: final}
    transitions:
      - {guard: "run.visits.get('mid', 0) < 1", target: mid}
      - {target: done}
  mid:
    do: {agent: x, task: four}
    transitions: [{target: o}]
  done: {type: final}
"""
MOCKS = {"a": "1", "b": "2", "c": "3", "draft": "4", "edit": "5", "x": "6", "y": "7", "mid": "8"}


async def _source(data: Path, machine: str = MACHINE, watchpoints=()) -> tuple:
    """(data directory, run id, harness): one finished mock run of *machine*, a store document beside it. With
    *watchpoints* the run was watched -- someone debugged it -- and continued at every halt until it ended."""
    harness = Harness(data / "stategraph")
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": machine}), mock_only=True, mocks=MOCKS,
                                 watchpoints=list(watchpoints))
    for _ in range(50):
        await manager.wait(run_id, timeout=5)
        live = manager.live.get(run_id)
        if live is None or live.task.done():
            break
        if live.ctx.status == "paused":
            manager.control(run_id, "continue")
        elif machine is ASKING:  # the event it waits for
            manager.send_event(run_id, "go")
        else:  # the timer
            await asyncio.sleep(0.2)
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded", row  # fixture
    if watchpoints:
        assert any(r["status"] == "paused" for r in harness.store.rows(run_id, kinds=("trace",))), "never halted"
    (data / "store" / "ns").mkdir(parents=True)
    (data / "store" / "ns" / "doc.json").write_text("{}", encoding="utf-8")
    return data, run_id, harness


def _closed_source(data: Path, **kwargs) -> tuple:
    """_source for a test that runs the child, which brings its own event loop: (data directory, run id)."""
    async def make():
        made = await _source(data, **kwargs)
        await made[2].close()
        return made[:2]
    return asyncio.run(make())


@pytest.fixture
async def source(tmp_path):
    made = await _source(tmp_path / "source-data")
    yield made
    await made[2].close()


def _suite(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _fork_suite(tmp_path: Path, fork: str, **top: str) -> Path:
    extra = "".join(f"{k}: {v}\n" for k, v in top.items())
    return _suite(tmp_path, f"""
name: s
agent: a
{extra}tasks:
  - id: t1
    fork: {fork}
    check: {{expect: {{contains: [""]}}}}
""")


def test_export_run_freezes_the_run_and_the_data_paths_it_names(source, tmp_path):
    data, run_id, _ = source
    out = runner.export_run(run_id, tmp_path / "export", data=data, include=["store/ns"])
    exported = json.loads((out / "source.json").read_text(encoding="utf-8"))
    assert exported["run"]["id"] == run_id and exported["journal"], exported["run"]
    assert (out / "data" / "store" / "ns" / "doc.json").is_file()
    assert {k: exported["run"][k] for k in runner._UNEXPORTED} == dict.fromkeys(runner._UNEXPORTED)
    assert exported["run"]["definition"] and exported["run"]["params"] is not None  # what the fork replays stays
    with pytest.raises(ValueError, match="not an empty directory"):
        runner.export_run(run_id, out, data=data)
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not an empty directory"):
        runner.export_run(run_id, tmp_path / "a-file", data=data)
    nested = runner.export_run(run_id, tmp_path / "nested", data=data, include=["store/ns", "store"])
    assert (nested / "data" / "store" / "ns" / "doc.json").is_file()
    (tmp_path / "outside").write_text("not the run's", encoding="utf-8")  # there, but no path of the data dir
    with pytest.raises(ValueError, match="no path"):
        runner.export_run(run_id, tmp_path / "e2", data=data, include=["../outside"])
    with pytest.raises(ValueError, match="no run"):
        runner.export_run("nope", tmp_path / "e3", data=data)


async def test_export_run_refuses_a_run_that_has_not_ended(source, tmp_path):
    data, _, harness = source
    paused = await harness.manager().start(runnable({"m.yaml": MACHINE}), mock_only=True, pause_at_start=True,
                                           mocks=MOCKS)
    with pytest.raises(ValueError, match="export a run that ended"):
        runner.export_run(paused, tmp_path / "export", data=data)


def test_a_failed_export_leaves_nothing_behind(source, tmp_path, monkeypatch):
    data, run_id, _ = source

    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(runner.shutil, "copytree", broken)
    with pytest.raises(OSError):
        runner.export_run(run_id, tmp_path / "export", data=data, include=["store/ns"])
    assert not (tmp_path / "export").exists()
    from plugins.agent_eval.__main__ import main
    monkeypatch.chdir(tmp_path)  # main enters the project directory; this puts it back afterwards
    assert main(["export-run", run_id, str(tmp_path / "export"), "--data", str(data), "--include", "store/ns"]) == 1


def test_a_fork_task_finds_its_step_and_refuses_what_cannot_run(source, tmp_path):
    data, run_id, _ = source
    runner.export_run(run_id, tmp_path / "export", data=data)
    suite = load_suite(_fork_suite(tmp_path, "{source: export, at: b, until: c}"))
    assert suite.tasks[0].fork.step == 1  # a is the top-level step 0
    assert suite.tasks[0].fork.source == str((tmp_path / "export").resolve())
    for fork, top, why in [("{source: export, at: b, visit: 2}", {}, "1 time"),
                           ("{source: export, at: nowhere}", {}, "0 time"),
                           ("{source: missing, at: b}", {}, "no source.json"),
                           ("{source: export, at: b}", {"data": "shared"}, "data: fresh"),
                           ("{source: export, at: b}", {"chain": "true"}, "no chain"),
                           ("{source: export, at: b, until: nowhere, definition: snapshot}", {}, "no state"),
                           ("{source: export, at: b, until: done, definition: snapshot}", {}, "never stops")]:
        # done: a top-level final, where nothing stops
        with pytest.raises(ValueError, match=why):
            load_suite(_fork_suite(tmp_path, fork, **top))
    both = _suite(tmp_path, """
        name: s
        agent: a
        tasks:
          - {id: t1, input: x, fork: {source: export, at: b}, check: {expect: {contains: [ok]}}}
    """)
    with pytest.raises(ValueError, match="exactly one of input and fork"):
        load_suite(both)
    newer = json.loads((tmp_path / "export" / "source.json").read_text(encoding="utf-8"))
    newer["run"]["definition"] = json.dumps({"format": "of a later stategraph"})
    (tmp_path / "newer").mkdir()
    (tmp_path / "newer" / "source.json").write_text(json.dumps(newer), encoding="utf-8")
    with pytest.raises(ValueError, match="until: KeyError"):  # a suite list must not break on one such suite
        load_suite(_fork_suite(tmp_path, "{source: newer, at: b, until: c, definition: snapshot}"))
    own = _suite(tmp_path, """
        name: s
        agent: a
        tasks:
          - {id: t1, fork: {source: export, at: b}, agent_config: {template_vars: {x: y}}, check: {expect: {contains: [ok]}}}
    """)
    with pytest.raises(ValueError, match="take no agent_config"):
        load_suite(own)
    idle = _suite(tmp_path, """
        name: s
        agent: a
        arms:
          base: {}
          think: {agent_config: {llm_profile: [think]}}
        tasks:
          - {id: t1, fork: {source: export, at: b}, check: {expect: {contains: [ok]}}}
    """)
    with pytest.raises(ValueError, match=r"arms \['think'\]: every task is a fork task"):
        load_suite(idle)


def test_every_fork_run_gets_the_exports_data_as_new_files_and_the_fork_in_its_job(source, tmp_path,
                                                                                   monkeypatch):
    data, run_id, _ = source
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "eval-data"))
    monkeypatch.setattr(runner, "check_arms", lambda suite: None)
    export = runner.export_run(run_id, tmp_path / "export", data=data, include=["store/ns"])
    old = time.time() - 3 * 86400
    os.utime(export / "data" / "store" / "ns" / "doc.json", (old, old))
    seen = []

    def child(args, cwd, **kwargs):
        run_data = Path(kwargs["env"][DATA_DIR_ENV])
        doc = run_data / "store" / "ns" / "doc.json"
        job = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
        seen.append((doc.is_file() and doc.stat().st_mtime > old + 86400, job["fork"]))
        Path(job["result_path"]).write_text(json.dumps({"answer": ""}), encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "run_bounded", child)
    suite_path = _fork_suite(tmp_path, "{source: export, at: b, until: c, mocks: {c: x}}", repeats="2")
    runner.run_suite(suite_path)
    assert seen == [(True, {"source": str(export.resolve() / "source.json"), "at_step": 1, "until": "c",
                            "definition": "current", "mocks": {"c": "x"}, "stategraph": "stategraph"})] * 2, seen


def _run_child(tmp_path: Path, monkeypatch, fork: str, request_id: str) -> dict:
    """The real child, in-process, with the real configuration's stategraph instance; its result."""
    from plugins.agent_eval import child

    suite = load_suite(_fork_suite(tmp_path, fork))
    env = runner.child_env(suite, tmp_path / "run-data")
    for key in set(os.environ) - set(env):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    result_path = tmp_path / "result.json"
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"agent": "file_ops_test_agent", "overlays": [], "input": "fork at b",
                               "fork": runner._fork_job(suite.tasks[0]), "request_id": request_id,
                               "session_user": runner.SESSION_USER, "result_path": str(result_path)}),
                   encoding="utf-8")
    try:
        child.main(str(job))
    except SystemExit as e:
        assert e.code in (0, 1, None)
    return json.loads(result_path.read_text(encoding="utf-8"))


def _fork_row_and_done(tmp_path: Path, run_id: str) -> tuple:
    """The fork's row in the run's runs.db, and the keys of the activities it finished."""
    from plugins.stategraph.engine.journal import RunStore
    runs = RunStore.read_only(tmp_path / "run-data" / "stategraph" / "runs.db")
    try:
        return runs.get_run(run_id), {r["key"] for r in runs.rows(run_id, kinds=("activity",))
                                      if r["status"] == "done"}
    finally:
        runs.close()


@pytest.mark.parametrize("watched", [False, True], ids=["a_plain_source", "a_source_someone_watched"])
def test_the_child_forks_the_source_and_stops_where_the_task_says(tmp_path, monkeypatch, watched):
    """The source goes into the run's runs.db, the fork runs as <request id>_sg1 (its agents' cost is the run's),
    b runs, the timer after it is waited out, and entering c holds it -- then it is terminated, c never ran. A
    source's own watchpoints do not halt the fork short of c."""
    data, run_id = _closed_source(tmp_path / "source-data", watchpoints=["run.step"] if watched else ())
    export = runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, "{source: export, at: b, until: c, definition: snapshot}",
                        "ev-fork-1")
    assert not result["refused"] and not result["exception"], result
    assert result["fork"]["run_id"] == "ev-fork-1_sg1" and result["fork"]["until_reached"], result
    assert result["model"] is None, "the model of the suite's agent, which never ran"
    row, done = _fork_row_and_done(tmp_path, "ev-fork-1_sg1")
    assert (row["parent_run"], row["status"]) == (run_id, "cancelled"), row
    journal = json.loads((export / "source.json").read_text(encoding="utf-8"))["journal"]
    assert f"s{fork_steps(journal, 'b')[0]}" in done, done
    assert f"s{fork_steps(journal, 'c')[0]}" not in done, done


def test_until_a_composite_stops_the_fork_at_the_first_state_inside_it(tmp_path, monkeypatch):
    data, run_id = _closed_source(tmp_path / "source-data", machine=STAGED)
    runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, "{source: export, at: b, until: stage, definition: snapshot}",
                        "ev-fork-4")
    assert result["fork"]["until_reached"], result
    row, done = _fork_row_and_done(tmp_path, "ev-fork-4_sg1")
    assert row["status"] == "cancelled" and "s1" in done and not any(k.startswith("s2") for k in done), done


@pytest.mark.parametrize("at", ["b", "a"], ids=["later", "at_the_first_state"])
def test_until_a_composite_a_choice_leads_out_of_still_stops_the_fork(tmp_path, monkeypatch, at):
    """No state inside it is entered; forked at a, its entry is the fork's first step."""
    data, run_id = _closed_source(tmp_path / "source-data", machine=PASSING.replace(
        "transitions: [{target: b}]", "transitions: [{target: stage}]") if at == "a" else PASSING)
    runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, f"{{source: export, at: {at}, until: stage, definition: snapshot}}",
                        "ev-fork-5")
    assert result["fork"]["until_reached"] and result["fork"]["status"] == "paused", result
    assert _fork_row_and_done(tmp_path, "ev-fork-5_sg1")[0]["status"] == "cancelled"


def test_until_a_nested_composite_stops_at_its_entry_not_at_its_parents(tmp_path, monkeypatch):
    """Forked at mid, until cc: o is entered again, then x runs, then cc is entered -- there it stops, x done."""
    data, run_id = _closed_source(tmp_path / "source-data", machine=NESTED)
    export = runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, "{source: export, at: mid, until: cc, definition: snapshot}",
                        "ev-fork-7")
    assert result["fork"]["until_reached"], result
    journal = json.loads((export / "source.json").read_text(encoding="utf-8"))["journal"]
    again = fork_steps(journal, "x")[1]  # x's second entry, after mid
    done = _fork_row_and_done(tmp_path, "ev-fork-7_sg1")[1]
    assert f"s{again}" in done and f"s{fork_steps(journal, 'y')[1]}" not in done, (done, again)


def test_until_the_state_it_forks_at_is_its_next_entry_not_the_fork_point(tmp_path, monkeypatch):
    """b is entered once: forked at b until b, the fork does not stop where it starts -- it runs on to its end."""
    data, run_id = _closed_source(tmp_path / "source-data")
    export = runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, "{source: export, at: b, until: b, definition: snapshot}",
                        "ev-fork-6")
    assert (result["fork"]["status"], result["fork"]["until_reached"]) == ("succeeded", False), result
    journal = json.loads((export / "source.json").read_text(encoding="utf-8"))["journal"]
    assert f"s{fork_steps(journal, 'c')[0]}" in _fork_row_and_done(tmp_path, "ev-fork-6_sg1")[1]  # c ran


def test_a_fork_waiting_for_an_event_nobody_sends_is_stopped_short_of_until(tmp_path, monkeypatch):
    data, run_id = _closed_source(tmp_path / "source-data", machine=ASKING)
    runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, "{source: export, at: b, until: c, definition: snapshot}",
                        "ev-fork-3")
    assert result["fork"]["status"] == "waiting" and not result["fork"]["until_reached"], result
    assert _fork_row_and_done(tmp_path, "ev-fork-3_sg1")[0]["status"] == "cancelled"


def test_a_fork_that_cannot_start_is_refused_not_scored(tmp_path, monkeypatch):
    """A fork onto the current definition of a machine this installation does not have: the suite is wrong, so the
    run does not count against the arm."""
    data, run_id = _closed_source(tmp_path / "source-data")
    runner.export_run(run_id, tmp_path / "export", data=data)
    result = _run_child(tmp_path, monkeypatch, "{source: export, at: b, until: c, definition: current}", "ev-fork-2")
    assert (result["refused"] or "").startswith("the fork did not start:"), result


class _Server:
    """A stategraph server stand-in for run_fork: the run's rows come from *rows*, one per wait."""

    def __init__(self, rows, terminate_fails=False):
        from types import SimpleNamespace
        self.rows = list(rows)
        self.waits = 0
        self.terminated = []
        server = self

        async def wait(run_id):
            server.waits += 1
            return server.rows[min(server.waits, len(server.rows)) - 1]

        async def fork_run(*args, **kwargs):
            return {}

        async def control_run(run_id, action, **kwargs):
            if terminate_fails:
                raise RuntimeError("lease lost")
            server.terminated.append(run_id)

        self.service = SimpleNamespace(fork_run=fork_run, control_run=control_run)
        self.run_store = SimpleNamespace(import_run=lambda *a, **k: None)
        self.run_manager = SimpleNamespace(wait=wait, live={})

    def use_registry(self, registry):
        pass


def _waiting(accepts, inbox=()):
    return {"status": "waiting", "output": None, "error": None,
            "view": {"frames": [{"prefix": "", "accepts": accepts, "deadline": "2026-10-29T00:00:00"}],
                     "inbox": list(inbox)}}


@pytest.mark.parametrize("rows, waits", [
    ([_waiting([]), _waiting([]), _waiting(["go"])], 3),                   # a timer: waited out
    ([_waiting(["go"], [{"name": "go"}]), _waiting(["go"])], 2),            # an event in the inbox: it takes it
    ([_waiting(["go"])], 1),                                                # a wait with a timeout that takes go
], ids=["a_timer", "an_event_due", "a_timed_wait_that_takes_an_event"])
async def test_a_fork_is_stopped_at_a_wait_only_when_it_takes_an_event(tmp_path, rows, waits):
    from plugins.agent_eval import child
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"run": {"id": "src", "machine_id": "m"}, "journal": []}), encoding="utf-8")
    server = _Server(rows)
    result = await child.run_fork({"stategraph": server}, {"source": str(source), "stategraph": "stategraph",
                                                           "at_step": 1, "until": None, "definition": "current"},
                                  "ev-1", "u")
    assert (server.waits, server.terminated, result["fork"]["status"]) == (waits, ["ev-1_sg1"], "waiting"), result


async def test_a_terminate_that_fails_is_an_error_of_the_run_not_a_crash(tmp_path):
    from plugins.agent_eval import child
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"run": {"id": "src", "machine_id": "m"}, "journal": []}), encoding="utf-8")
    result = await child.run_fork({"stategraph": _Server([_waiting(["go"])], terminate_fails=True)},
                                  {"source": str(source), "stategraph": "stategraph", "at_step": 1, "until": None,
                                   "definition": "current"}, "ev-1", "u")
    assert result["errors"] == ["the fork was not terminated: RuntimeError: lease lost"], result
