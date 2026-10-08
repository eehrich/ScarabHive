"""A machine that waits for an event while its caller blocks asks the person who watches that caller
(wait_questions.py): in the form every client draws, answered from agent-cli chat as from the web chat.

The real facade (MachineAgent) over a real StateGraphServer; the question goes through core.run_questions as
any client's answer does (answer_question), and once through a whole agent-cli chat turn.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from agent_system.core.request_context import release_run_attended, set_run_attended
from agent_system.core.run_questions import AnswerRejected, answer_question
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, until
from plugins.stategraph.tests.test_plugin_stategraph_facade import Env, final_of

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

REVIEW = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
events:
  approve: {description: the draft is fine}
  reject: {description: send it back, data: {type: object, properties: {why: {type: string}}}}
context: {why: null}
initial: review
states:
  review:
    description: Is the draft fine?
    transitions:
      - trigger: approve
        target: done
      - trigger: reject
        target: rejected
        effect: ctx.why = event.data["why"]
  done: {type: final, output: {verdict: approved}}
  rejected: {type: final, output: {verdict: rejected, why: "{{ ctx.why }}"}}
"""


@pytest.fixture
async def blocking(tmp_path):
    made = Env(tmp_path)  # on_wait: block, the default
    (tmp_path / "machines" / "m.yaml").write_text(REVIEW, encoding="utf-8")
    yield made
    await made.close()


def _said(events, words):
    return any(words in json.dumps(event, default=str) for event in events)


async def _asked(env, request_id="r1"):
    """The request's run, consumed into a list as it goes; and the question it put once it waits."""
    events: list = []

    async def consume():
        async for event in env.agent.run_events("the draft", request_id=request_id, session_id="s1"):
            events.append(event)

    task = asyncio.ensure_future(consume())
    await until(lambda: env.server.wait_questions.pending(), what="the question")
    return task, events, env.server.wait_questions.pending()[0]


async def test_a_blocking_machine_asks_the_person_who_watches_and_the_answer_sends_its_event(blocking):
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        form = question.form()
        assert form["prompt"] == "m waits for an event in 'review'", form
        assert [c["label"] for c in form["choices"]] == ["approve", "reject"], form
        assert form["detail"].splitlines() == [
            "review: Is the draft fine?", "approve: the draft is fine",
            'reject: send it back -- data: {"type": "object", "properties": {"why": {"type": "string"}}}'], form
        assert form["text"] == {"label": "Data to send with it (JSON or text)", "alone": False}, form
        await until(lambda: _said(events, question.id), what="the question on the caller's stream")

        for choices, text, said in ((["approve", "reject"], "", "pick one event"),
                                    (["reject"], '"too long"', "")):   # the machine wants an object
            with pytest.raises(AnswerRejected) as refused:
                answer_question(question.id, choices, text, answered_by="u")
            assert refused.value.status == 422 and said in refused.value.detail, refused.value.detail
        assert blocking.server.wait_questions.pending() == [question], "a refused answer ended the question"

        answer_question(question.id, ["reject"], '{"why": "too long"}', answered_by="u")
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"verdict": "rejected", "why": "too long"}'
    assert _said(events, "reject sent by u"), "the question's row did not say what became of it"
    assert blocking.server.wait_questions.pending() == []


async def test_nobody_watching_is_asked_nothing_and_the_run_waits_as_before(blocking, monkeypatch):
    # counted, not looked at: a question put to nobody ends at once (gone) and would be missed between two looks
    opened: list = []
    open_question = blocking.server.wait_questions.open_question
    monkeypatch.setattr(blocking.server.wait_questions, "open_question",
                        lambda *args, **fields: opened.append(fields) or open_question(*args, **fields))
    events: list = []

    async def consume():
        async for event in blocking.agent.run_events("the draft", request_id="r1", session_id="s1"):
            events.append(event)

    task = asyncio.ensure_future(consume())
    await until(lambda: any(r["status"] == "waiting" for r in blocking.runs()), what="the wait")
    await asyncio.sleep(1.2)  # a tick of the facade's loop: it would have asked by now
    assert opened == [] and not task.done(), opened

    blocking.server.service.send_event(blocking.runs()[0]["id"], "approve")
    await asyncio.wait_for(task, 10)
    assert final_of(events)["summary"] == '{"verdict": "approved"}'


async def test_an_event_sent_elsewhere_ends_the_question_and_says_so(blocking):
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        blocking.server.service.send_event(question.run_id, "approve")  # the panel's, a callback's
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"verdict": "approved"}'
    assert _said(events, "m moved on without an answer here"), [e for e in events if e.get("type") == "status"]
    with pytest.raises(AnswerRejected) as late:
        answer_question(question.id, ["approve"], "", answered_by="u")
    assert late.value.status == 404


