"""agent-cli chat answers what a run asks the person at the terminal (ask_user, tool_approval): the question drawn
from the form every client draws, answered with the next line typed -- as the web chat answers it."""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import pytest

from agent_system.cli_utils import chat
from agent_system.cli_utils.chat import ChatRenderer, _ChatContext, _execute_turn
from agent_system.cli_utils.questions import ANSWERED, NOT_AN_ANSWER, NotAnAnswer, parse_answer, question_lines
from agent_system.core.request_context import run_is_attended
from agent_system.core.run_questions import AnswerRejected, Question, QuestionBroker, put_to_person
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from agent_system.tools.status import StatusScope, status_bus

APPROVAL = {"prompt": "Approve write_file?", "detail": '{"path": "a.txt"}\n{"mode": "w"}',
            "warning": "It runs without approvals.",
            "choices": [{"value": "allow_once", "label": "Allow once"},
                        {"value": "allow_session", "label": "Allow for this session"},
                        {"value": "deny", "label": "Deny"}],
            "multi_select": False, "text": {"label": "Why not (sent to the agent with Deny)", "alone": False}}
PICKS = {"prompt": "Which?", "detail": None, "warning": None,
         "choices": [{"value": "pg", "label": "Postgres"}, {"value": "lite", "label": "SQLite"}],
         "multi_select": True, "text": {"label": "Or answer in your own words", "alone": True}}
OPEN = {"prompt": "Name it", "detail": None, "warning": None, "choices": [], "multi_select": False,
        "text": {"label": "Your answer", "alone": True}}
NUMBER_ONLY = {**APPROVAL, "text": None}


def _options(*labels):
    return {**PICKS, "multi_select": False, "choices": [{"value": label, "label": label} for label in labels]}


@pytest.mark.parametrize("form, line, answer", [
    (APPROVAL, "1", (["allow_once"], "")),
    (APPROVAL, "3 too wide", (["deny"], "too wide")),
    (APPROVAL, "3: too wide", (["deny"], "too wide")),
    (APPROVAL, "3 - too wide", (["deny"], "too wide")),
    (APPROVAL, "deny", (["deny"], "")),
    (PICKS, "1, 2", (["pg", "lite"], "")),
    (PICKS, "1 2", (["pg", "lite"], "")),
    (PICKS, "2: small is enough", (["lite"], "small is enough")),
    # words that may answer alone are the person's answer: "2 days" is not the second choice with a remark
    (PICKS, "2 small is enough", ([], "2 small is enough")),
    (_options("1 day", "3 days", "a week"), "2 days", ([], "2 days")),
    (PICKS, "neither, use files", ([], "neither, use files")),
    (PICKS, "2024 was a good year", ([], "2024 was a good year")),
    (OPEN, "Scarab", ([], "Scarab")),
    (_options("1 day", "3 days", "7 days"), "3 days", (["3 days"], "")),   # its text first, not the third
    (_options("1 day", "3 days", "a week"), "b", (["3 days"], "")),   # a label begins with a number: letters
    (_options("none", "a few"), "5 people", ([], "5 people")),
    # a label that is a number would be named twice by a typed number: the choices go by letters
    (_options("3", "5", "8"), "3", (["3"], "")),
    (_options("3", "5", "8"), "c", (["8"], "")),
    (_options("4", "3", "2", "1"), "1", (["1"], "")),
    (_options("4", "3", "2", "1"), "A", (["4"], "")),
    (_options("2023", "2024", "2025"), "2024", (["2024"], "")),
    (_options("2023", "2024", "2025"), "b: a good one", (["2024"], "a good one")),
])
def test_a_typed_line_picks_by_number_or_label_and_says_the_rest(form, line, answer):
    assert parse_answer(form, line) == answer


