"""The judge: after all runs, per task one call over every arm's products, shuffled and unnamed; a yes or no per
criterion, the share of yes the run's score. Agent runs and the judge call are stand-ins here -- the judge one
answers from what it is asked, so a product mapped back to the wrong run would score wrong."""
from __future__ import annotations

import json
import os
import re
import textwrap
from pathlib import Path

import pytest

from agent_system.paths import DATA_DIR_ENV
from plugins.agent_eval import runner, store
from plugins.agent_eval.stats import families, length_pairs


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch, request):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setattr(runner, "check_arms", lambda suite: None)  # agent 'a' is no agent of any configuration
    if "check_judge" not in request.node.name:
        monkeypatch.setattr(runner, "check_judge", lambda suite, expected: None)
    return tmp_path / "data"


SUITE = """
name: s
agent: a
repeats: {repeats}
arms: {{alpha: {{}}, beta: {{}}}}
judge:
  agent: j
  instructions: Short greetings.
  criteria: {{warm: Is it warm?, short: Is it short?}}
  {product}
tasks:
  - {{id: t1, input: greet someone, check: {{judge: true}}}}
  - {{id: t2, input: greet someone else, check: {{judge: [warm]}}}}
  - {{id: t3, input: plain, check: {{expect: {{contains: [x]}}}}}}
"""


def _suite(tmp_path: Path, repeats: int = 2, product: str = "") -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(textwrap.dedent(SUITE.format(repeats=repeats, product=product)), encoding="utf-8")
    return path


def _agents(answers):
    """Agent runs that answer *answers[arm]* (a function of the repeat)."""
    def run(suite, run_id, job, task, jdir, workspace, data):
        return {"status": "done", "answer": answers[job["arm"]](job["rep"]), "cost": 0.01, "request_id": "r"}
    return run


def _judge(asked, verdict=lambda text, criterion: criterion == "warm" and "warm" in text, answer=None):
    """The judge call: answers per product from its text alone, what it was asked kept in *asked*."""
    real = runner._child

    def child(suite, job, jdir, name, workspace, data, timeout):
        if not name.startswith("judge"):
            return real(suite, job, jdir, name, workspace, data, timeout)
        (jdir / f"job-{name}.json").write_text(json.dumps(job), encoding="utf-8")  # as the real child's call does
        then = store.read_meta(jdir.parents[1].name)  # the meta on disk while the judge works
        asked.append({**job, "meta_then": then})
        products = dict(re.findall(r"=== (P\d+) ===\n(.*?)\n=== end of \1 ===", job["input"], re.S))
        criteria = re.findall(r"^- ([\w-]+): ", job["input"], re.M)
        said = {label: {c: verdict(text, c) for c in criteria} for label, text in products.items()}
        return {"status": "done", "answer": answer(said) if answer else "thinking...\n" + json.dumps(said),
                "cost": 0.5, "estimated_cost": 0.2, "request_id": job["request_id"], "models": {"j": {"m-judge": 1}}}
    return child


def test_the_judge_scores_each_run_from_its_own_product(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: "a warm hello", "beta": lambda rep: "hi"}))
    monkeypatch.setattr(runner, "_child", _judge(asked))
    run_id = runner.run_suite(_suite(tmp_path), progress=lambda _: None)
    records = store.read_records(run_id)
    judged = {(r["arm"], r["task"]): r for r in records if r["task"] != "t3"}
    # t1 by both criteria (short never holds here), t2 by warm alone
    assert {k: r["score"] for k, r in judged.items()} == {
        ("alpha", "t1"): 0.5, ("beta", "t1"): 0.0, ("alpha", "t2"): 1.0, ("beta", "t2"): 0.0}, judged
    assert all(r["scored"] and r["judge"] == "done" for r in judged.values())
    assert judged[("alpha", "t1")]["of"] == 2 and judged[("alpha", "t1")]["detail"] == "warm: yes, short: no"
    assert judged[("alpha", "t2")]["verdicts"] == {"warm": True} and judged[("alpha", "t2")]["passed"]
    # one call per judged task, every arm's every repeat in it, under labels that do not name the arm
    assert [len(re.findall(r"^=== P\d+ ===$", job["input"], re.M)) for job in asked] == [4, 4]
    assert not any(arm in job["input"] for job in asked for arm in ("alpha", "beta")), asked[0]["input"]
    assert "greet someone else" in asked[1]["input"] and "Short greetings." in asked[1]["input"]
    assert "short" not in asked[1]["input"].split("Criteria:")[1].split("===")[0]
    assert store.read_meta(run_id)["status"] == "done"
    assert [c["judged"] for c in store.read_meta(run_id)["judge"]["calls"]] == [4, 4]