async def test_the_answered_wait_is_not_asked_again_before_the_run_took_its_event(blocking, monkeypatch):
    service, loop = blocking.server.service, asyncio.get_running_loop()
    send = service.send_event

    def later(run_id, name, data=None, frame=None, *, user_id=None):  # taken now, dispatched in 1.5 s
        loop.call_later(1.5, lambda: send(run_id, name, data, frame, user_id=user_id))
        return {"accepted": True}

    monkeypatch.setattr(service, "send_event", later)
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        answer_question(question.id, ["approve"], "", answered_by="u")
        await asyncio.sleep(1.2)  # a tick of the facade's loop on the same wait
        assert blocking.server.wait_questions.pending() == [], "the answered wait was asked again"
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"verdict": "approved"}'


async def test_a_question_nobody_answers_in_time_ends_and_the_run_waits_on(blocking, monkeypatch):
    from plugins.stategraph import wait_questions

    monkeypatch.setattr(wait_questions, "ASK_TIMEOUT", 0.3)
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        await until(lambda: _said(events, "no answer within 0.3 s; m waits on"), what="the timeout's line")
        await asyncio.sleep(1.2)
        assert blocking.server.wait_questions.pending() == [] and not task.done(), "asked again after its timeout"
        blocking.server.service.send_event(question.run_id, "approve")
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")


async def test_a_wait_is_asked_again_when_somebody_reads_the_run_again(blocking):
    set_run_attended("r1", True)
    try:
        task, events, first = await _asked(blocking)
        release_run_attended("r1")  # the person left
        await until(lambda: _said(events, "nobody reads the run any more; m waits on"), what="the left line")
        assert blocking.server.wait_questions.pending() == []
        set_run_attended("r1", True)  # and came back
        again = await _next(blocking, first)
        answer_question(again.id, ["approve"], "", answered_by="u")
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"verdict": "approved"}'


GUARDED = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
events:
  approve: {description: fine}
  note: {description: count}
context: {n: 0}
initial: review
states:
  review:
    transitions:
      - trigger: approve
        target: done
        guard: "ctx.n > 0"
      - trigger: note
        effect: ctx.n = ctx.n + 1
  done: {type: final, output: {verdict: approved}}
"""


async def _next(env, after):
    await until(lambda: [q for q in env.server.wait_questions.pending() if q.id != after.id], what="the next question")
    return env.server.wait_questions.pending()[0]


async def test_an_answer_a_guard_discards_is_asked_again_with_why(blocking, tmp_path):
    (tmp_path / "machines" / "m.yaml").write_text(GUARDED, encoding="utf-8")
    set_run_attended("r1", True)
    try:
        task, events, first = await _asked(blocking)
        answer_question(first.id, ["approve"], "", answered_by="u")
        again = await _next(blocking, first)
        assert again.form()["detail"].splitlines()[0] == ("Not taken: 'approve' in 'review' -- no transition took it "
                                                          "(guards: ctx.n > 0 -> False)."), again.form()
        assert again.form()["text"] is None, "data offered where no event takes any"
        answer_question(again.id, ["note"], "", answered_by="u")
        third = await _next(blocking, again)
        assert "Not taken" not in third.form()["detail"], "an answer the run took was said not taken"
        answer_question(third.id, ["approve"], "", answered_by="u")
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"verdict": "approved"}'


SUB = """\
stategraph: 1
id: sub
params: {label: {type: string}}
events: {approve: {description: ok}}
initial: w
states:
  w:
    transitions: [{trigger: approve, target: ok}]
  ok: {type: final, output: "{{ params.label }}"}
"""
PARALLEL = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
imports: {sub: ./sub.yaml}
initial: work
states:
  work:
    do:
      parallel:
        left: {machine: sub, params: {label: left}}
        right: {machine: sub, params: {label: right}}
    transitions: [{target: done}]
  done: {type: final, output: both}
"""


async def test_an_event_several_frames_take_is_offered_once_for_each(blocking, tmp_path):
    (tmp_path / "machines" / "m.yaml").write_text(PARALLEL, encoding="utf-8")
    (tmp_path / "machines" / "sub.yaml").write_text(SUB, encoding="utf-8")
    set_run_attended("r1", True)
    try:
        task, events, _ = await _asked(blocking)
        await until(lambda: len(blocking.server.wait_questions.pending()[0].offers) == 2, what="both frames waiting")
        both = blocking.server.wait_questions.pending()[0]
        choices = both.form()["choices"]
        assert all(c["label"].startswith("approve in '") for c in choices) and len({c["value"] for c in choices}) == 2

        answer_question(both.id, [choices[0]["value"]], "", answered_by="u")
        other = await _next(blocking, both)
        assert [c["label"] for c in other.form()["choices"]] == ["approve"], "the frame answered was offered again"
        answer_question(other.id, ["approve"], "", answered_by="u")
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"output": "both"}'