@pytest.mark.parametrize("form, line, said", [
    (APPROVAL, "maybe", "Type a number (1-3)"),
    (APPROVAL, "1,3", "Pick one"),
    (APPROVAL, "1 2", "Pick one"),
    (APPROVAL, "1\n2", "Pick one"),   # two lines pasted
    (APPROVAL, "4", "There is no choice 4"),
    (APPROVAL, "4 too wide", "There is no choice 4"),
    (PICKS, "3", "There is no choice 3"),
    (_options("none", "a few"), "5", "A number as your own answer needs a word with it"),
    (_options("1 day", "3 days", "a week"), "3", "There is no choice 3: Type a letter (a-c)"),
    (_options("2023", "2024", "2025"), "2", "There is no choice 2: Type a letter (a-c)"),
    (APPROVAL, "  ", "Type a number (1-3)"),
    (NUMBER_ONLY, "1 because", "Only a number answers this"),
])
def test_a_line_that_answers_nothing_says_what_would(form, line, said):
    with pytest.raises(NotAnAnswer, match=said.replace("(", r"\(").replace(")", r"\)")):
        parse_answer(form, line)


def test_the_question_is_drawn_with_its_detail_warning_choices_and_how_to_answer():
    lines = [text for text, _ in question_lines({"id": "q", "agent": "coder", "form": APPROVAL}, sub_agent=True)]

    assert lines == ["? Approve write_file? (coder)", '    {"path": "a.txt"}', '    {"mode": "w"}',
                     "  ! It runs without approvals.", "  1) Allow once", "  2) Allow for this session",
                     "  3) Deny",
                     "  Type a number (1-3), and after it why not (sent to the agent with Deny) if you like. "
                     "Enter sends it."]


def test_choices_whose_labels_are_numbers_are_drawn_with_letters():
    lines = [text for text, _ in question_lines({"id": "q", "form": {**_options("4", "3"), "prompt": "Stars?"}})]

    assert lines == ["? Stars?", "  a) 4", "  b) 3", "  Type a letter (a-b), words after a colon if you like -- "
                     "or answer in your own words. Enter sends it."]


@dataclass
class _Which(Question):
    def form(self):
        return PICKS


class _Broker(QuestionBroker):
    def take(self, question_id, choices, text, answered_by=None):
        if not choices and not text:
            raise AnswerRejected(422, "pick an option or write an answer")
        return self.resolve(question_id, (tuple(choices), text, answered_by))


class _AskingAgent:
    """A run that asks the person watching it, through the real wait: its stream (the forwarder a run starts)
    and the question's status row, asked again every few hundredths of a second."""

    def __init__(self, broker):
        self.broker = broker
        self.attended = None
        self.injected = []

    async def run_events(self, task, request_id=None, session_id=None, llm_override=None,
                         llm_profile_info_override=None):
        yield {"type": "start", "request_id": request_id, "session_id": session_id}
        self.request_id = request_id
        self.attended = run_is_attended(request_id)
        stream = StatusEventForwarder()
        await stream.start_forwarding(request_id)
        row = f"{request_id}_001"
        question = self.broker.open_question(_Which, owner="u", session_id=session_id, request_id=row,
                                             agent_name="a", timeout=5)
        try:
            async with StatusScope(status_bus, "probe", request_id=row, start_msg="Which?") as scope:
                outcome = await put_to_person(
                    self.broker, question, scope, meta_key="probe", answer_url="/plugins/probe/answer",
                    line="Which?", token=None, reask_seconds=0.05, gone_after_seconds=5, cut_off="cut off")
                await scope.end("answered")
        finally:
            await stream.stop_forwarding()
        yield {"type": "final", "summary": repr(outcome)}

    async def append_user_message(self, request_id, content):
        self.injected.append(content)
        return True

    async def cancel_request(self, request_id):
        return True


class _Typist:
    """The person at the terminal: types the lines once the question is shown -- the first while it is
    shown only (not an answer), then the answer."""

    def __init__(self, broker, lines):
        self.broker = broker
        self.lines = list(lines)
        self.buffer = ""
        self.enabled = True
        self._seen = 0

    def poll(self):
        if self.lines and self.broker.pending():
            self._seen += 1
            if self._seen > 6:   # a few re-asks first: each is shown once only
                self._seen = 0
                return [self.lines.pop(0)]
        return []

    def close(self):
        self.enabled = False


