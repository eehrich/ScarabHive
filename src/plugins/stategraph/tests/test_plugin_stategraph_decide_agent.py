"""A decision point that asks an agent instead of the decision model.

`decide` asked the configured decision model only -- the name of one of them stood in the format. An agent can
judge where no decision model fits (a long text, a judgement the model was not calibrated for): `by:` names it,
checks its answer like a schema, and the machine reads the same out as from a decision model, without the
confidence and probabilities an agent cannot give.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from plugins.stategraph.engine.backend import make_config_check
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, FakeBackend, Harness, errors, found, validate

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


def machine(do: str, head: str = "") -> dict[str, str]:
    """State judge runs ``do`` and keeps its out; the run's output is that out."""
    return {"m.yaml": "stategraph: 1\nid: m\n" + textwrap.dedent(head)
            + "context: {verdict: null}\ninitial: judge\nstates:\n  judge:\n" + textwrap.indent(textwrap.dedent(do), "    ")
            + "    transitions:\n      - target: done\n        effect: ctx.verdict = out\n"
            + "  done: {type: final, output: \"{{ ctx.verdict }}\"}\n"}


CHOICE = """\
do:
  decide: choice
  by: critic
  question: Is the scene ready?
  criteria: {ready: nothing to change, revise: needs another pass}
  input: "The scene text."
"""

QUESTIONS = """\
do:
  decide: questions
  by: critic
  input: {text: "The scene text.", round: 2}
  questions:
    ready: {type: noul, question: Is it ready?}
    tension: {type: score, question: How tense is it?, criteria: [flat, taut, gripping]}
"""


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def test_an_agent_decides_a_choice_and_the_machine_reads_the_decisions_shape(harness):
    backend = FakeBackend({"judge": "Revising is best.\n```json\n{\"decision\": \"revise\"}\n```"})

    row = await harness.run(machine(CHOICE), backend=backend)

    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == {"value": "revise", "confidence": None, "probabilities": None}
    [call] = backend.calls
    assert call["kind"] == "agent_create" and call["agent"] == "critic", "no decision model asked"
    assert '"ready": nothing to change' in call["task"] and '"revise": needs another pass' in call["task"]
    assert call["task"].endswith("Content:\nThe scene text.")


async def test_an_unusable_answer_goes_back_to_the_same_agent_once(harness):
    backend = FakeBackend({"judge": lambda call: (
        '{"ready": 0.8, "tension": "medium"}' if call["kind"] == "agent_create"
        else '{"ready": 0.8, "tension": "taut"}')})

    row = await harness.run(machine(QUESTIONS), backend=backend)

    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == {
        "ready": {"value": 0.8, "confidence": None, "probabilities": None},
        "tension": {"value": 1, "confidence": None, "probabilities": None}}, "a score is its position, as a model's"
    first, feedback = backend.calls
    assert '"flat", "taut", "gripping"' in first["task"] and '"round": 2' in first["task"]
    assert feedback["kind"] == "agent_continue" and feedback["instance_id"] == "inst-judge"
    assert "tension" in feedback["message"], feedback["message"]


async def test_an_answer_still_unusable_fails_the_decision(harness):
    backend = FakeBackend({"judge": '{"decision": "maybe"}'})

    row = await harness.run(machine(CHOICE), backend=backend)

    assert row["status"] == "failed"
    assert "schema_invalid" in json.dumps(row["error"]), row["error"]


@pytest.mark.parametrize("extra, message", [
    ("  profile: jev\n", "a decision profile or an agent (by:), not both"),
    ("", "a literal or {{ params.<name> }}"),
])
def test_the_agent_is_named_plainly_and_alone(extra, message):
    do = CHOICE.replace("by: critic", "by: \"{{ ctx.who }}\"" if not extra else "by: critic") + extra

    tree = validate(machine(do))

    assert any(message in p.message for p in errors(tree)), [p.as_dict() for p in tree.problems]


def test_the_agent_takes_the_agent_activitys_configuration_check():
    """Not configured, or one that reaches machines: refused as an agent activity naming it is (SG007)."""
    from plugins.stategraph.tests.test_plugin_stategraph_review_format import system_config

    check = make_config_check(system_config(), runner="stategraph_runner", own_instance="stategraph")
    unknown = validate(machine(CHOICE.replace("critic", "no_such_agent")), config_check=check)
    author = validate(machine(CHOICE.replace("critic", "stategraph_author")), config_check=check)
    by_param = validate(machine(CHOICE.replace("critic", "\"{{ params.who }}\""),
                                head="params: {who: {type: string}}\n"), config_check=check)

    assert found(unknown, "SG007") and found(author, "SG007"), ([p.as_dict() for p in unknown.problems],
                                                                [p.as_dict() for p in author.problems])
    assert found(by_param, "SG005"), [p.as_dict() for p in by_param.problems]


@pytest.mark.parametrize("do", [
    CHOICE.replace("criteria: {ready: nothing to change, revise: needs another pass}", 'criteria: "{{ [1, 2] }}"'),
    CHOICE.replace('input: "The scene text."', 'input: "{{ ctx.verdict }}"'),  # null before judge ran
])
async def test_content_the_machine_got_wrong_fails_as_a_template_every_time(harness, do):
    """Criteria rendered into the wrong shape and empty content fail the same on every try: template_failed, which
    neither a retry nor the same request again repeats -- decision_failed is a model's or provider's failure."""
    backend = FakeBackend({"judge": '{"decision": "ready"}'})

    row = await harness.run(machine(do), backend=backend)

    assert row["status"] == "failed" and row["error"]["type"] == "template_failed", row["error"]
    assert backend.calls == [], "the agent was asked about content the machine got wrong"
