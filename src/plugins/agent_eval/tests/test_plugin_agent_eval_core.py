"""agent_eval without agents: the suite format, overlays, the statistics and the checks."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_system.config.models import AgentConfig
from plugins.agent_eval.checks import run_check
from plugins.agent_eval.overlay import apply_overlay, machine_overlay_problems
from plugins.agent_eval.stats import compare, needed, regressions, report, sign_test, t_interval, task_means
from plugins.agent_eval.suite import discover_suites, load_suite

PLUGIN = Path(__file__).resolve().parents[1]


def _suite(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestSuite:
    def test_the_shipped_example_loads_and_is_discovered(self):
        suite = load_suite(PLUGIN / "evals" / "hello" / "suite.yaml")
        assert [t.id for t in suite.tasks] == ["greet", "capital"]
        assert suite.reference_arm == "base"
        found = discover_suites([str(PLUGIN.parent)], PLUGIN.parents[2])
        assert PLUGIN / "evals" / "hello" / "suite.yaml" in found

    @pytest.mark.parametrize("tasks, error", [
        ("- {id: a, input: x, check: {command: 'true'}}\n- {id: a, input: y, check: {command: 'true'}}",
         "duplicate task ids"),
        ("- {id: a, input: x, check: {command: 'true', pytest: tests}}", "exactly one"),
        ("- {id: a, input: x, check: {}}", "exactly one"),
        ("- {id: a, input: x, workspace: nowhere, check: {command: 'true'}}", "does not exist"),
        ("- {id: 'a b', input: x, check: {command: 'true'}}", "letters, digits"),
    ])
    def test_a_broken_suite_is_refused_with_its_reason(self, tmp_path, tasks, error):
        path = _suite(tmp_path, "name: s\nagent: a\ntasks:\n" + textwrap.indent(tasks, "  "))
        with pytest.raises(ValueError, match=error):
            load_suite(path)


class TestOverlay:
    BASE = AgentConfig(llm_profile="normal", tools={"allowed": ["file_ops/*", "terminal/*"], "blocked": ["terminal/kill"]},
                       llm_params={"*": {"thinking_level": "medium", "max_tokens": 8000}})

    def test_admin_overlay_merges_like_inheritance(self):
        merged = apply_overlay(self.BASE, {"llm_profile": "think", "tools": {"allowed": ["+web/*", "!terminal/*"]}})
        assert merged.llm_profile == "think"
        assert merged.tools.allowed == ["file_ops/*", "web/*"]
        assert self.BASE.llm_profile == "normal", "the base must stay untouched"

    def test_null_in_an_admin_overlay_removes_what_the_agent_has(self):
        """An agent whose llm_params name its profile: an arm on another profile must take them away, else the
        merged config names a profile its chain lacks and does not validate."""
        base = AgentConfig(llm_profile=["normal"], llm_params={"normal": {"max_tokens": 1000}})
        with pytest.raises(ValueError, match="appear in no LLM chain"):
            apply_overlay(base, {"llm_profile": ["think"]})
        merged = apply_overlay(base, {"llm_profile": ["think"], "llm_params": {"normal": None},
                                      "tools": {"blocked": None}})
        assert merged.llm_profile == ["think"] and "normal" not in (merged.llm_params or {}), merged.llm_params
        assert base.llm_params == {"normal": {"max_tokens": 1000}}, "the base must stay untouched"
        # where the agent has nothing, a null is a value, as in an agent YAML: the provider's default
        bare = apply_overlay(AgentConfig(llm_profile=["normal"]), {"llm_params": {"*": {"max_tokens": None}}})
        assert bare.llm_params == {"*": {"max_tokens": None}}, bare.llm_params

    def test_machine_overlay_may_narrow(self):
        assert machine_overlay_problems(self.BASE, {
            "system_prompt": "Be brief.", "template_vars": {"tone": "dry"},
            "tools": {"allowed": ["!terminal/*"], "blocked": ["+file_ops/delete"]},
            "llm_params": {"*": {"temperature": 0.2, "thinking_level": "low", "max_tokens": 4000}},
        }) == []

    @pytest.mark.parametrize("overlay, problem", [
        ({"hooks": {"enabled": False}}, "'hooks' is not open"),
        ({"system_template": "config/secrets.env"}, "'system_template' is not open"),
        ({"llm_profile": "expensive"}, "'llm_profile' is not open"),
        ({"tools": {"allowed": ["+ssh_control/*"]}}, "tools.allowed gains"),
        ({"tools": {"blocked": ["!terminal/kill"]}}, "tools.blocked loses"),
        ({"tools": {"blocked": None}}, "tools.blocked loses"),  # null removes -- for an admin only
        ({"llm_params": {"*": None}}, "llm_params.*: a mapping"),
        ({"llm_params": None}, "llm_params: a mapping"),
        ({"tools": None}, "only 'allowed' and 'blocked'"),
        ({"template_vars": None}, "strings only"),
        ({"tools": {"deferred": ["x"]}}, "only 'allowed' and 'blocked'"),
        ({"template_vars": {"n": 3}}, "strings only"),
        ({"llm_params": {"*": {"thinking_level": "high"}}}, "thinking_level: at most"),
        ({"llm_params": {"*": {"max_tokens": 9000}}}, "max_tokens: at most"),
        ({"llm_params": {"*": {"top_p": 0.5}}}, "not open to a machine overlay"),
    ])
    def test_machine_overlay_may_not_widen(self, overlay, problem):
        problems = machine_overlay_problems(self.BASE, overlay)
        assert any(problem in p for p in problems), problems


def _records(arm: str, scores: dict, runs: int = 3, kind: str = "learn"):
    return [{"arm": arm, "task": t, "kind": kind, "scored": True, "score": s, "passed": s >= 1.0, "cost": 0.01}
            for t, s in scores.items() for _ in range(runs)]


class TestStats:
    def test_sign_test_is_exact(self):
        assert sign_test(9, 1) == pytest.approx(0.0215, abs=1e-4)
        assert sign_test(8, 2) == pytest.approx(0.1094, abs=1e-4)
        assert sign_test(0, 0) == 1.0

    def test_better_worse_and_no_difference(self):
        ref = task_means(_records("a", {f"t{i}": 0.0 for i in range(10)}), "a")
        better = task_means(_records("b", {f"t{i}": 1.0 for i in range(10)}), "b")
        assert compare(ref, better)["verdict"] == "better"
        assert compare(better, ref)["verdict"] == "worse"
        mixed = task_means(_records("c", {f"t{i}": float(i % 2) for i in range(10)}), "c")
        result = compare(ref, mixed)
        assert result["verdict"] == "no detectable difference" and result["better_tasks"] == 5

    def test_the_direction_is_the_counts_not_the_means(self):
        """Nine tasks a little better, one much worse: the mean is negative, the count says better."""
        ref = task_means(_records("a", {f"t{i}": 0.5 for i in range(10)}), "a")
        arm = task_means(_records("b", {**{f"t{i}": 0.51 for i in range(9)}, "t9": 0.0}), "b")
        result = compare(ref, arm)
        assert result["mean_diff"] < 0 and result["better_tasks"] == 9 and result["verdict"] == "better", result

    def test_how_many_tasks_it_takes_to_decide(self):
        assert [needed(k) for k in (5, 6, 8, 10, 15, 20)] == [None, 6, 8, 9, 12, 15]
        ref = task_means(_records("a", {f"t{i}": 0.0 for i in range(5)}), "a")
        all_better = task_means(_records("b", {f"t{i}": 1.0 for i in range(5)}), "b")
        assert compare(ref, all_better)["verdict"] == "not decidable with so few tasks"

    def test_ties_do_not_count_against_a_decision(self):
        """Six of ten better, four tied: the sign test decides, and the report says six of six moved."""
        ref = task_means(_records("a", {f"t{i}": 0.0 for i in range(10)}), "a")
        arm = task_means(_records("b", {f"t{i}": 1.0 if i < 6 else 0.0 for i in range(10)}), "b")
        result = compare(ref, arm)
        assert result["verdict"] == "better" and result["moved"] == 6 and result["needed"] == 6, result

    def test_the_t_quantile_beyond_the_table(self):
        values = [float(i % 2) for i in range(41)]  # df 40, t(0.975) = 2.0211
        low, high = t_interval(values)
        import statistics
        half = (high - low) / 2
        assert half == pytest.approx(2.0211 * statistics.stdev(values) / len(values) ** 0.5, rel=1e-4)

    def test_the_interval_is_students_t_and_absent_without_spread(self):
        low, high = t_interval([0.0, 1.0])
        assert low == pytest.approx(0.5 - 12.706 * 0.5) and high == pytest.approx(0.5 + 12.706 * 0.5)
        same = task_means(_records("a", {"t1": 1.0, "t2": 1.0}), "a")
        assert compare(same, same)["ci95"] is None

    def test_cost_counts_unknown_runs_apart_and_p90_is_the_nearest_rank(self):
        records = [{"arm": "a", "task": f"t{i}", "kind": "learn", "scored": True, "score": 1.0, "passed": True,
                    "cost": float(i)} for i in range(1, 11)]
        records.append({"arm": "a", "task": "t11", "kind": "learn", "scored": True, "score": 0.0, "cost": None})
        arm = report(records, "a", ["a"])["arms"]["a"]
        assert arm["cost"] == 55.0 and arm["cost_unknown_runs"] == 1 and arm["cost_p90"] == 9.0, arm

    def test_no_common_tasks_is_told_from_no_tasks_of_the_kind(self):
        ref = task_means(_records("a", {"t1": 1.0}), "a")
        other = task_means(_records("b", {"t2": 1.0}), "b")
        result = compare(ref, other, ("learn",))
        assert result["verdict"] == "no common tasks" and result["reference_tasks"] == 1
        assert compare(ref, other, ("holdout",))["reference_tasks"] == 0

    def test_unscored_runs_do_not_count_but_failed_ones_do(self):
        records = _records("a", {"t1": 1.0}, runs=2) + [
            {"arm": "a", "task": "t1", "kind": "learn", "scored": True, "score": 0.0, "passed": False},
            {"arm": "a", "task": "t1", "kind": "learn", "scored": False, "status": "budget"},
        ]
        assert task_means(records, "a")["t1"]["runs"] == 3

    def test_regression_task_that_breaks_is_named(self):
        ref = task_means(_records("a", {"keep": 1.0}, kind="regression"), "a")
        broken = task_means(_records("b", {"keep": 0.0}, kind="regression"), "b")
        assert regressions(ref, broken) == ["keep"]
        assert regressions(ref, ref) == []

    def test_holdout_is_judged_apart_from_learn(self):
        records = (_records("a", {"l1": 0.0, "l2": 0.0}) + _records("a", {"h1": 0.0, "h2": 0.0}, kind="holdout")
                   + _records("b", {"l1": 1.0, "l2": 1.0}) + _records("b", {"h1": 0.0, "h2": 0.0}, kind="holdout"))
        arm = report(records, "a", ["a", "b"])["arms"]["b"]
        assert arm["learn"]["mean_diff"] == 1.0 and arm["holdout"]["mean_diff"] == 0.0


class TestChecks:
    def _task_suite(self, tmp_path: Path, check: str) -> tuple:
        template = tmp_path / "template"
        (template / "tests").mkdir(parents=True)
        (template / "tests" / "test_m.py").write_text(
            "from m import f\n\ndef test_one():\n    assert f() == 1\n\ndef test_two():\n    assert f() == 2\n",
            encoding="utf-8")
        suite = load_suite(_suite(tmp_path, f"""
            name: s
            agent: a
            tasks:
              - id: t
                input: x
                workspace: template
                check: {check}
        """))
        workspace = tmp_path / "run" / "workspace"
        import shutil
        shutil.copytree(template, workspace)
        return suite, suite.tasks[0], workspace

    def test_pytest_scores_the_share_of_green_tests(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        verdict = run_check(task.check, suite, task, ws, {})
        assert verdict["score"] == 0.5 and not verdict["passed"], verdict

    def test_pytest_ignores_a_conftest_above_the_workspace(self, tmp_path):
        """Runs live under the data directory, i.e. below the repository: its conftest must not take part."""
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        for above in (ws.parent, tmp_path):
            (above / "conftest.py").write_text(
                "def pytest_collection_modifyitems(items):\n    items.clear()\n", encoding="utf-8")
        assert run_check(task.check, suite, task, ws, {})["score"] == 0.5

    def test_pytest_refuses_changed_tests(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (ws / "tests" / "test_m.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
        verdict = run_check(task.check, suite, task, ws, {})
        assert verdict["score"] == 0.0 and "test files changed" in verdict["detail"], verdict

    def test_pytest_refuses_a_planted_conftest(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (ws / "conftest.py").write_text("", encoding="utf-8")
        verdict = run_check(task.check, suite, task, ws, {})
        assert "conftest.py" in verdict["detail"] and verdict["score"] == 0.0, verdict

    def test_expect_scores_the_share_met(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{expect: {contains: [Paris], regex: ['^\\\\d+$']}}")
        assert run_check(task.check, suite, task, ws, {"answer": "Paris"})["score"] == 0.5
        assert run_check(task.check, suite, task, ws, {"answer": "Lyon"})["detail"].startswith("0/2")

    def test_python_check_and_a_broken_one(self, tmp_path, monkeypatch):
        (tmp_path / "evalcheck_mod.py").write_text(
            "def half(workspace, run, task):\n    return {'score': 0.5, 'detail': run['answer'] + task.id}\n",
            encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        suite, task, ws = self._task_suite(tmp_path, "{python: 'evalcheck_mod:half'}")
        verdict = run_check(task.check, suite, task, ws, {"answer": "x-"})
        assert verdict == {"score": 0.5, "passed": False, "detail": "x-t"}, verdict
        suite, task, ws = self._task_suite(tmp_path / "broken", "{python: 'builtins:print'}")
        verdict = run_check(task.check, suite, task, ws, {})
        assert verdict["score"] == 0.0 and "must return a dict" in verdict["detail"], verdict

    def test_a_score_that_is_no_number_fails_the_check(self, tmp_path, monkeypatch):
        (tmp_path / "evalcheck_none.py").write_text("def f(workspace, run, task):\n    return {'score': None}\n",
                                                    encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        suite, task, ws = self._task_suite(tmp_path, "{python: 'evalcheck_none:f'}")
        verdict = run_check(task.check, suite, task, ws, {})
        assert verdict["score"] == 0.0 and "check failed: TypeError" in verdict["detail"], verdict

    def test_pytest_on_the_whole_workspace_lets_the_run_change_the_code(self, tmp_path):
        """`pytest: .` covers the code under test too: the run fixes it, only test material is guarded."""
        suite, task, ws = self._task_suite(tmp_path, "{pytest: .}")
        for root in (tmp_path / "template", ws):
            (root / "m.py").write_text("def f():\n    return 0\n", encoding="utf-8")
            (root / "tests" / "expected.txt").write_text("1\n", encoding="utf-8")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        assert run_check(task.check, suite, task, ws, {})["score"] == 0.5
        (ws / "tests" / "expected.txt").write_text("0\n", encoding="utf-8")
        verdict = run_check(task.check, suite, task, ws, {})
        assert verdict["score"] == 0.0 and "tests/expected.txt" in verdict["detail"], verdict

    def test_pytest_is_not_fooled_by_a_planted_pytest_module(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (ws / "pytest.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        assert run_check(task.check, suite, task, ws, {})["score"] == 0.5

    def test_pytest_collects_only_the_templates_test_files(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (ws / "tests" / "test_zz.py").write_text(
            "".join(f"def test_{i}():\n    pass\n" for i in range(98)), encoding="utf-8")
        assert run_check(task.check, suite, task, ws, {})["score"] == 0.5

    def test_pytest_never_reads_a_report_from_before(self, tmp_path):
        suite, task, ws = self._task_suite(tmp_path, "{pytest: tests}")
        (ws.parent / "pytest-report.xml").write_text(
            '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>', encoding="utf-8")
        (ws / "m.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
        verdict = run_check(task.check, suite, task, ws, {})
        assert verdict["score"] == 0.0 and "no report" in verdict["detail"], verdict

    def test_a_check_past_its_timeout_ends_with_what_it_started(self, tmp_path):
        """A command that leaves a process behind holding its output: the timeout must still end the check."""
        import sys
        import time
        import psutil
        script = tmp_path / "hang.py"
        pid_file = tmp_path / "sleeper.pid"
        script.write_text("import subprocess, sys, time\n"
                          "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                          f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
                          "time.sleep(60)\n", encoding="utf-8")
        suite, task, ws = self._task_suite(tmp_path, f'{{command: \'"{sys.executable}" "{script}"\', timeout_s: 3}}')
        started = time.monotonic()
        verdict = run_check(task.check, suite, task, ws, {})
        assert time.monotonic() - started < 20 and "timed out" in verdict["detail"], verdict
        sleeper = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while psutil.pid_exists(sleeper) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not psutil.pid_exists(sleeper), "the process the check left behind still runs"
