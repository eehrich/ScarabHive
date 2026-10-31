"""agent_eval's runner and child: scheduling, setup, chains, budget and stop around a stand-in agent run, and
one child run through the real agent-run path (the LLM is the root conftest's fake)."""
from __future__ import annotations

import json
import os
import sys
import textwrap
from collections import Counter
from pathlib import Path

import pytest

from agent_system.paths import DATA_DIR_ENV
from plugins.agent_eval import runner, store


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch, request):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "data"))
    # The stand-in suites name agent 'a', which no configuration has; check_arms has its own test.
    if "check_arms" not in request.node.name:
        monkeypatch.setattr(runner, "check_arms", lambda suite: None)
    return tmp_path / "data"


def _usage_db(data: Path, rows) -> None:
    import sqlite3
    db = data / "context_usage_tracker" / "usage.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE usage_snapshots (request_id TEXT, cost REAL, prompt_tokens INTEGER, "
                     "completion_tokens INTEGER)")
        conn.executemany("INSERT INTO usage_snapshots VALUES (?, ?, 10, 2)", rows)


def _suite(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _stand_in(answer=lambda task, workspace: "ok", cost=0.0, status="done", seen=None):
    """An agent run that writes a marker into its workspace and answers; records what it saw there."""
    def run(suite, run_id, job, task, jdir, workspace, data):
        if seen is not None:
            seen.append((job["arm"], task.id, job["rep"], sorted(p.name for p in workspace.iterdir())))
        (workspace / f"done-{task.id}").write_text("x", encoding="utf-8")
        return {"status": status, "answer": answer(task, workspace), "cost": cost, "request_id": "r"}
    return run


def _records(run_id: str):
    return store.read_records(run_id)


def test_every_arm_runs_every_task_every_repeat_interleaved(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in())
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 2
        arms: {one: {}, two: {}, three: {}}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
          - {id: t2, input: x, check: {expect: {contains: [ok]}}}
    """)
    run_id = runner.run_suite(path, progress=lambda _: None)
    records = _records(run_id)
    assert Counter((r["arm"], r["task"], r["rep"]) for r in records) == Counter(
        {(a, t, rep): 1 for a in ("one", "two", "three") for t in ("t1", "t2") for rep in (1, 2)})
    # Interleaved: the arms of one task and repeat start next to each other, not arm after arm.
    order = [(r["task"], r["rep"]) for r in sorted(records, key=lambda r: r["n"])]
    assert order[:3] == [("t1", 1)] * 3
    assert all(r["scored"] and r["score"] == 1.0 for r in records)
    assert store.read_meta(run_id)["status"] == "done"


def test_setup_runs_once_and_every_run_starts_from_its_copy(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "_agent_run", _stand_in(seen=seen))
    counter = tmp_path / "setups.txt"
    setup = f'"{sys.executable}" -c "open(\'seed.txt\',\'w\').write(\'s\'); open(r\'{counter}\',\'a\').write(\'1\')"'
    path = _suite(tmp_path, f"""
        name: s
        agent: a
        repeats: 2
        arms: {{one: {{}}, two: {{}}}}
        tasks:
          - id: t1
            input: x
            setup: {json.dumps(setup)}
            check: {{expect: {{contains: [ok]}}}}
    """)
    runner.run_suite(path, progress=lambda _: None)
    assert counter.read_text() == "1", "setup must run once per task, not per run"
    assert len(seen) == 4 and all(files == ["seed.txt"] for *_, files in seen), seen


def test_python_in_a_setup_is_the_runners_interpreter(tmp_path, monkeypatch):
    """A setup command says plain `python` -- with no environment activated (a PATH without any python) it must
    still be the interpreter the runner runs on, not whatever else is first or nothing. The agent under test
    keeps the PATH it would have in production."""
    seen = []
    monkeypatch.setattr(runner, "_agent_run", _stand_in(seen=seen))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert runner.child_env(runner.load_suite(Path(runner.__file__).parent / "evals" / "hello" / "suite.yaml"),
                            tmp_path)["PATH"] == str(tmp_path / "empty")
    setup = 'python -c "import sys; open(\'py.txt\',\'w\').write(sys.executable)"'
    path = _suite(tmp_path, f"""
        name: s
        agent: a
        tasks:
          - {{id: t1, input: x, setup: {json.dumps(setup)}, check: {{expect: {{contains: [ok]}}}}}}
    """)
    records = _records(runner.run_suite(path, progress=lambda _: None))
    assert records[0]["status"] == "done", records
    assert seen[0][3] == ["py.txt"], seen


def test_a_failed_setup_starts_none_of_its_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in())
    path = _suite(tmp_path, f"""
        name: s
        agent: a
        tasks:
          - {{id: t1, input: x, setup: {json.dumps(f'"{sys.executable}" -c "raise SystemExit(3)"')}, check: {{expect: {{contains: [ok]}}}}}}
          - {{id: t2, input: x, check: {{expect: {{contains: [ok]}}}}}}
    """)
    records = {r["task"]: r for r in _records(runner.run_suite(path, progress=lambda _: None))}
    assert records["t1"]["status"] == "setup_failed" and not records["t1"]["scored"]
    assert records["t2"]["scored"]


def test_a_chain_carries_the_workspace_from_task_to_task(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(runner, "_agent_run", _stand_in(seen=seen))
    path = _suite(tmp_path, """
        name: s
        agent: a
        chain: true
        repeats: 2
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
          - {id: t2, input: x, check: {expect: {contains: [ok]}}}
    """)
    runner.run_suite(path, progress=lambda _: None)
    by_rep = {(task, rep): files for _, task, rep, files in seen}
    assert by_rep[("t2", 0)] == ["done-t1"], "the second task must see what the first left"
    assert by_rep[("t1", 1)] == [], "a new repeat starts from scratch"


def test_budget_stops_starting_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in(cost=1.0))
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 4
        budget_usd: 1.5
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    statuses = Counter(r["status"] for r in _records(runner.run_suite(path, progress=lambda _: None)))
    assert statuses == {"done": 2, "budget": 2}


def test_a_run_with_runs_that_did_not_count_is_incomplete_and_no_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in(cost=1.0))
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 2
        budget_usd: 0.5
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    run_id = runner.run_suite(path, progress=lambda _: None)
    assert store.read_meta(run_id)["status"] == "incomplete"
    with pytest.raises(ValueError, match="incomplete"):
        store.set_baseline("s", run_id)
    assert store.baselines() == {}


def test_the_check_reads_the_whole_answer_the_record_keeps_its_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in(answer=lambda task, ws: "Paris" + "." * 5000))
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 1
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [Paris]}}}
    """)
    (record,) = _records(runner.run_suite(path, progress=lambda _: None))
    assert record["score"] == 1.0 and len(record["answer"]) == runner._ANSWER_CHARS, record["detail"]