def _turn(monkeypatch, agent, typist, show_status=True):
    monkeypatch.setattr(chat, "_KeyReader", lambda active: typist)
    out = io.StringIO()
    renderer = ChatRenderer(ansi=False, out=out, width_override=200, height_override=30)
    ctx = _ChatContext(agent=agent, entry_name="a", session_service=None, session_user="u", session_id="s",
                       was_new_session=False, llm_profile="p", llm_override=None, llm_profile_info=None,
                       show_status=show_status)
    loop = asyncio.new_event_loop()
    try:
        result = _execute_turn(loop, ctx, "hi", renderer)
    finally:
        loop.close()
    return result, out.getvalue()


def test_the_person_at_the_terminal_answers_what_the_run_asks(monkeypatch):
    broker = _Broker()
    agent = _AskingAgent(broker)

    typist = _Typist(broker, ["3", "2: small is enough"])
    result, shown = _turn(monkeypatch, agent, typist)

    assert agent.attended is True, "a chat turn with a person at the terminal is not attended"
    assert result["summary"] == repr((("lite",), "small is enough", "u")), (result, shown)
    assert agent.injected == [], "a line that answers the question went into the run"
    assert shown.count("? Which?") == 1, f"a question asked again was shown again:\n{shown}"
    # what the line was read as, not the line
    assert "There is no choice 3" in shown and "» SQLite: small is enough" in shown, shown
    assert result.get("typed_partial") == "3", "a line that answered nothing was lost, not put back to edit"
    assert not run_is_attended(agent.request_id), "the turn's mark outlived it"
    assert broker.pending() == []


class _Line:
    def __init__(self, meta):
        self.meta = meta


def _asked(broker, request_id, prompt, agent="main"):
    question = broker.open_question(_Which, owner="u", session_id="s", request_id=request_id, agent_name=agent,
                                    timeout=5)
    return question, _Line({"probe": {**question.to_public(), "form": {**PICKS, "prompt": prompt}}})


async def test_the_turn_answers_its_own_questions_one_after_the_other_and_only_while_they_wait():
    """Only the question the next line answers is shown in full: two calls of one tool would otherwise be told
    apart by nothing but their order on the screen."""
    from agent_system.cli_utils.questions import TurnQuestions

    broker = _Broker()
    shown = []
    turn = TurnQuestions("run1", "u", lambda text, colour: shown.append(text), agent="main")
    other_run, other_line = _asked(broker, "run9_001", "Elsewhere?")
    first, first_line = _asked(broker, "run1_001", "First?")   # the chat's agent, in a call of its own
    second, second_line = _asked(broker, "run1_002_003", "Second?", agent="helper")   # a sub-agent

    for line in (other_line, first_line, second_line, first_line):
        turn.see(line)

    assert [s for s in shown if s.startswith("?")] == ["? First?"], shown
    assert [s for s in shown if "then:" in s] == ["  … then: Second? (helper)"], "a question asked again was named again"
    assert turn.current == first.id
    assert turn.answer("9", turn.current) == NOT_AN_ANSWER and not first.answer.done()
    assert turn.answer("1", first.id) == ANSWERED and first.answer.result() == (("pg",), "", "u"), "not the one shown"
    assert [s for s in shown if s.startswith("?")] == ["? First?", "? Second? (helper)"], "the next not shown in full"
    assert turn.current == second.id
    broker.close(second)   # its asker gave up waiting
    turn.refresh()
    assert "  (no longer waiting: Second?)" in shown, shown
    drawn = list(shown)
    turn.see(second_line)   # its last re-ask, late
    assert shown == drawn, "a question no longer waiting came back"
    assert turn.current is None and turn.answer("2", None) is None, "no question waits, and the line was taken"
    assert not other_run.answer.done(), "a question of another run was answered"