NESTED = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
imports: {sub: ./sub.yaml}
events: {approve: {description: skip the inner review}}
initial: work
states:
  work:
    do: {machine: sub, params: {label: inner}}
    transitions:
      - {target: done}
      - {trigger: approve, target: skipped}
  done: {type: final, output: inner}
  skipped: {type: final, output: skipped}
"""


async def test_an_event_the_waiting_child_and_its_parent_take_goes_to_the_child(blocking, tmp_path):
    (tmp_path / "machines" / "m.yaml").write_text(NESTED, encoding="utf-8")
    (tmp_path / "machines" / "sub.yaml").write_text(SUB, encoding="utf-8")
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        assert [c["label"] for c in question.form()["choices"]] == ["approve"], question.form()
        assert question.form()["detail"] == "approve: ok", "described by another machine than the one it goes to"
        answer_question(question.id, ["approve"], "", answered_by="u")  # refused as "several frames" before
        await asyncio.wait_for(task, 10)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"output": "inner"}'


async def test_a_reply_in_the_conversation_goes_to_the_waiting_child_too(tmp_path):
    """on_wait: ask -- the reply names the event, not the frame; the parent's invoking state takes it as well."""
    env = Env(tmp_path, on_wait="ask")
    (tmp_path / "machines" / "m.yaml").write_text(NESTED, encoding="utf-8")
    (tmp_path / "machines" / "sub.yaml").write_text(SUB, encoding="utf-8")
    try:
        question = final_of(await env.ask("go", request_id="r1", session_id="s1"))
        assert "- approve: ok" in question["summary"], question["summary"]
        answer = final_of(await env.ask("approve", request_id="r2", session_id="s1"))
    finally:
        await env.close()

    assert answer["summary"] == '{"output": "inner"}', answer


SLOW = """\
stategraph: 1
id: slow
python: slow.py
initial: nap
states:
  nap:
    do: {call: nap}
    transitions: [{target: up}]
  up: {type: final, output: rested}
"""
BUSY = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
imports: {sub: ./sub.yaml, slow: ./slow.yaml}
initial: work
states:
  work:
    do:
      parallel:
        left: {machine: sub, params: {label: left}}
        right: {machine: slow}
    transitions: [{target: done}]
  done: {type: final, output: both}
