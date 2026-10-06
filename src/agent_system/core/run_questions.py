"""Questions a run puts to the person watching it, and the wait for the answer.

Two things ask that person: tool_approval's pre_tool_call hook (may this call
run?) and the ask_user tool (what the model wants to know). Both use what is
here:

* ``QuestionBroker`` -- the open questions by id. The asker opens one and
  waits on its ``answer`` future; the answer route (``api.question_routes``)
  resolves it. One broker per plugin instance, in the process that runs the
  agents. Nothing is persisted: a restart ends every run that waits.
* ``put_to_person`` -- the question as a status line of the run, with
  ``meta[<kind>]`` naming it for the chat (which draws the answer box on that
  row), sent again every ``reask_seconds`` while it waits: until the answer,
  the run's cancellation, the timeout, until nobody reads the run any more,
  or until a check of the asker's own (``interrupt``) names an outcome -- ask_user
  ends when the person writes into the run instead.
  The question is closed when it returns -- an answer that comes later is
  refused, never taken for a question already settled -- and a wait cut off
  from outside ends the row before the cancellation goes on.
* ``status_line`` -- one row of the status stream.

Who can be asked at all: ``status_forwarding.attended_stream_of`` -- a run
whose client shows its questions to the person who started it (the web chat
and ``agent-cli chat`` say so, ``request_context.set_run_attended``), or a run
above it, while a tab reads it.

A question describes itself in one form any client can draw
(``Question.form``), and takes an answer in one form any client can send
(``QuestionBroker.take``): ``agent-cli chat`` draws every kind from it and
hands the answer to ``answer_question`` in its own process. The web chat does
not yet: it draws the two kinds it knows (ask_user, tool_approval) with boxes
of their own and posts to their own routes -- a new kind needs a box there.
"""
from __future__ import annotations

import asyncio
import secrets
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Type, TypeVar

#: A status row is cut at this width (tests/plugins/test_status_end_lines.py).
STATUS_WIDTH = 140
#: How often a token without ``wait_for_cancellation`` is looked at, in seconds.
CANCEL_POLL_SECONDS = 0.25
#: How often a waiting question looks whether anybody still reads the run, in seconds.
READER_POLL_SECONDS = 1.0

#: What a wait ends with when no answer came.
TIMEOUT = "timeout"
CANCELLED = "cancelled"
GONE = "gone"

#: Said to whoever answers a question that is no longer waiting.
NOT_WAITING = "No such question is waiting -- it was answered, timed out or its run ended."