async def test_a_line_answers_the_question_shown_when_it_was_begun_and_no_other():
    """The person reads First? and types "1"; First? ends before Enter. The line must not approve Second?,
    which came up in its place unread -- nor answer anything when it was begun before any question showed."""
    from agent_system.cli_utils.questions import TOO_LATE, TurnQuestions

    broker = _Broker()
    shown = []
    turn = TurnQuestions("run1", "u", lambda text, colour: shown.append(text), agent="main")
    first, first_line = _asked(broker, "run1_001", "First?")
    second, second_line = _asked(broker, "run1_002", "Second?")
    turn.see(first_line)
    turn.see(second_line)
    begun_for = turn.current

    broker.close(first)

    assert turn.answer("1", begun_for) == TOO_LATE and not second.answer.done(), "First?'s answer went to Second?"
    # named: Second? is drawn by now, and the line was not for it
    assert turn.current == second.id and "  Not taken: First? -- it no longer waits." in shown, shown
    assert turn.answer("1", None) is None and not second.answer.done(), "a line begun before any question took one"
    # words for First? are not lost: they go to the agent as a message -- a bare number would reach it as "7"
    assert turn.answer("neither, use files", first.id) is None and not second.answer.done()
    assert "  First? no longer waits: your line goes to the agent as a message." in shown, shown
    assert turn.answer("Postgres", first.id) is None, "an option named for a question gone was dropped"
    assert turn.answer("7", first.id) == TOO_LATE, "a bare number for a question gone was sent on"


async def test_the_chat_looks_at_its_questions_while_nothing_happens():
    """A question may end while the run says nothing and nobody types: the type-ahead tick looks."""
    from agent_system.cli_utils.chat import _poll_typed_input

    class _Idle:
        enabled = True
        buffer = ""

        def poll(self):
            return []

    class _Questions:
        looked = 0
        current = None

        def refresh(self):
            self.looked += 1

    questions = _Questions()
    out = io.StringIO()
    renderer = ChatRenderer(ansi=False, out=out, width_override=200, height_override=30)
    ctx = _ChatContext(agent=None, entry_name="a", session_service=None, session_user="u", session_id="s",
                       was_new_session=False, llm_profile="p", llm_override=None, llm_profile_info=None,
                       show_status=True)
    task = asyncio.create_task(_poll_typed_input(_Idle(), renderer, ctx, {"request_id": "q", "questions": questions}))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert questions.looked >= 2, questions.looked


@pytest.mark.parametrize("typed, taken, draft, kept", [
    ("maybe", NOT_AN_ANSWER, "", "maybe"),
    ("one\ntwo", NOT_AN_ANSWER, "", ""),   # pasted lines would break the input row, and stay out
    ("1", ANSWERED, "", ""),
    ("maybe", NOT_AN_ANSWER, "half a line", "half a line"),   # typed on meanwhile: not overwritten
])
async def test_a_line_that_answers_nothing_goes_back_on_the_input_line_if_it_fits_there(typed, taken, draft, kept):
    """One line goes back to be made an answer -- a line that answered, or onto a line typed since, not."""
    from agent_system.cli_utils.chat import _poll_typed_input

    class _Once:
        enabled = True
        buffer = ""
        sent = False

        def poll(self):
            if self.sent:
                return []
            self.sent = True
            self.buffer = draft
            return [typed]

    class _Questions:
        current = "q1"

        def answer(self, line, meant_for):
            return taken

        def refresh(self):
            pass

    reader = _Once()
    out = io.StringIO()
    renderer = ChatRenderer(ansi=False, out=out, width_override=200, height_override=30)
    ctx = _ChatContext(agent=None, entry_name="a", session_service=None, session_user="u", session_id="s",
                       was_new_session=False, llm_profile="p", llm_override=None, llm_profile_info=None,
                       show_status=True)
    task = asyncio.create_task(_poll_typed_input(reader, renderer, ctx, {"request_id": "q", "questions": _Questions()}))
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert reader.buffer == kept


