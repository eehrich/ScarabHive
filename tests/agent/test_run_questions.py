"""core.run_questions: an answer that took the question is the outcome.

The route answers "ok" once ``resolve`` has taken the question out of the
broker. A route served on another loop than the run's hands the answer over a
moment later (call_soon_threadsafe); if the wait ends in that moment -- its
timeout, a message in the chat -- the answer must still be the outcome, not
dropped behind an "ok" the person was shown.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from dataclasses import dataclass

from agent_system.core.request_context import release_run_attended, set_run_attended
from agent_system.core.run_questions import (
    AnswerRejected,
    Question,
    QuestionBroker,
    answer_question,
    put_to_person,
    waiting_question,
)
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder

STOPPED = "stopped by the asker"


@dataclass
class _Probe(Question):
    """A question with the least a kind needs: a form any client draws."""

    def form(self):
        return {"prompt": "Which?", "detail": None, "warning": None,
                "choices": [{"value": "pg", "label": "Postgres"}], "multi_select": False, "text": None}


class _ProbeBroker(QuestionBroker):
    """Takes an answer in the form any client sends: what it picked."""

    def take(self, question_id, choices, text, answered_by=None):
        if list(choices) != ["pg"]:
            raise AnswerRejected(422, "pick Postgres")
        return self.resolve(question_id, (tuple(choices), text, answered_by))


class _Row:
    """A status row that records its lines."""

    def __init__(self):
        self.lines = []

    async def progress(self, message, meta=None):
        self.lines.append(("progress", message))

    async def error(self, message, meta=None):
        self.lines.append(("error", message))


async def _ask(broker, interrupt, row=None):
    run = "inflight1"
    set_run_attended(run, True)
    stream = StatusEventForwarder()
    await stream.start_forwarding(run)
    question = broker.open_question(_Probe, owner="alice", session_id="s", request_id=f"{run}_001",
                                    agent_name="a", timeout=30)
    try:
        return question, await put_to_person(
            broker, question, row or _Row(), meta_key="probe", answer_url="/plugins/probe/answer", line="Which?",
            token=None, reask_seconds=10, gone_after_seconds=10, cut_off="cut off", interrupt=interrupt)
    finally:
        await stream.stop_forwarding()
        release_run_attended(run)


async def test_an_answer_handed_over_from_another_thread_is_the_outcome():
    broker = _ProbeBroker()
    taken = []

    def answered_elsewhere_then_interrupted():
        # the route, on a loop of its own, takes the question just before the wait ends
        [question] = broker.pending()
        worker = threading.Thread(target=lambda: taken.append(broker.resolve(question.id, "Postgres")))
        worker.start()
        worker.join()
        return STOPPED

    question, outcome = await _ask(broker, answered_elsewhere_then_interrupted)

    assert taken == [question], "fixture: the route did not take the question"
    assert outcome == "Postgres", "the person was told 'ok', and the answer was dropped"
    assert broker.pending() == []


async def test_without_an_answer_the_wait_keeps_its_own_outcome():
    broker = _ProbeBroker()

    question, outcome = await _ask(broker, lambda: STOPPED)

    assert outcome == STOPPED
    assert broker.pending() == [], "the question was still open while its caller settled"
    assert not question.answer.done(), "an answer nobody gave"


async def test_a_cancel_while_the_answer_is_handed_over_ends_the_row_as_cut_off():
    """The route took the question from another thread; the wait is over and
    awaits the handed-over answer -- and the run is torn down right then. The
    row ends with the asker's cut-off line, not StatusScope's generic failure."""
    broker = _ProbeBroker()
    row = _Row()

    def taken_elsewhere_then_torn_down():
        [question] = broker.pending()
        loop = asyncio.get_running_loop()
        loop.call_soon(asyncio.current_task().cancel)   # before the handover below runs
        worker = threading.Thread(target=lambda: broker.resolve(question.id, "Postgres"))
        worker.start()
        worker.join()
        return STOPPED

    with pytest.raises(asyncio.CancelledError):
        await _ask(broker, taken_elsewhere_then_torn_down, row)

    assert row.lines[-1] == ("error", "cut off"), row.lines
    assert broker.pending() == []


class _Row_with_meta(_Row):
    async def progress(self, message, meta=None):
        self.lines.append(("progress", message, meta))


async def test_a_question_goes_on_its_row_with_the_form_any_client_draws():
    """The row carries the question under its kind with ``form``: the web chat
    and agent-cli chat draw the same question from it."""
    broker = _ProbeBroker()
    row = _Row_with_meta()

    await _ask(broker, lambda: STOPPED, row)

    [(_, line, meta)] = [entry for entry in row.lines if entry[0] == "progress"][:1]
    shown = meta["probe"]
    assert line == "Which?" and shown["answer_url"] == "/plugins/probe/answer", row.lines
    assert shown["form"] == _Probe.form(None), shown


@pytest.mark.parametrize("kind, broker_type", [(Question, _ProbeBroker), (_Probe, QuestionBroker)],
                         ids=["no form", "no take"])
def test_a_kind_one_client_could_not_draw_or_answer_fails_before_anyone_is_asked(kind, broker_type):
    broker = broker_type()

    async def ask():
        return broker.open_question(kind, owner=None, session_id="s", request_id="r", agent_name="a", timeout=5)

    with pytest.raises(NotImplementedError, match="needs Question.form and its broker QuestionBroker.take"):
        asyncio.run(ask())
    assert broker.pending() == [], "a question nobody could answer was opened"


async def test_an_answer_finds_the_broker_its_question_waits_in():
    """agent-cli chat answers in the run's own process: by the question's id
    alone, whichever asker waits for it -- through the kind's own check."""
    brokers = [_ProbeBroker(), _ProbeBroker()]
    questions = [broker.open_question(_Probe, owner="alice", session_id="s", request_id="r", agent_name="a",
                                      timeout=5) for broker in brokers]
    question = questions[1]

    assert waiting_question(question.id) is question
    with pytest.raises(AnswerRejected) as refused:
        answer_question(question.id, ["mysql"], "", answered_by="alice")
    assert refused.value.status == 422 and waiting_question(question.id) is question, "a refused answer took it"

    # each broker holds one: whichever the process looks at first, a wrong one answers neither
    for each in questions:
        assert answer_question(each.id, ["pg"], "fast", answered_by="alice") is each
        assert each.answer.result() == (("pg",), "fast", "alice")
    assert [broker.pending() for broker in brokers] == [[], []]
    with pytest.raises(AnswerRejected) as again:
        answer_question(question.id, ["pg"], "", answered_by="alice")
    assert again.value.status == 404, "an answered question was answered twice"
