"""The check of the suite coder-workflow-ab (evals/workflow_ab/checks.py) scores what it claims to.

* the templates fail as shipped: there is something to do
* every hidden test is collected -- a hidden file that does not even import scores as one failed test, and a
  suite over it measures nothing
* a module the run never wrote costs all of its tests -- a run that wrote nothing scores below one that began,
  and the count is the task's (checks.TESTS) whatever imports
* a run that changed a test of the template, or added a conftest.py, scores 0
* a conftest.py above the workspace (the checkout's, under the runs' data directory) takes no part: each test
  here has one that breaks every test it reaches
"""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from plugins.coder.evals.workflow_ab import checks


def _score(tmp_path, task_id, template, change=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "conftest.py").write_text("raise RuntimeError('a conftest above the workspace was loaded')\n",
                                          encoding="utf-8")
    workspace = tmp_path / "workspace"
    shutil.copytree(checks.HERE / template, workspace)
    if change:
        change(workspace)
    return checks.score(workspace, {"stategraph_runs": [{"id": "sg_one"}]},
                        SimpleNamespace(id=task_id, workspace=template))


def test_the_bug_template_fails_and_every_hidden_test_is_collected(tmp_path):
    result = _score(tmp_path, "bugs", "ledger_bugs")
    assert result["passed"] is False
    assert result["detail"].splitlines()[0] == "tests/ 1/3, hidden 0/7; workflow runs: 1"


def _stub_currency(workspace):
    (workspace / "ledger" / "currency.py").write_text(
        "def convert(*args, **kwargs):\n    raise NotImplementedError\n", encoding="utf-8")


def test_a_module_the_run_never_wrote_costs_all_of_its_tests(tmp_path):
    untouched = _score(tmp_path / "a", "currencies", "ledger_feature")
    # tests/test_ledger.py passes as shipped; tests/test_currency.py and the hidden file cannot import yet
    assert untouched["detail"].splitlines()[0] == "tests/ 3/7, hidden 0/14; workflow runs: 1", untouched["detail"]
    begun = _score(tmp_path / "b", "currencies", "ledger_feature", _stub_currency)
    assert untouched["of"] == begun["of"] and untouched["score"] < begun["score"] < 1, (untouched, begun)


def test_the_count_of_each_task_is_what_pytest_collects_once_everything_imports(tmp_path):
    for task_id, template, change in (("currencies", "ledger_feature", _stub_currency), ("bugs", "ledger_bugs", None)):
        workspace = tmp_path / task_id
        shutil.copytree(checks.HERE / template, workspace)
        if change:
            change(workspace)
        shutil.copytree(checks.HERE / checks.HIDDEN[task_id], workspace / "_hidden_tests")
        counted = tuple(checks._pytest(workspace, [part], part)[0][1] for part in ("tests", "_hidden_tests"))
        assert counted == checks.TESTS[task_id], task_id


@pytest.mark.parametrize("path, text", [("tests/test_ledger.py", "def test_x():\n    pass\n"),
                                        ("ledger/conftest.py", "")])
def test_a_run_that_changed_a_test_scores_nothing(tmp_path, path, text):
    def rewrite(workspace):
        (workspace / path).write_text(text, encoding="utf-8")

    result = _score(tmp_path, "bugs", "ledger_bugs", rewrite)
    assert (result["score"], result["passed"]) == (0.0, False)
    assert path in result["detail"]