def test_a_timed_out_run_scores_zero_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in(status="timeout"))
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 1
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    (record,) = _records(runner.run_suite(path, progress=lambda _: None))
    assert record["scored"] and record["score"] == 0.0 and record["detail"] == "timeout"


def test_stop_starts_no_further_runs(tmp_path, monkeypatch):
    def stop_after_first(suite, run_id, job, task, jdir, workspace, data):
        store.request_stop(run_id)
        return {"status": "done", "answer": "ok"}
    monkeypatch.setattr(runner, "_agent_run", stop_after_first)
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 3
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    run_id = runner.run_suite(path, progress=lambda _: None)
    assert Counter(r["status"] for r in _records(run_id)) == {"done": 1, "stopped": 2}
    assert store.read_meta(run_id)["status"] == "stopped"


def test_stop_after_the_last_run_started_leaves_the_run_done(tmp_path, monkeypatch):
    def stop_in_the_last(suite, run_id, job, task, jdir, workspace, data):
        store.request_stop(run_id)
        return {"status": "done", "answer": "ok"}
    monkeypatch.setattr(runner, "_agent_run", stop_in_the_last)
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 1
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    run_id = runner.run_suite(path, progress=lambda _: None)
    assert store.read_meta(run_id)["status"] == "done"
    store.set_baseline("s", run_id)
    assert store.baseline_for("s") == run_id


@pytest.mark.parametrize("task_wants, arm_wants", [("base", "other"), ("other", None), ("legacy", "other"),
                                                    ("layer", "other"), ("base", "suite")],
                         ids=["the_arm_over_the_task", "the_task_alone", "a_job_from_before_overlays",
                              "the_arms_layer_for_the_agent", "a_profile_of_the_suites_config"])
