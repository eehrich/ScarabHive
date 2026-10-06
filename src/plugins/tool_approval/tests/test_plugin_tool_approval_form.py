"""tool_approval's question in the form every client draws (run_questions.Question.form), and its answer in the form
every client sends (QuestionBroker.take): what agent-cli chat shows and answers, as the web chat does."""
from __future__ import annotations

import pytest

from agent_system.core.run_questions import AnswerRejected, answer_question
from plugins.tool_approval.broker import ALLOW_ONCE, DENY, CUT_NOTE, Answer, ApprovalBroker


def _open(broker: ApprovalBroker, **fields):
    return broker.open(owner="alice", session_id="s", request_id="r", agent_name="a", tool="write_file",
                       server="file_ops", arguments_preview='{"path": "a.txt"}', timeout=30, **fields)


async def test_the_call_its_decisions_and_a_reason_for_deny_are_the_form():
    broker = ApprovalBroker()
    plain = _open(broker)
    script = _open(broker, arguments_cut=True, warning="It runs without approvals.", decisions=(ALLOW_ONCE, DENY))

    assert plain.to_public()["form"] == {
        "prompt": "Approve write_file?", "detail": '{"path": "a.txt"}', "warning": None,
        "choices": [{"value": "allow_once", "label": "Allow once"},
                    {"value": "allow_session", "label": "Allow for this session"},
                    {"value": "deny", "label": "Deny"}],
        "multi_select": False, "text": {"label": "Why not (sent to the agent with Deny)", "alone": False}}
    form = script.form()
    assert [c["value"] for c in form["choices"]] == ["allow_once", "deny"], "a decision not offered is drawn"
    assert form["warning"] == f"It runs without approvals.\n{CUT_NOTE}"


async def test_an_answer_in_the_common_form_is_one_decision_and_its_reason():
    broker = ApprovalBroker()
    question = _open(broker, decisions=(ALLOW_ONCE, DENY))

    with pytest.raises(AnswerRejected, match="pick one decision"):
        answer_question(question.id, [], "no", answered_by="alice")
    with pytest.raises(AnswerRejected, match="pick one decision"):
        answer_question(question.id, [ALLOW_ONCE, DENY], "", answered_by="alice")
    with pytest.raises(AnswerRejected, match="this question takes"):
        answer_question(question.id, ["allow_session"], "", answered_by="alice")
    # "1 more thing: do not touch prod" typed at the terminal reads as Allow with words: it allows nothing
    with pytest.raises(AnswerRejected, match="a reason goes with Deny only"):
        answer_question(question.id, [ALLOW_ONCE], "more thing: do not touch prod", answered_by="alice")
    assert not question.answer.done()

    answer_question(question.id, [DENY], " too wide ", answered_by="alice")

    assert question.answer.result() == Answer(decision=DENY, reason="too wide", answered_by="alice")