"""


async def test_a_wait_is_asked_while_another_branch_still_works(blocking, tmp_path):
    (tmp_path / "machines" / "m.yaml").write_text(BUSY, encoding="utf-8")
    (tmp_path / "machines" / "sub.yaml").write_text(SUB, encoding="utf-8")
    (tmp_path / "machines" / "slow.yaml").write_text(SLOW, encoding="utf-8")
    (tmp_path / "machines" / "slow.py").write_text("import asyncio\n\n\nasync def nap():\n    await asyncio.sleep(3)\n",
                                                   encoding="utf-8")
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        assert blocking.runs()[0]["status"] == "running", "asked only once the other branch was done"
        answer_question(question.id, ["approve"], "", answered_by="u")
        await asyncio.wait_for(task, 15)
    finally:
        release_run_attended("r1")

    assert final_of(events)["summary"] == '{"output": "both"}'


def test_another_frame_moving_on_leaves_the_answer_for_this_one_standing():
    from plugins.stategraph.wait_questions import Offer, WaitBroker, WaitQuestion

    sent: list = []
    now = {"status": "running", "view": {"frames": [{"prefix": "a/", "step": 5, "accepts": ["go"]},
                                                    {"prefix": "b/", "step": 2, "accepts": ["stop"]}]}}
    broker = WaitBroker(lambda *event: sent.append(event) or {"accepted": True}, lambda run_id: now)
    loop = asyncio.new_event_loop()
    try:
        async def asked():
            return broker.open_question(WaitQuestion, owner=None, session_id="s", request_id="r", agent_name="a",
                                        timeout=60, machine="m", run_id="x", waits=frozenset({("a/", 1), ("b/", 2)}),
                                        offers=(Offer("go", "go", "a/", "go", "", None),
                                                Offer("stop", "stop", "b/", "stop", "", None)))

        question = loop.run_until_complete(asked())
        with pytest.raises(AnswerRejected) as refused:  # a/ waits at another step now: its wait was left
            broker.take(question.id, ["go"], "")
        assert refused.value.status == 409 and sent == []
        broker.take(question.id, ["stop"], "")  # b/ still waits where it was asked
    finally:
        loop.close()
    assert sent == [("x", "stop", None, "b/", None)]


async def test_a_question_that_cannot_be_put_leaves_the_caller_waiting_on(monkeypatch, caplog):
    from types import SimpleNamespace

    from plugins.stategraph.wait_questions import WaitAsker, WaitBroker

    def broken(*args):
        raise OSError("database is locked")

    monkeypatch.setitem(WaitAsker.look.__globals__, "is_read", lambda *args: True)
    row = {"id": "x", "status": "waiting", "definition": {},
           "view": {"frames": [{"prefix": "", "step": 1, "accepts": ["approve"], "machine": "m", "state": "w"}]}}
    broker = WaitBroker(lambda *event: {"accepted": True}, lambda run_id: row)
    asker = WaitAsker(broker, request_id="r", session_id="s", owner=None, agent_name="a", machine="m", token=None,
                      answer_url="/a", store=SimpleNamespace(tail=broken, page=broken), stopped=lambda: False)

    await asker.look(row)  # raised into the facade's loop before, and ended the request

    assert broker.pending() == [] and "was not put to the person" in caplog.text
    await asker.close()


def test_words_nested_deeper_than_the_parser_goes_are_sent_as_words():
    """A written answer is JSON when it reads as JSON: brackets nested past the parser's depth are text, not a 500.

    How deep the parser goes depends on the thread's stack: 5000 levels passed it on Windows (1 MB), but parsed on
    Linux (8 MB: past 5000 on Python 3.13, past 50 000 on 3.14). A million is past it on both."""
    from plugins.stategraph.wait_questions import data_of

    deep = "[" * 1_000_000 + "]" * 1_000_000

    assert data_of(deep) == deep
    assert data_of(' {"a": [1]} ') == {"a": [1]} and data_of("  ") is None


def test_an_answer_to_a_wait_the_run_has_left_is_refused_and_sends_nothing():
    """The run moves on before its caller looks again: the event would wait in the inbox for the next wait."""
    from plugins.stategraph.wait_questions import Offer, WaitBroker, WaitQuestion

    sent: list = []
    now = {"status": "waiting", "view": {"frames": [{"prefix": "", "step": 3, "accepts": ["approve"]}]}}
    broker = WaitBroker(lambda *event: sent.append(event) or {"accepted": True}, lambda run_id: now)
    loop = asyncio.new_event_loop()
    try:
        async def asked():
            return broker.open_question(WaitQuestion, owner=None, session_id="s", request_id="r", agent_name="a",
                                        timeout=60, machine="m", run_id="x", waits=frozenset({("", 2)}),
                                        offers=(Offer("approve", "approve", "", "approve", "", None),))

        question = loop.run_until_complete(asked())
        with pytest.raises(AnswerRejected) as refused:
            broker.take(question.id, ["approve"], "")
        assert refused.value.status == 409 and "out of date" in refused.value.detail and sent == []

        now["view"]["frames"][0]["step"] = 2  # still in the wait asked about: taken
        broker.take(question.id, ["approve"], "")
    finally:
        loop.close()
    assert sent == [("x", "approve", None, "", None)]


async def test_a_run_this_process_does_not_run_is_not_asked_about(monkeypatch):
    """Another process holds it, or its dead owner's lease has not run out: no answer could reach it here."""
    from types import SimpleNamespace

    from plugins.stategraph.wait_questions import WaitAsker, WaitBroker

    monkeypatch.setitem(WaitAsker.look.__globals__, "is_read", lambda *args: True)
    row = {"id": "x", "status": "waiting", "definition": {},
           "view": {"frames": [{"prefix": "", "step": 1, "accepts": ["approve"], "machine": "m", "state": "w"}]}}
    here: dict = {"row": None}
    broker = WaitBroker(lambda *event: {"accepted": True}, lambda run_id: here["row"])
    asker = WaitAsker(broker, request_id="r", session_id="s", owner=None, agent_name="a", machine="m", token=None,
                      answer_url="/a", store=SimpleNamespace(tail=lambda *a: [], page=lambda *a, **k: []),
                      stopped=lambda: False)

    await asker.look(row)
    assert broker.pending() == []
    here["row"] = row  # control: the run lives here
    await asker.look(row)
    assert len(broker.pending()) == 1
    await asker.close()