def test_child_runs_the_real_agent_run_path_with_the_overlays(tmp_path, data_dir, monkeypatch, task_wants,
                                                              arm_wants):
    """One child run in-process: real config, real bootstrap, agent-run's own session handling; the overlays'
    llm_profile must reach the agent (the fake client carries the profile's model name) -- the arm's over the
    task's, the task's where the arm leaves it, the arm of a job a runner from before 'overlays' wrote, an
    arm's layer for the agent (``agents``), which the registry builds it from, and a profile only the suite's
    ``config:`` defines."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    from plugins.agent_eval import child

    config = load_settings()
    agent = "file_ops_test_agent"
    base_profile = get_tool_server_config(agent, config).agent_config.llm_profile
    base_profile = base_profile[0] if isinstance(base_profile, list) else base_profile
    other = next(name for name, p in config.llm_system.profiles.items()
                 if name != base_profile and p.model_ref != config.llm_system.profiles[base_profile].model_ref)
    expected = config.llm_system.models[config.llm_system.profiles[other].model_ref].model
    profile = {"base": base_profile, "other": other, "suite": "zz_eval_suite_profile"}
    # Known only through the suite's layer: a run that does not get it fails its profile, it does not fall back.
    layer = {"llm_system": {"profiles": {"zz_eval_suite_profile": {
        "model_ref": config.llm_system.profiles[other].model_ref}}}}

    result_path = tmp_path / "result.json"
    job = tmp_path / "job.json"
    arm = {"llm_profile": [profile[arm_wants]]} if arm_wants else {}
    overlays = ({"agent_config": arm} if task_wants == "legacy"
                else {"overlays": [], "agents": {agent: arm}} if task_wants == "layer"
                else {"overlays": [{"llm_profile": [profile[task_wants]]}, arm]})
    job.write_text(json.dumps({
        "agent": agent, "input": "say the word marmalade", **overlays,
        "request_id": "ev-test-1", "session_user": runner.SESSION_USER, "result_path": str(result_path),
        "config": layer if arm_wants == "suite" else {},
    }), encoding="utf-8")
    # The environment the runner gives a child -- the root conftest names a session store of its own,
    # as the API does for processes it starts; a fresh-data run must not write there.
    env = runner.child_env(runner.load_suite(Path(runner.__file__).parent / "evals" / "hello" / "suite.yaml"), data_dir)
    for key in set(os.environ) - set(env):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    try:
        child.main(str(job))
    except SystemExit as e:  # agent-run exits 1 when the run reports errors; the result says which
        assert e.code in (0, 1, None)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["model"] == expected, result
    assert "marmalade" in result["answer"], result
    from agent_system.config import layers
    assert layers.process_layer == {}, "the suite's layer outlived the run"
    assert not result["refused"], result
    assert list((data_dir / "sessions").rglob(f"{result['session_id']}*")), "agent-run did not save the session"


def test_an_arms_layer_reaches_the_agents_that_inherit_from_it_and_leaves_the_config_alone():
    """Against the real configuration: an agent that inherits from a layered one inherits its overlay, '+x' acts
    on what the agent resolves to, and the configuration it was laid over is not changed (check_arms lays every
    arm over the same one)."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    from plugins.agent_eval.overlay import layer_agents

    config = load_settings()
    parent, heir = "skills_agent", "skills_agent_multimodal"
    assert config.plugins.servers[heir].type == parent, "fixture: the heir no longer inherits from the parent"
    before = get_tool_server_config(heir, config).agent_config
    layered = layer_agents(config, {parent: {"max_steps": before.max_steps + 7,
                                             "tools": {"allowed": ["+zz_eval_marker/*"]}}})
    after = get_tool_server_config(heir, layered).agent_config
    assert after.max_steps == before.max_steps + 7
    assert set(after.tools.allowed) == set(before.tools.allowed) | {"zz_eval_marker/*"}
    assert get_tool_server_config(heir, config).agent_config == before
    # Named heir first: it still gets its parent's overlay under its own.
    both = layer_agents(config, {heir: {"system_prompt": "heir"}, parent: {"max_steps": before.max_steps + 7}})
    assert get_tool_server_config(heir, both).agent_config.max_steps == before.max_steps + 7
    assert get_tool_server_config(heir, both).agent_config.system_prompt == "heir"
    with pytest.raises(ValueError, match="no_such_agent"):
        layer_agents(config, {"no_such_agent": {}})