def status_line(text: str) -> str:
    """One status row: a single line within STATUS_WIDTH."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= STATUS_WIDTH else flat[:STATUS_WIDTH - 1] + "…"


class AnswerRejected(Exception):
    """The answer cannot be taken; ``status`` is the HTTP status that says why."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Question:
    """One question waiting for a person. A kind of question adds its own
    fields in a subclass and shows them in ``to_public``."""

    id: str
    #: The user the run belongs to: who may answer, besides an admin.
    owner: Optional[str]
    session_id: str
    #: The id of the status stream line the question belongs to (a run's, or a call's).
    request_id: str
    agent_name: str
    asked_at: float
    timeout: float
    answer: "asyncio.Future[Any]" = field(repr=False)

    def to_public(self) -> Dict[str, Any]:
        """What a client shows about the question: ``form`` for any client."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "agent": self.agent_name,
            "asked_at": self.asked_at,
            "expires_at": self.asked_at + self.timeout,
            "form": self.form(),
        }

    def form(self) -> Dict[str, Any]:
        """The question as any client draws it, and what it takes back (``QuestionBroker.take``):

        ``prompt`` what is asked; ``detail`` text shown as it is (a call's arguments), or None;
        ``warning`` what answering gives up, or None; ``choices`` ``[{"value", "label"}]`` to pick
        from (``value`` is what the answer names); ``multi_select`` whether several may be picked;
        ``text`` ``{"label", "alone"}`` when the person may write something -- ``alone``: the
        text answers without a choice -- or None.

        Every kind of question has one: a question only one client can draw is one the
        person at the other cannot answer.
        """
        raise NotImplementedError(f"{type(self).__name__} has no form: no client could draw it")


Q = TypeVar("Q", bound=Question)


def _resolve(future: "asyncio.Future[Any]", answer: Any) -> None:
    if not future.done():
        future.set_result(answer)


#: Every broker in the process: ``answer_question`` finds the one a question waits in.
_brokers: "weakref.WeakSet[QuestionBroker]" = weakref.WeakSet()


class QuestionBroker:
    """The open questions of one asker, by id."""

    def __init__(self) -> None:
        self._questions: Dict[str, Question] = {}
        _brokers.add(self)

    def open_question(self, question_type: Type[Q], **fields: Any) -> Q:
        """A new question whose answer the caller awaits on ``question.answer``.

        A kind without a form or a broker without ``take`` fails here, before
        anyone is asked: one client could show it, the other not answer it."""
        if question_type.form is Question.form or type(self).take is QuestionBroker.take:
            raise NotImplementedError(f"{question_type.__name__} asked through {type(self).__name__}: "
                                      "a question needs Question.form and its broker QuestionBroker.take")
        question = question_type(
            # hex: the id can end up in a status line's request id, where a `_` or a
            # trailing `_nnn` would read as a level of the run tree
            id=secrets.token_hex(8), asked_at=time.time(),
            answer=asyncio.get_running_loop().create_future(), **fields)
        self._questions[question.id] = question
        return question

    def close(self, question: Question) -> None:
        """The asker stopped waiting: the question can no longer be answered."""
        self._questions.pop(question.id, None)
        if not question.answer.done():
            question.answer.cancel()

    def withdraw(self, question: Question) -> bool:
        """Take ``question`` out of reach of answers. False when an answer took
        it first (``resolve`` removed it) -- that answer was told it was taken."""
        return self._questions.pop(question.id, None) is not None

    def get(self, question_id: str) -> Optional[Question]:
        return self._questions.get(question_id)

    def pending(self) -> List[Question]:
        """Every open question, oldest first."""
        return sorted(self._questions.values(), key=lambda q: q.asked_at)

    def take(self, question_id: str, choices: Sequence[str], text: str,
             answered_by: Optional[str] = None) -> Question:
        """Hand the waiting asker an answer in the form any client sends
        (``Question.form``): the ``value`` of each choice picked, and the text
        written. The kind's own check decides whether it fits (AnswerRejected)."""
        raise NotImplementedError(f"{type(self).__name__} takes no answer: no client could answer it")

    def resolve(self, question_id: str, answer: Any) -> Question:
        """Hand the waiting asker ``answer``. Whether the answer fits the
        question is the caller's check; this one only takes it once."""
        # Taken out first: of two answers at once, one finds it and the other does not.
        question = self._questions.pop(question_id, None)
        if question is None or question.answer.done():
            raise AnswerRejected(404, NOT_WAITING)
        loop = question.answer.get_loop()
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is loop:
            question.answer.set_result(answer)
        else:
            # The app serves routes on the loop the runs run on; a server that
            # does not must not touch the future from another thread.
            loop.call_soon_threadsafe(_resolve, question.answer, answer)
        return question


def waiting_question(question_id: str) -> Optional[Question]:
    """The question waiting under ``question_id`` in any broker of the process, or None."""
    for broker in list(_brokers):
        question = broker.get(question_id)
        if question is not None:
            return question
    return None


def answer_question(question_id: str, choices: Sequence[str], text: str,
                    answered_by: Optional[str] = None) -> Question:
    """Answer the question waiting under ``question_id``, whichever asker
    waits for it (``QuestionBroker.take``); AnswerRejected when none does or
    the answer does not fit. Who may answer is the caller's check: the routes'
    ``may_answer``, or -- for ``agent-cli chat`` -- the person at the terminal
    that started the run."""
    for broker in list(_brokers):
        if broker.get(question_id) is not None:
            return broker.take(question_id, choices, text, answered_by=answered_by)
    raise AnswerRejected(404, NOT_WAITING)


def is_read(request_id: str, gone_after_seconds: float) -> bool:
    """Whether a person watches a stream that the status lines of
    ``request_id`` reach (see ``status_forwarding.attended_stream_of``)."""
    from ..servers.agent.components.status_forwarding import attended_stream_of
    return attended_stream_of(request_id, gone_after_seconds) is not None


async def wait_for_answer(question: Question, token: Any, *, wait: float, reask_seconds: float,
                          reask: Callable[[], Awaitable[Any]], read: Callable[[], bool],
                          interrupt: Optional[Callable[[], Any]] = None) -> Any:
    """The person's answer (what the route resolved the question with), or
    TIMEOUT after ``wait`` seconds, CANCELLED once ``token`` is, GONE once
    ``read()`` says nobody reads the run any more (no reader for the grace
    ``read`` allows, counted by the run's job from the moment the last one
    left, or the stream is over), or whatever ``interrupt()`` returns once it
    returns something other than None (looked at as often as ``read``). Asks
    again every ``reask_seconds`` while it waits."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    cancelled = None
    if token is not None and hasattr(token, "wait_for_cancellation"):
        cancelled = asyncio.ensure_future(token.wait_for_cancellation())
    next_reask = loop.time() + reask_seconds
    try:
        while True:
            if question.answer.done():
                return question.answer.result()
            if token is not None and getattr(token, "is_cancelled", False):
                return CANCELLED
            now = loop.time()
            if now >= deadline:
                return TIMEOUT
            if not read():
                return GONE
            stop = interrupt() if interrupt is not None else None
            if stop is not None:
                return stop
            if now >= next_reask:
                await reask()
                next_reask = now + reask_seconds
            wake = min(deadline, next_reask, now + READER_POLL_SECONDS) - now
            if cancelled is None and token is not None:
                wake = min(wake, CANCEL_POLL_SECONDS)
            waiters = {question.answer} | ({cancelled} if cancelled is not None else set())
            await asyncio.wait(waiters, timeout=max(wake, 0), return_when=asyncio.FIRST_COMPLETED)
    finally:
        if cancelled is not None and not cancelled.done():
            cancelled.cancel()


async def put_to_person(broker: QuestionBroker, question: Question, scope: Any, *, meta_key: str,
                        answer_url: str, line: str, token: Any, reask_seconds: float,
                        gone_after_seconds: float, cut_off: str,
                        interrupt: Optional[Callable[[], Any]] = None) -> Any:
    """Put ``question`` on the row of ``scope`` (an entered StatusScope) and
    wait for it: what ``wait_for_answer`` returns.

    The row's lines carry ``meta[meta_key]`` -- the question as ``to_public``
    shows it, and ``answer_url``, where the chat posts the answer. The row's
    last line is the caller's (it knows what the outcome means), except when
    the wait is cut off from outside (the run torn down, a registry's timeout):
    then the row ends here with ``cut_off`` and the cancellation goes on. The
    question is out of the broker before this returns or re-raises; the caller
    closes it once more when its call ends, for an error on the way.

    Whoever takes the question out of the broker owns the outcome: an answer
    that took it after the wait's last look (``resolve`` from a thread of its
    own hands the answer to this loop a moment later) was told it was taken,
    so it is what this returns -- not a timeout the person never learns of.
    """
    meta = {meta_key: {**question.to_public(), "answer_url": answer_url}}
    await scope.progress(line, meta)
    try:
        outcome = await wait_for_answer(
            question, token, wait=question.timeout, reask_seconds=reask_seconds,
            reask=lambda: scope.progress(line, meta),
            read=lambda: is_read(question.request_id, gone_after_seconds), interrupt=interrupt)
        # Out of reach before the caller awaits anything (its last line): an
        # answer that came in then would be taken ("ok") for a question already
        # settled. Unless an answer took it first -- then that answer is the
        # outcome, handed over a moment later (inside the try: a cancel while it
        # is handed over ends the row as any cut-off wait does).
        if not question.answer.done() and not broker.withdraw(question):
            outcome = await question.answer
    except asyncio.CancelledError:
        # The row must not keep its answer box, and an answer that comes
        # now must be refused.
        broker.close(question)
        await scope.error(status_line(cut_off))
        raise
    return outcome