async def test_a_cancelled_request_is_not_asked_again(blocking, monkeypatch):
    from agent_system.core.cancellation import get_cancellation_manager

    opened: list = []
    open_question = blocking.server.wait_questions.open_question

    def counted(*args, **fields):
        opened.append(fields.get("run_id"))
        return open_question(*args, **fields)

    monkeypatch.setattr(blocking.server.wait_questions, "open_question", counted)
    set_run_attended("r1", True)
    try:
        task, events, question = await _asked(blocking)
        get_cancellation_manager().cancel_request("r1")
        await asyncio.wait_for(task, 20)
    finally:
        release_run_attended("r1")

    assert final_of(events)["type"] == "cancelled", final_of(events)
    assert len(opened) == 1, f"asked {len(opened)} times"


async def test_a_wait_asked_in_the_conversation_is_not_put_to_the_person_as_well(tmp_path):
    """on_wait: ask ends the turn with the question; the reply is the next message, not an answer box too."""
    env = Env(tmp_path, on_wait="ask")
    (tmp_path / "machines" / "m.yaml").write_text(REVIEW, encoding="utf-8")
    set_run_attended("r1", True)
    try:
        events = await env.ask("the draft", request_id="r1", session_id="s1")
    finally:
        release_run_attended("r1")
        await env.close()

    assert final_of(events)["waiting"] == {"states": ["review"], "events": ["approve", "reject"]}
    assert not _said(events, "r1_wait_"), [e for e in events if e.get("type") == "status"]


async def test_called_as_a_tool_a_wait_asks_the_person_who_watches_the_caller(tmp_path):
    """on_wait: ask ends a conversation's turn with the question; as a tool it cannot, so the person is asked."""
    env = Env(tmp_path, on_wait="ask")
    (tmp_path / "machines" / "m.yaml").write_text(REVIEW, encoding="utf-8")
    caller = StatusEventForwarder()  # the chat's run, which called the machine as its tool
    await caller.start_forwarding("c1")
    set_run_attended("c1", True)
    try:
        called = asyncio.ensure_future(env.agent.call(env.agent.name, {"task": "the draft", "request_id": "c1"}))
        await until(lambda: env.server.wait_questions.pending(), what="the question")
        question = env.server.wait_questions.pending()[0]
        assert question.request_id.startswith("c1_") and question.agent_name == "story_machine", question

        answer_question(question.id, ["approve"], "", answered_by="u")

        assert (await asyncio.wait_for(called, 10))["summary"] == '{"verdict": "approved"}'
    finally:
        release_run_attended("c1")
        await caller.stop_forwarding()
        await env.close()


def test_the_person_at_the_terminal_answers_a_waiting_machine_in_agent_cli_chat(tmp_path, monkeypatch):
    """The whole way: agent-cli chat with the machine agent, its wait drawn under the status lines, the line
    typed answering it."""
    from agent_system.cli_utils import chat
    from agent_system.cli_utils.chat import ChatRenderer, _ChatContext, _execute_turn

    class Typist:
        """Types a few ticks after the question is on the screen: a line begun before it was shown is the chat's,
        not its answer (cli_utils.questions.TurnQuestions.answer)."""

        enabled, buffer = True, ""

        def __init__(self, screen, lines):
            self.screen, self.lines, self.read = screen, list(lines), 0

        def poll(self):
            if self.lines and "? m waits" in self.screen.getvalue():
                self.read += 1
                if self.read > 3:
                    return [self.lines.pop(0)]
            return []

        def close(self):
            self.enabled = False

    loop = asyncio.new_event_loop()
    try:
        async def made():
            env = Env(tmp_path)
            (tmp_path / "machines" / "m.yaml").write_text(REVIEW, encoding="utf-8")
            return env

        env = loop.run_until_complete(made())
        out = io.StringIO()
        monkeypatch.setattr(chat, "_KeyReader", lambda active: Typist(out, ["approve"]))
        renderer = ChatRenderer(ansi=False, out=out, width_override=200, height_override=30)
        ctx = _ChatContext(agent=env.agent, entry_name=env.agent.name, session_service=None, session_user="u",
                           session_id="s", was_new_session=False, llm_profile="p", llm_override=None,
                           llm_profile_info=None, show_status=True)
        try:
            result = _execute_turn(loop, ctx, "the draft", renderer)
        finally:
            loop.run_until_complete(env.close())
    finally:
        loop.close()

    shown = out.getvalue()
    assert result["summary"] == '{"verdict": "approved"}', (result, shown)
    assert "? m waits for an event in 'review'" in shown and "» approve" in shown, shown