def test_the_judge_sees_the_products_shuffled(tmp_path, monkeypatch):
    """In the order the runs ran, the arms would alternate in the judge's list -- a position it could read."""
    asked = []
    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: f"warm {rep}", "beta": lambda rep: f"b {rep}"}))
    monkeypatch.setattr(runner, "_child", _judge(asked))
    run_id = runner.run_suite(_suite(tmp_path, repeats=4), progress=lambda _: None)
    by_n = {r["n"]: r for r in store.read_records(run_id)}
    shown = [re.findall(r"=== P\d+ ===\n(.*?)\n", job["input"]) for job in asked]
    ran = [[by_n[n]["answer"] for n in sorted(by_n) if by_n[n]["task"] == task] for task in ("t1", "t2")]
    assert sorted(shown[0]) == sorted(ran[0]) and shown != ran, (shown, ran)


def test_a_product_the_judge_leaves_out_is_not_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: "warm", "beta": lambda rep: "cold"}))

    def drop_one(said):  # the first product lacks a criterion, the second is gone, the rest answered as strings
        labels = sorted(said, key=lambda p: int(p[1:]))
        said[labels[0]].popitem()
        del said[labels[1]]
        return json.dumps(said)
    monkeypatch.setattr(runner, "_child", _judge([], answer=drop_one))
    run_id = runner.run_suite(_suite(tmp_path), progress=lambda _: None)
    records = [r for r in store.read_records(run_id) if r["task"] == "t1"]
    failed = [r for r in records if r.get("status") == "judge_failed"]
    assert len(failed) == 2 and all(not r["scored"] for r in failed), records
    assert "the judge did not judge it" in failed[0]["detail"]
    assert store.read_meta(run_id)["status"] == "incomplete"


def test_a_judge_call_that_fails_leaves_its_tasks_runs_uncounted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: "warm", "beta": lambda rep: "cold"}))
    real = runner._child

    def timeout(suite, job, jdir, name, workspace, data, t):
        if not name.startswith("judge"):
            return real(suite, job, jdir, name, workspace, data, t)
        return {"status": "timeout", "detail": "timed out after 900s", "request_id": job["request_id"]}
    monkeypatch.setattr(runner, "_child", timeout)
    run_id = runner.run_suite(_suite(tmp_path), progress=lambda _: None)
    judged = [r for r in store.read_records(run_id) if r["task"] != "t3"]
    assert {r["status"] for r in judged} == {"judge_failed"} and "timed out after 900s" in judged[0]["detail"]
    from plugins.agent_eval.stats import report
    arm = report(store.read_records(run_id), "alpha", ["alpha"])["arms"]["alpha"]
    assert arm["cost"] == 0.06 and arm["runs"] == 2, arm  # unjudged, but the four runs of t1 and t2 ran