def test_check_arms_refuses_an_arm_that_cannot_run(tmp_path):
    """Against the real configuration: each would end every run of its arm before the agent saw its task."""
    from plugins.agent_eval.suite import load_suite
    hello = Path(runner.__file__).parent / "evals" / "hello" / "suite.yaml"
    runner.check_arms(load_suite(hello))  # the shipped suite can run
    path = _suite(tmp_path, """
        name: s
        agent: file_ops_test_agent
        arms:
          base: {}
          ghost: {agent: no_such_agent}
          broken: {agent_config: {max_steps: lots}}
          nowhere: {agent_config: {llm_profile: [no_such_profile]}}
          stray: {agents: {no_such_agent: {}}}
          lost: {agents: {skills_agent: {llm_profile: [no_such_profile]}}}
          tool: {agents: {file_ops: {max_steps: 3}}}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    with pytest.raises(ValueError) as refused:
        runner.check_arms(load_suite(path))
    message = str(refused.value)
    assert "arm ghost" in message and "arm broken" in message and "arm nowhere" in message
    assert "arm stray: agents" in message and "arm lost, agent skills_agent: unknown llm_profile" in message
    assert "arm tool: agents: ['file_ops'] are not agents" in message
    assert "arm base" not in message


def test_check_arms_and_the_runs_see_the_suites_config(tmp_path, monkeypatch):
    """A profile only the suite's ``config:`` defines: the runner's checks know it, the expected models name its
    model, every run's job carries the layer -- and once the suite ran, the runner's configuration is its own
    again."""
    from agent_system.config import layers
    from agent_system.config.settings import load_settings

    llm = load_settings().llm_system
    ref = next(iter(llm.profiles.values())).model_ref
    jobs = []
    monkeypatch.setattr(runner, "run_bounded", lambda argv, **kw: jobs.append(
        json.loads(Path(argv[-1]).read_text(encoding="utf-8"))) or 0)
    path = _suite(tmp_path, f"""
        name: s
        agent: file_ops_test_agent
        repeats: 1
        config:
          llm_system: {{profiles: {{zz_eval_suite_profile: {{model_ref: {ref}}}}}}}
        arms:
          base: {{}}
          suite: {{agent_config: {{llm_profile: [zz_eval_suite_profile]}}}}
        tasks:
          - {{id: t1, input: x, check: {{expect: {{contains: [ok]}}}}}}
    """)
    run_id = runner.run_suite(path, progress=lambda _: None)
    expected = store.read_meta(run_id)["expected_models"]
    assert expected["suite"]["models"]["file_ops_test_agent"]["model"] == llm.models[ref].model
    assert [job["config"] for job in jobs] == [{"llm_system": {"profiles": {
        "zz_eval_suite_profile": {"model_ref": ref}}}}] * 2
    assert layers.process_layer == {} and "zz_eval_suite_profile" not in load_settings().llm_system.profiles


@pytest.mark.parametrize("section", ["paths", "auth", "includes"])
def test_a_suites_config_leaves_data_auth_and_includes_alone(tmp_path, section):
    from plugins.agent_eval.suite import load_suite
    path = _suite(tmp_path, f"""
        name: s
        agent: a
        config: {{{section}: {{}}}}
        tasks:
          - {{id: t1, input: x, check: {{expect: {{contains: [ok]}}}}}}
    """)
    with pytest.raises(ValueError, match=f"config: \\['{section}'\\] cannot be set by a suite"):
        load_suite(path)


def test_check_arms_refuses_a_task_overlay_that_breaks_an_arm(tmp_path):
    from plugins.agent_eval.suite import load_suite
    path = _suite(tmp_path, """
        name: s
        agent: file_ops_test_agent
        arms:
          base: {}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
          - {id: t2, input: x, check: {expect: {contains: [ok]}}, agent_config: {max_steps: lots}}
    """)
    with pytest.raises(ValueError) as refused:
        runner.check_arms(load_suite(path))
    assert "arm base, task t2" in str(refused.value) and "task t1" not in str(refused.value)


def test_check_arms_checks_the_profile_a_run_gets_from_task_and_arm(tmp_path):
    """A task's unknown llm_profile would crash every run of the task; an arm that names its own profile over
    it runs (the arm's overlay comes after the task's)."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    from plugins.agent_eval.suite import load_suite
    own = get_tool_server_config("file_ops_test_agent", load_settings()).agent_config.llm_profile
    own = own[0] if isinstance(own, list) else own
    path = _suite(tmp_path, f"""
        name: s
        agent: file_ops_test_agent
        arms:
          base: {{}}
          fixed: {{agent_config: {{llm_profile: [{own}]}}}}
        tasks:
          - {{id: t1, input: x, check: {{expect: {{contains: [ok]}}}}, agent_config: {{llm_profile: [no_such_profile]}}}}
    """)
    with pytest.raises(ValueError) as refused:
        runner.check_arms(load_suite(path))
    assert "arm base, task t1: unknown llm_profile" in str(refused.value), refused.value
    assert "arm fixed" not in str(refused.value), refused.value


def test_a_suites_overlay_paths_mean_its_own_directory(tmp_path):
    """'./' in an arm's or a task's system_template is the suite's directory, as in an agent YAML it is the
    YAML's -- not the run's workspace, where the file is not."""
    from plugins.agent_eval.suite import load_suite
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "old.md").write_text("old", encoding="utf-8")
    path = _suite(tmp_path, """
        name: s
        agent: a
        arms:
          base: {}
          old: {agent_config: {system_template: ./prompts/old.md}}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}, agent_config: {system_template: ./prompts/old.md}}
    """)
    suite = load_suite(path)
    expected = str((tmp_path / "prompts" / "old.md").resolve())
    assert suite.arms["old"].agent_config["system_template"] == expected
    assert suite.tasks[0].agent_config["system_template"] == expected
    path.write_text(path.read_text(encoding="utf-8").replace("old.md}}\ntasks", "gone.md}}\ntasks"), encoding="utf-8")
    with pytest.raises(ValueError, match="arm old: system_template .*gone.md does not exist"):
        load_suite(path)
    # Any other relative path is the project's, as in an agent YAML -- checked there.
    path.write_text(path.read_text(encoding="utf-8").replace("./prompts/gone.md", "config/no_such_prompt.md"),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="arm old: system_template config/no_such_prompt.md does not exist"):
        load_suite(path)


def test_a_run_gets_the_tasks_overlay_then_the_arms(tmp_path, monkeypatch):
    """The job names both, the task's first: the arm's change acts on the task's material."""
    from plugins.agent_eval.suite import load_suite
    path = _suite(tmp_path, """
        name: s
        agent: a
        arms:
          base: {}
          b: {agent_config: {template_vars: {tone: dry}}}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}, agent_config: {template_vars: {genre: kids}}}
    """)
    suite = load_suite(path)
    jdir = tmp_path / "job"
    jdir.mkdir()
    monkeypatch.setattr(runner, "run_bounded", lambda *a, **k: 0)
    runner._agent_run(suite, "20261028-120000-abcd", {"arm": "b", "rep": 0, "n": 1, "task": "t1"},
                      suite.tasks[0], jdir, tmp_path, tmp_path / "data")
    job = json.loads((jdir / "job-t1.json").read_text(encoding="utf-8"))
    assert job["overlays"] == [{"template_vars": {"genre": "kids"}}, {"template_vars": {"tone": "dry"}}]


