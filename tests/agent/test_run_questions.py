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

from agent_system.core.request_context import release_run_attended, set_run_attended
from agent_system.core.run_questions import Question, QuestionBroker, put_to_person

STOPPED = "stopped by the asker"
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder


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
    question = broker.open_question(Question, owner="alice", session_id="s", request_id=f"{run}_001",
                                    agent_name="a", timeout=30)
    try:
        return question, await put_to_person(
            broker, question, row or _Row(), meta_key="probe", answer_url="/plugins/probe/answer", line="Which?",
            token=None, reask_seconds=10, gone_after_seconds=10, cut_off="cut off", interrupt=interrupt)
    finally:
        await stream.stop_forwarding()
        release_run_attended(run)


async def test_an_answer_handed_over_from_another_thread_is_the_outcome():
    broker = QuestionBroker()
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
    broker = QuestionBroker()

    question, outcome = await _ask(broker, lambda: STOPPED)

    assert outcome == STOPPED
    assert broker.pending() == [], "the question was still open while its caller settled"
    assert not question.answer.done(), "an answer nobody gave"


async def test_a_cancel_while_the_answer_is_handed_over_ends_the_row_as_cut_off():
    """The route took the question from another thread; the wait is over and
    awaits the handed-over answer -- and the run is torn down right then. The
    row ends with the asker's cut-off line, not StatusScope's generic failure."""
    broker = QuestionBroker()
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