def test_a_judge_call_that_raises_fails_its_runs_and_judge_does_them_again(tmp_path, monkeypatch, capsys):
    from plugins.agent_eval.__main__ import build_report, main

    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: "warm", "beta": lambda rep: "cold"}))
    asked = []
    works = _judge(asked)

    def broken(suite, job, jdir, name, workspace, data, t):
        if name.startswith("judge") and "greet someone else" not in job["input"]:
            raise ValueError("a broken result")
        return works(suite, job, jdir, name, workspace, data, t)
    monkeypatch.setattr(runner, "_child", broken)
    path = _suite(tmp_path)
    run_id = runner.run_suite(path, progress=lambda _: None)
    by_task = {}
    for r in store.read_records(run_id):
        by_task.setdefault(r["task"], set()).add(r.get("status"))
    assert by_task == {"t1": {"judge_failed"}, "t2": {"done"}, "t3": {"done"}}, by_task  # t2 judged all the same
    assert store.read_meta(run_id)["status"] == "incomplete"
    # a runner that died while judging leaves runs pending: the report counts them -- once it is no longer running
    store.append_judgment(run_id, {"n": next(r["n"] for r in store.read_records(run_id) if r["task"] == "t2"),
                                   "judge": "pending", "status": "done", "scored": False})
    assert build_report(run_id)["skipped"]["judge_pending"] == 1
    meta = store.read_meta(run_id)
    store.write_meta(run_id, {**meta, "status": "running"})  # its own pid: alive
    assert "judge_pending" not in build_report(run_id)["skipped"]
    assert main(["judge", run_id]) == 1 and "still running" in capsys.readouterr().err
    # the runner died: its meta says running forever; a run from before expected_models were kept, and of one arm
    # (--arm): the suite's other arm never ran, its agents are no candidates
    store.write_meta(run_id, {**{k: v for k, v in meta.items() if k != "expected_models"}, "status": "running",
                              "pid": 2 ** 22 + 7, "arms": ["alpha"], "repeats": 4})
    checked = []
    monkeypatch.setattr(runner, "check_judge", lambda suite, expected: checked.append(set(expected)))
    monkeypatch.setattr(runner, "_child", works)
    assert main(["judge", run_id]) == 0
    assert checked == [{"alpha"}], checked
    statuses = {r["status"] for r in store.read_records(run_id)}
    assert statuses == {"done"} and store.read_meta(run_id)["status"] == "done", statuses
    assert len(store.read_meta(run_id)["judge"]["calls"]) == 4  # two then, two now (t1 again, t2's one left)
    # t1's first call broke before it started a child: nothing ran under judge1, so its call now is judge1; t2's
    # second call has a request id of its own -- the usage store would count the first one's again
    assert [(job["request_id"].split("-")[-2], job["request_id"].split("-")[-1]) for job in asked] == [
        ("judge1", "t2"), ("judge1", "t1"), ("judge2", "t2")], asked
    # a judge again is refused while this one judges: running, under this process
    assert (asked[-1]["meta_then"]["status"], asked[-1]["meta_then"]["pid"]) == ("running", os.getpid())
    # a run that ended before every run had its record is not done, judged again or not
    store.write_meta(run_id, {**store.read_meta(run_id), "arms": ["alpha", "beta"], "repeats": 3})
    assert main(["judge", run_id]) == 0 and store.read_meta(run_id)["status"] == "incomplete"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert main(["judge", run_id]) == 1 and "changed since" in capsys.readouterr().err


def test_a_runner_that_dies_while_judging_keeps_the_calls_it_made(tmp_path, monkeypatch):
    """Each call is in the meta once made: what it cost survives the runner, and the next call of a task -- the
    one it died in -- gets a request id of its own, though the meta never heard of it."""
    from plugins.agent_eval.__main__ import main

    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: "warm", "beta": lambda rep: "cold"}))
    asked = []
    works = _judge(asked)

    def dies(suite, job, jdir, name, workspace, data, t):
        result = works(suite, job, jdir, name, workspace, data, t)
        if "greet someone else" in job["input"]:
            raise KeyboardInterrupt  # the runner is stopped in t2's call, after it started
        return result
    monkeypatch.setattr(runner, "_child", dies)
    with pytest.raises(KeyboardInterrupt):
        runner.run_suite(_suite(tmp_path), progress=lambda _: None)
    run_id = store.list_runs()[0]["run_id"]
    assert [c["task"] for c in store.read_meta(run_id)["judge"]["calls"]] == ["t1"]
    # on disk while t2's call ran -- a runner killed outright keeps it too, not only one that got to its finally
    assert [c["task"] for c in asked[-1]["meta_then"]["judge"]["calls"]] == ["t1"]
    monkeypatch.setattr(runner, "_child", works)
    assert main(["judge", run_id]) == 0
    assert [job["request_id"].split("-")[-2] for job in asked] == ["judge1", "judge1", "judge2"], asked
    assert [c["task"] for c in store.read_meta(run_id)["judge"]["calls"]] == ["t1", "t2"]