def test_a_run_names_its_data_directory_and_the_machine_runs_it_started(tmp_path, monkeypatch):
    """What a check reads the products from: the run's data directory, and the stategraph runs begun or moved on
    in it during this agent run -- not one a task before it left there untouched (a chain keeps its data
    directory), but one it left waiting and this run drove on."""
    import time
    from plugins.agent_eval.suite import load_suite
    from plugins.stategraph.engine.journal import RunStore

    path = _suite(tmp_path, """
        name: s
        agent: a
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    suite = load_suite(path)
    jdir, data = tmp_path / "job", tmp_path / "rundata"
    jdir.mkdir()
    runs = RunStore(data / "stategraph" / "runs.db")
    runs.create_run("before", "m", {})
    runs.create_run("waiting", "m", {}, status="waiting")
    time.sleep(0.01)

    def child(args, **kwargs):
        runs.update_run("waiting", status="succeeded")
        runs.create_run("mine", "m", {}, status="succeeded")
        (jdir / "result-t1.json").write_text(json.dumps({"answer": "ok"}), encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "run_bounded", child)
    result = runner._agent_run(suite, "20261028-120000-abcd", {"arm": "base", "rep": 0, "n": 1, "task": "t1"},
                               suite.tasks[0], jdir, tmp_path, data)
    assert result["status"] == "done", result
    assert result["data_dir"] == str(data)
    assert [r["id"] for r in result["stategraph_runs"]] == ["waiting", "mine"], result["stategraph_runs"]
    assert {r["status"] for r in result["stategraph_runs"]} == {"succeeded"}
    assert result["stategraph_runs"][1]["caller_run"] is None


def test_a_shared_run_names_the_installations_data_directory_absolute(tmp_path, monkeypatch):
    """A check runs in the workspace: a relative data directory would point into it."""
    from agent_system.paths import PROJECT_ROOT, data_path
    from plugins.agent_eval.suite import load_suite

    monkeypatch.delenv(DATA_DIR_ENV)  # the installation's, as configured
    path = _suite(tmp_path, """
        name: s
        agent: a
        data: shared
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    suite = load_suite(path)
    jdir = tmp_path / "job"
    jdir.mkdir()
    monkeypatch.setattr(runner, "run_bounded", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "usage_of", lambda *a, **k: {})
    result = runner._agent_run(suite, "20261028-120000-abcd", {"arm": "base", "rep": 0, "n": 1, "task": "t1"},
                               suite.tasks[0], jdir, tmp_path, tmp_path / "data")
    assert Path(result["data_dir"]).is_absolute(), result["data_dir"]
    assert Path(result["data_dir"]) == PROJECT_ROOT / data_path()
    assert result["stategraph_runs"] is None


def test_usage_and_machine_runs_read_a_data_directory_whose_path_has_a_hash(tmp_path):
    """'#' ends a URI: read raw, the read-only open lost its mode, created a stray database and read nothing."""
    from plugins.stategraph.engine.journal import RunStore

    data = tmp_path / "a#b%20c"
    _usage_db(data, [("r1", 0.5)])
    RunStore(data / "stategraph" / "runs.db").create_run("one", "m", {})
    assert runner.usage_of("r1", data)["cost"] == 0.5
    assert [r["id"] for r in runner.stategraph_runs(data, "")] == ["one"]
    assert not (tmp_path / "a").exists(), "a stray database next to the data directory"


def test_choosing_arms_keeps_the_suites_order_and_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in())
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 1
        arms: {one: {}, two: {}, three: {}}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    meta = store.read_meta(runner.run_suite(path, arms=["three", "one"], progress=lambda _: None))
    assert meta["arms"] == ["one", "three"] and meta["reference_arm"] == "one"


def test_a_runner_error_in_one_run_does_not_end_the_suite(tmp_path, monkeypatch):
    calls = []

    def flaky(suite, run_id, job, task, jdir, workspace, data):
        calls.append(job["n"])
        if job["n"] == 1:
            raise OSError("file locked")
        return {"status": "done", "answer": "ok", "cost": 0.0}
    monkeypatch.setattr(runner, "_agent_run", flaky)
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 3
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    run_id = runner.run_suite(path, progress=lambda _: None)
    statuses = Counter(r["status"] for r in _records(run_id))
    assert statuses == {"error": 1, "done": 2} and store.read_meta(run_id)["status"] == "incomplete"
    (error,) = [r for r in _records(run_id) if r["status"] == "error"]
    assert not error["scored"] and "file locked" in error["detail"]


def test_a_setup_that_times_out_skips_only_its_task(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in())
    hang = f'"{sys.executable}" -c "import time; time.sleep(60)"'
    path = _suite(tmp_path, f"""
        name: s
        agent: a
        repeats: 1
        timeout_s: 2
        tasks:
          - {{id: t1, input: x, setup: {json.dumps(hang)}, check: {{expect: {{contains: [ok]}}}}}}
          - {{id: t2, input: x, check: {{expect: {{contains: [ok]}}}}}}
    """)
    records = {r["task"]: r for r in _records(runner.run_suite(path, progress=lambda _: None))}
    assert records["t1"]["status"] == "setup_failed" and records["t2"]["scored"]