@pytest.mark.parametrize("fails", ["gives up", "raises"])
async def test_a_turn_whose_typing_stopped_is_attended_no_more(fails):
    """The key reader broke: nothing typed reaches the run, so a question must not wait for its timeout. The
    real reader gives up quietly -- it disables itself on a read error (_KeyReader._read_chars)."""
    from agent_system.cli_utils.chat import _poll_typed_input
    from agent_system.core.request_context import set_run_attended

    class _Broken:
        enabled = True
        buffer = ""

        def poll(self):
            if fails == "raises":
                raise OSError("console gone")
            self.enabled = False
            return []

    set_run_attended("rq-typing", True)
    out = io.StringIO()
    renderer = ChatRenderer(ansi=False, out=out, width_override=200, height_override=30)
    ctx = _ChatContext(agent=None, entry_name="a", session_service=None, session_user="u", session_id="s",
                       was_new_session=False, llm_profile="p", llm_override=None, llm_profile_info=None,
                       show_status=True)

    await _poll_typed_input(_Broken(), renderer, ctx, {"request_id": "rq-typing"})

    assert not run_is_attended("rq-typing")


async def test_a_line_answers_the_question_shown_when_its_typing_began():
    """The poller tells the questions which one a line was begun under: none (it goes to the run), the one
    shown then though another replaced it since, or -- begun and sent in one tick -- the one shown since the
    tick before: a question drawn while the poller slept may have come after the keys."""
    from agent_system.cli_utils.chat import _poll_typed_input

    # per tick: the question shown in full, the input line after the keys of the tick, the lines sent in it
    ticks = [(None, "2", []), ("q1", "2", []), ("q1", "", ["2"]), ("q1", "x", []), ("q2", "", ["x"]),
             ("q2", "", ["y"]), (None, "", []), ("q3", "", ["z"])]

    class _Questions:
        current = ticks[0][0]
        at = 0
        taken = []

        def answer(self, line, meant_for):
            self.taken.append((line, meant_for))
            return ANSWERED

        def refresh(self):   # the end of a tick: the next one shows its question
            self.at += 1
            self.current = ticks[min(self.at, len(ticks) - 1)][0]

    questions = _Questions()

    class _Keys:
        enabled = True
        buffer = ""

        def poll(self):
            if questions.at >= len(ticks):
                return []
            _, self.buffer, sent = ticks[questions.at]
            return sent

    out = io.StringIO()
    renderer = ChatRenderer(ansi=False, out=out, width_override=200, height_override=30)
    ctx = _ChatContext(agent=None, entry_name="a", session_service=None, session_user="u", session_id="s",
                       was_new_session=False, llm_profile="p", llm_override=None, llm_profile_info=None,
                       show_status=True)
    task = asyncio.create_task(_poll_typed_input(_Keys(), renderer, ctx, {"request_id": "q", "questions": questions}))
    await asyncio.sleep(0.6)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert questions.taken == [("2", None), ("x", "q1"), ("y", "q2"), ("z", None)]


def test_a_line_typed_while_the_turn_is_stopped_answers_nothing():
    """Ctrl-C: the type-ahead runs on while the turn unwinds -- an Allow typed then must not beat the cancel."""
    loop = asyncio.new_event_loop()
    try:
        async def finished():
            return {"summary": None}

        turn = loop.create_task(finished())
        state = {"request_id": None, "questions": object()}
        renderer = ChatRenderer(ansi=False, out=io.StringIO(), width_override=200, height_override=30)
        ctx = _ChatContext(agent=None, entry_name="a", session_service=None, session_user="u", session_id="s",
                           was_new_session=False, llm_profile="p", llm_override=None, llm_profile_info=None,
                           show_status=True)
        chat._cancel_turn(loop, ctx, turn, state, renderer)
    finally:
        loop.close()

    assert "questions" not in state


@pytest.mark.parametrize("show_status, keys_read", [(False, True), (True, False)],
                         ids=["no status shown", "no keys read (no ANSI, NO_COLOR, not a terminal)"])
def test_a_turn_nobody_could_answer_in_is_not_attended(monkeypatch, show_status, keys_read):
    """Nobody would see the question, or nothing typed would reach it: the run decides without asking, as
    agent-run's do."""
    broker = _Broker()

    class _Probe(_AskingAgent):
        async def run_events(self, task, request_id=None, **kwargs):
            self.attended = run_is_attended(request_id)
            yield {"type": "final", "summary": "done"}

    agent = _Probe(broker)
    typist = _Typist(broker, [])
    typist.enabled = keys_read
    _turn(monkeypatch, agent, typist, show_status=show_status)

    assert agent.attended is False
