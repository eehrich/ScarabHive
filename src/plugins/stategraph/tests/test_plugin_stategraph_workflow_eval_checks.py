"""The check of the workflow-authoring suite scores what it claims: validity, subset, agents and shape."""
from __future__ import annotations

from pathlib import Path

from plugins.stategraph.evals.workflow_authoring import checks

GOOD = '''Here is the workflow.

```yaml
stategraph: 1
id: review_changes
title: Review a diff, verify every finding
params:
  diff: {type: string, required: true}
  lenses: {type: array, default: ["correctness", "security"]}
limits: {max_output_tokens: 200000, concurrency: 4}
context: {confirmed: []}
initial: review
states:
  review:
    do:
      map: params.lenses
      as: lens
      each:
        agent: coder_reviewer
        task: "Review for {{ lens }}:\\n{{ params.diff }}"
        schema: {type: object, required: [findings], properties: {findings: {type: array}}}
    transitions:
      - target: verify
        effect: 'ctx.confirmed = [f for r in out for f in r["findings"]]'
  verify:
    do:
      map: ctx.confirmed
      as: finding
      each:
        map: range(3)
        as: skeptic
        fail: collect
        each:
          agent: coder_explorer
          task: "Refute: {{ finding }}"
          schema: {type: object, required: [refuted], properties: {refuted: {type: boolean}}}
    transitions:
      - target: done
        effect: |
          ctx.confirmed = [f for f, votes in zip(ctx.confirmed, out)
                           if sum(1 for v in votes if v["status"] == "succeeded" and not v["out"]["refuted"]) >= 2]
  done: {type: final, output: {confirmed: "{{ ctx.confirmed }}"}}
```
'''

SUITE = Path(checks.__file__).with_name("suite.yaml")


def task(task_id: str = "diff-review"):
    """The suite's own Task, as agent_eval hands it to the check -- an object, not a dict."""
    from plugins.agent_eval.suite import load_suite

    return next(t for t in load_suite(SUITE).tasks if t.id == task_id)


def score(answer: str, task_id: str = "diff-review") -> dict:
    return checks.score(None, {"answer": answer}, task(task_id))


def test_every_task_of_the_suite_has_its_expectations():
    from plugins.agent_eval.suite import load_suite

    assert {t.id for t in load_suite(SUITE).tasks} == set(checks.TASKS)


def test_a_block_left_open_at_the_end_is_still_the_machine():
    unclosed = GOOD.rstrip().removesuffix("```")
    assert unclosed != GOOD.rstrip(), "fixture: the closing fence was not removed"
    assert score(unclosed)["passed"]


def test_a_valid_workflow_in_the_subset_with_the_shape_meets_every_criterion():
    result = score(GOOD)
    assert result["passed"], result
    assert result["score"] == 1.0
    assert result["of"] == 7, result   # parses, validates, max_output_tokens, subset, agents, fan_out, two_stage


def test_a_workflow_without_a_cost_cap_misses_that_criterion_only():
    result = score(GOOD.replace("limits: {max_output_tokens: 200000, concurrency: 4}", "limits: {concurrency: 4}"))
    assert not result["passed"]
    assert "missing: max_output_tokens" in result["detail"], result
    assert result["score"] == 6 / 7


def test_an_agent_the_task_did_not_allow_is_caught():
    result = score(GOOD.replace("agent: coder_explorer", "agent: sysadmin_agent"))
    assert "agents" in result["detail"].split(";")[0], result
    assert "sysadmin_agent" in result["detail"]


def test_a_companion_module_is_outside_the_subset():
    answer = GOOD.replace("limits:", "python: helpers.py\nlimits:")
    result = score(answer)
    assert "subset" in result["detail"].split(";")[0], result


def test_a_call_activity_inside_a_map_is_outside_the_subset():
    spec = {"states": {"s": {"do": {"map": "x", "each": {"call": "work"}}}}}
    assert "call" in {k for a in checks._activities(spec) for k in checks.FORBIDDEN_KINDS if k in a}
    answer = GOOD.replace('''        each:
          agent: coder_explorer
          task: "Refute: {{ finding }}"
          schema: {type: object, required: [refuted], properties: {refuted: {type: boolean}}}''',
                          '''        each: {call: refute}''')
    assert answer != GOOD, "fixture replacement did not apply"
    result = score(answer)
    assert "outside the subset: ['call']" in result["detail"], result


def test_a_machine_that_does_not_validate_scores_validates_false():
    result = score(GOOD.replace("target: verify", "target: nowhere"))
    assert "validates" in result["detail"].split(";")[0], result


def test_no_machine_at_all_scores_nothing_but_the_agents_shape_criteria_fail_too():
    result = score("I could not do it.")
    assert result["score"] == 0.0, result


def test_a_pipeline_needs_a_map_whose_item_is_a_submachine():
    one_stage = checks.shapes({"states": {"s": {"do": {"map": "x", "each": {"agent": "a", "task": "t"}}}}})
    staged = checks.shapes({"states": {"s": {"do": {"map": "x", "each": {"machine": "one"}}}}})
    assert not one_stage["pipeline"] and staged["pipeline"]


def test_a_loop_needs_max_visits_and_a_transition_back():
    looping = {"states": {"find": {"max_visits": 5, "do": {"agent": "a", "task": "t"},
                                   "transitions": [{"target": "find", "guard": "x"}, {"target": "done"}]}}}
    unbounded = {"states": {"find": {"do": {"agent": "a", "task": "t"},
                                     "transitions": [{"target": "find"}]}}}
    assert checks.shapes(looping)["loop"] and not checks.shapes(unbounded)["loop"]