def test_a_chain_runs_the_arms_side_by_side_task_by_task(tmp_path, monkeypatch):
    """With three workers, the next task of a chain starts only when every arm is done with the one before --
    one arm's slow first task holds the others' second, instead of them running ahead."""
    import threading
    import time
    spans, lock = {}, threading.Lock()

    def timed(suite, run_id, job, task, jdir, workspace, data):
        started = time.monotonic()
        time.sleep(0.6 if (job["arm"], task.id) == ("three", "t1") else 0.05)
        with lock:
            spans[(job["arm"], task.id)] = (started, time.monotonic())
        return {"status": "done", "answer": "ok", "cost": 0.0}
    monkeypatch.setattr(runner, "_agent_run", timed)
    path = _suite(tmp_path, """
        name: s
        agent: a
        chain: true
        repeats: 1
        parallel: 3
        arms: {one: {}, two: {}, three: {}}
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
          - {id: t2, input: x, check: {expect: {contains: [ok]}}}
    """)
    runner.run_suite(path, progress=lambda _: None)
    last_t1_end = max(end for (arm, task), (_, end) in spans.items() if task == "t1")
    first_t2_start = min(start for (arm, task), (start, _) in spans.items() if task == "t2")
    assert first_t2_start >= last_t1_end, spans


def test_usage_of_sums_the_run_and_its_sub_requests(tmp_path):
    data = tmp_path / "run-data"
    _usage_db(data, [("ev-1", 0.10), ("ev-1_sub_a", 0.05), ("ev-1_001", None), ("ev-10", 9.0), ("ev-1x", 9.0)])
    usage = runner.usage_of("ev-1", data)
    assert usage["calls"] == 3 and usage["cost"] == pytest.approx(0.15) and usage["unpriced_calls"] == 1
    assert usage["models"] is None, "a store without agent_name/model columns: its cost counts, its models are unknown"
    assert runner.usage_of("ev-1", tmp_path / "nothing")["cost"] is None


def test_usage_of_names_what_each_agent_really_ran_on(tmp_path):
    """A fallback in an agent's chain shows: the calls of the run and its sub-requests, per agent and model."""
    import sqlite3
    db = tmp_path / "context_usage_tracker" / "usage.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE usage_snapshots (request_id TEXT, agent_name TEXT, model TEXT, cost REAL, "
                     "prompt_tokens INTEGER, completion_tokens INTEGER)")
        conn.executemany("INSERT INTO usage_snapshots VALUES (?, ?, ?, 0.01, 10, 2)", [
            ("ev-1", "judge", "m-a"), ("ev-1_sg1_001", "judge", "m-a"), ("ev-1_sg1_002", "judge", "m-b"),
            ("ev-1_sg1_003", "writer", "m-c"), ("ev-2", "judge", "m-x")])
    assert runner.usage_of("ev-1", tmp_path)["models"] == {"judge": {"m-a": 2, "m-b": 1}, "writer": {"m-c": 1}}


def _expect(agent, **models):
    """expected_models' form for one arm: agent -> (model, advanced)."""
    return {"agent": agent, "models": {who: {"model": m, "advanced": a} for who, (m, a) in models.items()}}


def _scored(arm, models, model=None):
    return {"arm": arm, "task": "t", "kind": "learn", "scored": True, "score": 1.0, "passed": True, "cost": 0.01,
            "models": models, **({"model": model} if model else {})}


def test_a_report_names_an_agent_that_fell_back_from_the_model_its_arm_set(tmp_path, monkeypatch, capsys):
    """Expected per arm: the model each agent it names runs on first, and its advanced one. An agent that also ran
    on another fell back in its chain -- the run counted for the arm without measuring its choice; the report says
    so. Its advanced model is an escalation, as configured: said apart."""
    from plugins.agent_eval.__main__ import _print_report, build_report
    from plugins.agent_eval.stats import report

    records = [_scored("cand", {"judge": {"m-cand": 2, "m-ref": 1, "m-big": 1}, "writer": {"m-w": 3}}),
               _scored("ref", {"judge": {"m-ref": 3}})]
    expected = {"cand": _expect("a", judge=("m-cand", "m-big")), "ref": _expect("a", judge=("m-ref", None))}
    out = report(records, "ref", ["ref", "cand"], expected=expected)
    assert out["arms"]["cand"]["fallbacks"] == {"judge": ["m-ref"]}, out["arms"]["cand"]
    assert out["arms"]["cand"]["escalated"] == {"judge": ["m-big"]}, out["arms"]["cand"]
    assert out["arms"]["ref"]["fallbacks"] == {} and out["arms"]["ref"]["escalated"] == {}
    assert out["arms"]["cand"]["models"] == {"judge": {"m-cand": 2, "m-ref": 1, "m-big": 1}, "writer": {"m-w": 3}}
    assert report(records, "ref", ["ref", "cand"])["arms"]["cand"]["fallbacks"] == {}, "nothing expected"

    run_id = "20261028-230000-abcd"
    store.run_dir(run_id).mkdir(parents=True)
    store.write_meta(run_id, {"run_id": run_id, "suite": "s", "agent": "a", "arms": ["ref", "cand"],
                              "reference_arm": "ref", "status": "done", "expected_models": expected})
    for record in records:
        store.append_record(run_id, record)
    _print_report(build_report(run_id))
    printed = capsys.readouterr().out
    assert "WARNING: judge fell back to m-ref" in printed and "judge also ran on its advanced model m-big" in printed