def test_a_childs_answer_file_from_before_is_never_its_answer(tmp_path, monkeypatch):
    """A judge called again writes its answer where the earlier call did: a child that died before writing one
    would else hand on the earlier answer -- verdicts on other products under the same labels."""
    from plugins.agent_eval.suite import load_suite

    suite = load_suite(_suite(tmp_path))
    jdir = tmp_path / "j"
    jdir.mkdir()
    (jdir / "result-judge1.json").write_text(json.dumps({"answer": "{}"}), encoding="utf-8")
    monkeypatch.setattr(runner, "run_bounded", lambda *a, **k: 1)  # died before it wrote anything
    monkeypatch.setattr(runner, "usage_of", lambda *a, **k: {})
    result = runner._child(suite, {"request_id": "r", "agent": "j"}, jdir, "judge1", tmp_path, tmp_path, 10)
    assert result["status"] == "crashed", result


def test_a_product_script_gives_what_is_judged_and_a_failing_one_scores_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")  # a console of another code page: read back as UTF-8 all the same
    (tmp_path / "product.py").write_text(textwrap.dedent("""
        import json, sys
        run = json.load(open(sys.argv[2], encoding="utf-8"))
        print("noise", file=sys.stderr)
        if "cold" in run["answer"]:
            sys.exit(3)
        print("warm, from the store: " + run["answer"] + " → Grüße")
    """), encoding="utf-8")
    asked = []
    monkeypatch.setattr(runner, "_agent_run", _agents({"alpha": lambda rep: "x", "beta": lambda rep: "cold"}))
    monkeypatch.setattr(runner, "_child", _judge(asked))
    run_id = runner.run_suite(_suite(tmp_path, product="product: {script: product.py}"), progress=lambda _: None)
    records = [r for r in store.read_records(run_id) if r["task"] == "t1"]
    beta = [r for r in records if r["arm"] == "beta"]
    assert all(r["scored"] and r["score"] == 0.0 and "product failed" in r["detail"] and "noise" in r["detail"]
               for r in beta), beta
    assert all("warm, from the store: x → Grüße" in job["input"] and "noise" not in job["input"] for job in asked)
    assert len(re.findall(r"^=== P\d+ ===$", asked[0]["input"], re.M)) == 2  # only alpha's left a product


