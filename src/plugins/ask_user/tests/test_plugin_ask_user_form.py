"""ask_user's question in the form every client draws (run_questions.Question.form), and its answer in the form
every client sends (QuestionBroker.take): what agent-cli chat shows and answers, as the web chat does."""
from __future__ import annotations

import pytest

from agent_system.core.run_questions import AnswerRejected, answer_question
from plugins.ask_user.questions import AskUserBroker, UserAnswer, UserQuestion


def _open(broker: AskUserBroker, options=(), multi_select=False) -> UserQuestion:
    return broker.open_question(UserQuestion, owner="alice", session_id="s", request_id="r_001", agent_name="a",
                                timeout=30, question="Which database?", options=tuple(options),
                                multi_select=multi_select)


async def test_the_options_and_an_answer_in_ones_own_words_are_the_form():
    broker = AskUserBroker()
    picked = _open(broker, ("Postgres", "SQLite"), multi_select=True)
    free = _open(broker)

    assert picked.to_public()["form"] == {
        "prompt": "Which database?", "detail": None, "warning": None,
        "choices": [{"value": "Postgres", "label": "Postgres"}, {"value": "SQLite", "label": "SQLite"}],
        "multi_select": True, "text": {"label": "Or answer in your own words", "alone": True}}
    assert free.form()["choices"] == [] and free.form()["text"] == {"label": "Your answer", "alone": True}


async def test_an_answer_in_the_common_form_goes_through_the_question_s_own_check():
    broker = AskUserBroker()
    question = _open(broker, ("Postgres", "SQLite"))

    with pytest.raises(AnswerRejected, match="takes one option"):
        answer_question(question.id, ["Postgres", "SQLite"], "", answered_by="alice")
    with pytest.raises(AnswerRejected, match="not one of the question's options"):
        answer_question(question.id, ["MySQL"], "", answered_by="alice")

    answer_question(question.id, ["SQLite"], " small ", answered_by="alice")

    assert question.answer.result() == UserAnswer(choices=("SQLite",), text="small", answered_by="alice")