def test_the_suite_agent_is_held_to_the_model_its_run_was_built_with():
    """A task's own llm_profile: the child's record names the model the agent was built with after it -- that is
    no fallback; one of another model still is."""
    from plugins.agent_eval.stats import report

    expected = {"base": _expect("me", me=("m-arm", None))}
    by_task = [_scored("base", {"me": {"m-task": 2}}, model="m-task")]
    assert report(by_task, "base", ["base"], expected=expected)["arms"]["base"]["fallbacks"] == {}
    fell = [_scored("base", {"me": {"m-task": 1, "m-other": 1}}, model="m-task")]
    assert report(fell, "base", ["base"], expected=expected)["arms"]["base"]["fallbacks"] == {"me": ["m-other"]}
    # timed out: no model in the record -- the task's own expectation holds it
    timed_out = [{**_scored("base", {"me": {"m-task": 3}}), "status": "timeout"}]
    with_task = {"base": {**expected["base"], "tasks": {"t": "m-task"}}}
    assert report(timed_out, "base", ["base"], expected=with_task)["arms"]["base"]["fallbacks"] == {}
    assert report(timed_out, "base", ["base"], expected=expected)["arms"]["base"]["fallbacks"] == {"me": ["m-task"]}


def test_comparing_against_a_baseline_warns_when_its_reference_fell_back():
    from plugins.agent_eval.__main__ import build_report

    for run_id, arms, models in [("20261027-100000-aaaa", ["ref"], {"j": {"m-1": 1, "m-2": 1}}),
                                 ("20261028-100000-bbbb", ["ref"], {"j": {"m-1": 2}})]:
        store.run_dir(run_id).mkdir(parents=True)
        store.write_meta(run_id, {"run_id": run_id, "suite": "s", "agent": "a", "arms": arms, "reference_arm": "ref",
                                  "status": "done", "expected_models": {"ref": _expect("a", j=("m-1", None))}})
        store.append_record(run_id, _scored("ref", models))
    rep = build_report("20261028-100000-bbbb", against="20261027-100000-aaaa")
    assert any("20261027-100000-aaaa" in w and "j fell back to m-2" in w for w in rep["warnings"]), rep["warnings"]