def test_a_judge_check_needs_the_suites_judge_and_its_criteria(tmp_path):
    from plugins.agent_eval.suite import load_suite

    path = tmp_path / "suite.yaml"
    path.write_text("name: s\nagent: a\ntasks: [{id: t, input: x, check: {judge: true}}]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="needs the suite's judge"):
        load_suite(path)
    path.write_text("name: s\nagent: a\njudge: {agent: j, criteria: {warm: w}}\n"
                    "tasks: [{id: t, input: x, check: {judge: [cold]}}]\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"no criteria \['cold'\]"):
        load_suite(path)
    path.write_text("name: s\nagent: a\njudge: {agent: j, criteria: {P2: w}}\n"
                    "tasks: [{id: t, input: x, check: {judge: true}}]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="names a product"):
        load_suite(path)


def test_parse_verdicts_takes_the_last_answer_and_only_true_or_false():
    labels, criteria = ["P1", "P2"], ["a", "b"]
    answer = 'draft: {"P1": {"a": false, "b": false}} final: {"P1": {"a": true, "b": false}, "P2": {"a": "yes", "b": true}}'
    said, problem = runner.parse_verdicts(answer, labels, criteria)
    assert said == {"P1": {"a": True, "b": False}} and "P2" in problem
    assert runner.parse_verdicts("no json", labels, criteria)[0] == {}


def test_families_and_the_length_pairs():
    assert families("~anthropic/claude-sonnet-latest") & families("claude-opus-5-5")
    assert not families("~deepseek/deepseek-v4-flash-latest") & families("gemini-3.8-flash")
    # 40 and 30 score alike: that pair counts neither way
    runs = [{"task": "t", "judge": "done", "product_chars": n, "score": s}
            for n, s in ((10, 0.0), (20, 0.5), (30, 1.0), (40, 1.0))]
    runs.append({"task": "u", "judge": "done", "product_chars": 5, "score": 1.0})  # alone in its task: no pair
    assert length_pairs(runs) == {"longer_higher": 5, "longer_lower": 0, "p": 0.0625, "mean_chars": 21}
    assert length_pairs(runs[-1:]) is None  # repeats: 1 -- nothing was compared, nothing to report


def test_the_report_warns_of_a_judge_that_follows_length_or_shares_a_family(tmp_path, monkeypatch):
    from plugins.agent_eval.__main__ import build_report

    lengths = {"alpha": lambda rep: "warm" + "." * (10 * rep + 1), "beta": lambda rep: "b" + "." * (10 * rep + 1)}
    monkeypatch.setattr(runner, "_agent_run", _agents(lengths))
    monkeypatch.setattr(runner, "_child", _judge([], verdict=lambda text, c: len(text) > 25 if c == "warm" else True))
    run_id = runner.run_suite(_suite(tmp_path, repeats=6), progress=lambda _: None)
    rep = build_report(run_id)
    assert rep["arms"]["alpha"]["length"]["longer_higher"] > 0 == rep["arms"]["alpha"]["length"]["longer_lower"]
    assert any("may be judging length" in w for w in rep["warnings"]), rep["warnings"]
    assert rep["judge"]["calls"] == 2 and rep["judge"]["cost"] == 1.0
    assert rep["judge"]["cost_estimated"] == pytest.approx(0.4), "the judge's estimate went unmarked"
    records = store.read_records(run_id)
    store.append_record(run_id, {**records[0], "n": 999, "models": {"a": {"m-judge-2": 1}}})
    assert any("of the family 'm'" in w for w in build_report(run_id)["warnings"])


def test_check_judge_refuses_a_judge_of_a_candidates_family():
    from agent_system.config.settings import get_tool_server_config, load_settings
    from plugins.agent_eval.suite import Judge, Suite

    config = load_settings()
    agent = "file_ops_test_agent"
    own = get_tool_server_config(agent, config).agent_config.llm_profile
    own = own[0] if isinstance(own, list) else own
    profiles, models = config.llm_system.profiles, config.llm_system.models
    model = models[profiles[own].model_ref].model
    other = next(name for name, p in profiles.items() if not families(models[p.model_ref].model) & families(model))
    suite = Suite(name="s", agent=agent, tasks=[{"id": "t", "input": "x", "check": {"judge": True}}],
                  judge=Judge(agent=agent, criteria={"c": "?"}))
    expected = {"base": {"agent": agent, "models": {agent: {"model": model}}}}
    with pytest.raises(ValueError, match="favours its own family"):
        runner.check_judge(suite, expected)
    suite.judge.agent_config = {"llm_profile": [other]}
    runner.check_judge(suite, expected)
    escalates = {"base": {"agent": agent, "models": {agent: {"model": model,
                                                           "advanced": models[profiles[other].model_ref].model}}}}
    with pytest.raises(ValueError, match="favours its own family"):  # it escalates onto the judge's family
        runner.check_judge(suite, escalates)
    suite.judge.agent = "no-such-agent"
    with pytest.raises(ValueError, match="not an agent"):
        runner.check_judge(suite, expected)