def test_expected_models_are_the_first_profile_of_each_named_agent_after_the_arm():
    """From the real configuration: the suite's agent with the arm's overlay, and every agent of its agents."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    from plugins.agent_eval.suite import Arm, Suite

    config = load_settings()
    llm = config.llm_system

    def model_of(profile):
        return llm.models[llm.profiles[profile].model_ref].model

    base = get_tool_server_config("file_ops_test_agent", config).agent_config.llm_profile
    base = base[0] if isinstance(base, list) else base
    other = next(name for name, p in llm.profiles.items() if model_of(name) != model_of(base))
    suite = Suite(name="s", agent="file_ops_test_agent", tasks=[{"id": "t", "input": "x",
                                                                 "check": {"expect": {"contains": ["x"]}}}],
                  arms={"base": Arm(), "swap": Arm(agent_config={"llm_profile": [other]},
                                                   agents={"stategraph_example_agent": {"llm_profile": [other]}})})
    swapped = Arm(agent_config={"llm_profile": [other]}, agents={"file_ops_test_agent": {"llm_profile": [base]}})
    suite.arms["named-too"] = swapped  # the suite's agent under agents as well: the arm's overlay goes over that
    # an advanced chain is an escalation's only where the agent escalates by itself; else it is one more fallback
    suite.arms["escalates"] = Arm(agent_config={"llm_profile_advanced": [other], "auto_escalate_on_stuck": True})
    suite.arms["advanced-only"] = Arm(agent_config={"llm_profile_advanced": [other]})
    suite.tasks.append(suite.tasks[0].model_copy(update={"id": "own", "agent_config": {"llm_profile": [other]}}))
    got = runner.expected_models(suite)
    assert got["escalates"]["models"]["file_ops_test_agent"]["advanced"] == model_of(other), got
    assert got["advanced-only"]["models"]["file_ops_test_agent"]["advanced"] is None, got
    assert got["base"]["tasks"] == {"own": model_of(other)} and got["swap"]["tasks"] == {}, got
    first = {name: runs["model"] for name, runs in got["base"]["models"].items()}
    assert (got["base"]["agent"], first) == ("file_ops_test_agent", {"file_ops_test_agent": model_of(base)}), got
    assert {name: runs["model"] for name, runs in got["swap"]["models"].items()} == {
        "file_ops_test_agent": model_of(other), "stategraph_example_agent": model_of(other)}, got
    assert got["named-too"]["models"]["file_ops_test_agent"]["model"] == model_of(other), got


def test_a_timed_out_run_is_charged_from_the_usage_store(tmp_path, monkeypatch):
    from plugins.agent_eval.suite import load_suite
    suite = load_suite(_suite(tmp_path, """
        name: s
        agent: a
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """))
    jdir, data = tmp_path / "job", tmp_path / "job" / "data"
    (data / "workspace").mkdir(parents=True)
    job = {"arm": "base", "rep": 0, "n": 7, "task": "t1"}
    request_id = "ev-" + "20261026120000abcd"[-12:] + "-7-t1"
    _usage_db(data, [(request_id, 0.25), (request_id + "_sub_x", 0.5)])
    monkeypatch.setattr(runner, "run_bounded", lambda *a, **k: None)
    result = runner._agent_run(suite, "20261026-120000-abcd", job, suite.tasks[0], jdir, data / "workspace", data)
    assert result["status"] == "timeout" and result["cost"] == pytest.approx(0.75), result


def test_a_run_that_raised_counts_as_crashed_with_its_reason(tmp_path, monkeypatch):
    from plugins.agent_eval.suite import load_suite
    suite = load_suite(_suite(tmp_path, """
        name: s
        agent: a
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """))
    jdir = tmp_path / "job"
    jdir.mkdir()

    def child_that_raised(args, **kwargs):
        (jdir / "result-t1.json").write_text(json.dumps(
            {"answer": "", "errors": [], "exception": "RuntimeError: provider down"}), encoding="utf-8")
        return 1
    monkeypatch.setattr(runner, "run_bounded", child_that_raised)
    result = runner._agent_run(suite, "20261026-120000-abcd", {"arm": "base", "rep": 0, "n": 1, "task": "t1"},
                               suite.tasks[0], jdir, tmp_path, tmp_path / "data")
    assert result["status"] == "crashed" and "provider down" in result["detail"], result


def test_child_records_a_run_that_raised(tmp_path, data_dir, monkeypatch):
    """The real child around a run that raises: the result says so instead of passing for a finished run."""
    from agent_system import agent_run
    from plugins.agent_eval import child

    async def raising(*args, **kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(agent_run, "run_agent_request", raising)
    env = runner.child_env(runner.load_suite(Path(runner.__file__).parent / "evals" / "hello" / "suite.yaml"), data_dir)
    for key in set(os.environ) - set(env):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    result_path = tmp_path / "result.json"
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"agent": "file_ops_test_agent", "overlays": [], "input": "hi",
                               "request_id": "ev-test-2", "session_user": runner.SESSION_USER,
                               "result_path": str(result_path)}), encoding="utf-8")
    with pytest.raises(SystemExit):
        child.main(str(job))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["exception"] == "RuntimeError: provider down", result


def test_cli_baseline_of_an_incomplete_run_still_reports_and_fails(tmp_path, monkeypatch, capsys):
    from plugins.agent_eval.__main__ import main
    monkeypatch.chdir(tmp_path)  # main enters the project directory; this puts it back afterwards
    monkeypatch.setattr(runner, "_agent_run", _stand_in(cost=1.0))
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 2
        budget_usd: 0.5
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    assert main(["run", str(path), "--baseline"]) == 1
    out, err = capsys.readouterr()
    assert "reference:" in out and "not made the baseline" in err and "incomplete" in err, (out, err)
    assert store.baselines() == {}


def test_a_run_keeps_its_arms_expected_models_for_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _stand_in())
    monkeypatch.setattr(runner, "expected_models", lambda suite: {"base": _expect("a", a=("m-1", None))})
    path = _suite(tmp_path, """
        name: s
        agent: a
        repeats: 1
        tasks:
          - {id: t1, input: x, check: {expect: {contains: [ok]}}}
    """)
    run_id = runner.run_suite(path)
    assert store.read_meta(run_id)["expected_models"] == {"base": _expect("a", a=("m-1", None))}


def test_check_arms_passes_every_suite_in_the_repository():
    """Every suite the repository carries can run against the shipped configuration: its arms' and its judge's
    overlays merged over their agents, with the suite's own config layer -- what tests/config's scan of
    agent_config blocks leaves to this check, an overlay being no agent_config of its own (`key: null` removes an
    inherited key)."""
    import yaml
    from plugins.agent_eval.suite import load_suite

    root = Path(runner.__file__).resolve().parents[2]       # src/
    found, refused = [], []
    for path in sorted(root.rglob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - not YAML this test reads
            continue
        if not (isinstance(data, dict) and isinstance(data.get("tasks"), list) and isinstance(data.get("agent"), str)):
            continue
        found.append(path)
        try:
            suite = load_suite(path)
            with runner.layered(suite):
                runner.check_arms(suite)
                runner.check_judge(suite, runner.expected_models(suite))
        except Exception as e:  # noqa: BLE001 - named with its file
            refused.append(f"{path.relative_to(root)}: {e}")
    assert any(p.parent.name == "hello" for p in found), "the scan found not even the shipped hello suite"
    assert not refused, "\n".join(refused)
