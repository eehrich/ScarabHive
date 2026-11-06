"""A server's question to the person (MCP elicitation), against the real probe server.

What is pinned:

* the question goes to the one external call in flight on the connection -- the call the agent core
  hands over in ``current_external_call`` (set by the tool invocation itself); with two calls in flight
  it goes to nobody, and the server hears "cancel", not the person's "decline";
* while a person is asked, the call's timeout does not run out; the run's cancellation ends the question;
* a form is asked field by field: each answer typed as the schema asks, refused at the route when it does
  not fit, Skip for an optional field, Decline declines;
* one question at a time and a few per call; an address that is no plain http(s) one is not shown;
* outside an agent's tool call nobody is asked.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest
from mcp import types as mcp_types

from agent_system.core import run_questions
from agent_system.core.request_context import ExternalCall, current_external_call
from agent_system.core.run_questions import AnswerRejected
from plugins.mcp_client import elicitation
from plugins.mcp_client.connection import MAX_QUESTIONS_PER_CALL
from plugins.mcp_client.elicitation import ElicitBroker, Elicitation, ElicitQuestion
from plugins.mcp_client.manager import ExternalServerPool

PROBE = str(Path(__file__).parent / "probe_server.py")
MARKER = "AGENT_SYSTEM_TEST_SESSION"


def _config():
    return types.SimpleNamespace(
        url=None, transport="stdio", enabled=True, auth=None, initialization_options=None, description=None,
        tools=types.SimpleNamespace(blocked=[], allowed=[]), resources_and_prompts=False,
        command=sys.executable, args=[PROBE], env={MARKER: os.environ[MARKER]})


def _call(request_id: str = "run1_001", token=None) -> ExternalCall:
    return ExternalCall(request_id=request_id, session_id="s1", user_id="alice", agent_name="coder",
                        token=token or types.SimpleNamespace(is_cancelled=False))


async def _pool(elicit, timeout: float = 20) -> ExternalServerPool:
    pool = ExternalServerPool(timeout=timeout, elicit=elicit)
    pool.configure({"probe": _config()})
    await pool.connect("probe")
    return pool


async def _run(pool: ExternalServerPool, call, tool: str = "book", arguments=None) -> str:
    token = current_external_call.set(call)
    try:
        return await pool.call_tool("probe", tool, arguments or {})
    finally:
        current_external_call.reset(token)


def _watched(monkeypatch):
    monkeypatch.setattr(elicitation, "is_read", lambda request_id, gone: True)
    monkeypatch.setattr(run_questions, "is_read", lambda request_id, gone: True)


async def _waiting(broker: ElicitBroker, after=()) -> ElicitQuestion:
    for _ in range(400):
        fresh = [q for q in broker.pending() if q.id not in after]
        if fresh:
            return fresh[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no question was asked")


async def test_the_question_goes_to_the_call_in_flight_and_its_answer_to_the_server():
    asked = []

    async def elicit(server, call, params):
        asked.append((server, call, params.message))
        return mcp_types.ElicitResult(action="accept", content={"seats": 3, "window": True})

    pool = await _pool(elicit)
    try:
        call = _call()
        result = await _run(pool, call)
    finally:
        await pool.close_all()

    assert result == "booked 3 seats, window=True", result
    assert asked == [("probe", call, "How many seats?")]


async def test_the_tool_invocation_hands_the_call_over():
    """The agent core's external call sets the context the pool reads -- not a test doing it by hand."""
    from agent_system.servers.agent.components.tool_invocation import ToolInvoker

    seen = []

    class Integration:
        async def call_tool(self, server, tool, params, kind):
            seen.append(current_external_call.get())
            return {"status": "success"}

    token = types.SimpleNamespace(is_cancelled=False)
    agent = types.SimpleNamespace(name="coder", _tool_integration_manager=types.SimpleNamespace(
        tool_integration=Integration()))
    invoker = ToolInvoker(registry=None, agent=agent)
    tc = {"id": "c1", "function": {"name": "probe.book", "arguments": "{}"}}
    await invoker.execute_external_tool(tc, "probe.book", "probe__book", {"_cancellation_token": token}, 2,
                                        request_id="run7", session_id="s7", user_id="bob")

    assert len(seen) == 1 and seen[0] is not None, seen
    assert (seen[0].request_id, seen[0].session_id, seen[0].user_id, seen[0].agent_name, seen[0].token) == (
        "run7_002", "s7", "bob", "coder", token)
    assert current_external_call.get() is None, "the call's context outlived the call"


async def test_a_person_thinking_does_not_run_out_the_calls_timeout():
    async def slow_person(server, call, params):
        await asyncio.sleep(5)        # more than twice the call's timeout
        return mcp_types.ElicitResult(action="accept", content={"seats": 1})

    pool = await _pool(slow_person, timeout=2)
    try:
        result = await _run(pool, _call())
    finally:
        await pool.close_all()

    assert result == "booked 1 seats, window=False", result


async def test_with_two_calls_in_flight_nobody_is_asked():
    asked = []

    async def elicit(server, call, params):
        asked.append(call.request_id)
        return mcp_types.ElicitResult(action="accept", content={"seats": 2})

    pool = await _pool(elicit)
    try:
        # The first call only sleeps -- in flight, asking nothing -- while the second asks.
        first = asyncio.create_task(_run(pool, _call("run1_001"), "sleep", {"seconds": 3}))
        await asyncio.sleep(1)
        second = await _run(pool, _call("run2_001"))
        await first
    finally:
        await pool.close_all()

    assert second == "not booked: cancel", second
    assert asked == [], "a question reached a person while two runs' calls were in flight"


async def test_one_question_at_a_time_and_a_few_per_call():
    async def yes(server, call, params):
        await asyncio.sleep(0.2)
        return mcp_types.ElicitResult(action="accept", content={"ok": True})

    pool = await _pool(yes)
    try:
        together = await _run(pool, _call(), "ask_twice_at_once")
        many = await _run(pool, _call("run1_002"), "pester", {"times": MAX_QUESTIONS_PER_CALL + 1})
    finally:
        await pool.close_all()

    assert together == "accept,cancel", together
    assert many.split(",") == ["accept"] * MAX_QUESTIONS_PER_CALL + ["cancel"], many


async def test_the_person_answers_field_by_field_through_the_route(monkeypatch):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer", timeout=30)

    async def person():
        seats = await _waiting(broker)
        assert seats.form()["detail"].startswith("1/2: Seats"), seats.form()
        assert "skip" not in [c["value"] for c in seats.form()["choices"]], "a required field offers Skip"
        with pytest.raises(AnswerRejected, match="seats: a whole number"):
            broker.take(seats.id, [], "many")
        with pytest.raises(AnswerRejected, match="an answer, or Decline"):
            broker.take(seats.id, [], "  ")
        with pytest.raises(AnswerRejected, match="is required"):
            broker.take(seats.id, ["skip"], "")
        broker.take(seats.id, [], "4", answered_by="alice")
        window = await _waiting(broker, after={seats.id})
        assert [c["value"] for c in window.form()["choices"]] == ["yes", "no", "skip", "decline"]
        broker.take(window.id, ["yes"], "")

    pool = await _pool(asker.ask)
    try:
        answering = asyncio.create_task(person())
        result = await _run(pool, _call())
        await answering
    finally:
        await pool.close_all()

    assert result == "booked 4 seats, window=True", result


async def test_skip_leaves_an_optional_field_out_and_decline_declines(monkeypatch):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer", timeout=30)

    async def person(last):
        seats = await _waiting(broker)
        broker.take(seats.id, [], "2")
        window = await _waiting(broker, after={seats.id})
        broker.take(window.id, [last], "")

    pool = await _pool(asker.ask)
    try:
        answering = asyncio.create_task(person("skip"))
        skipped = await _run(pool, _call())
        await answering
        answering = asyncio.create_task(person("decline"))
        declined = await _run(pool, _call("run1_002"))
        await answering
    finally:
        await pool.close_all()

    assert skipped == "booked 2 seats, window=False", skipped
    assert declined == "not booked: decline", declined


async def test_the_runs_cancellation_ends_the_question(monkeypatch):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer", timeout=60)
    token = types.SimpleNamespace(is_cancelled=False)

    async def cancel_once_asked():
        await _waiting(broker)
        token.is_cancelled = True

    pool = await _pool(asker.ask)
    try:
        cancelling = asyncio.create_task(cancel_once_asked())
        result = await asyncio.wait_for(_run(pool, _call(token=token)), 30)
        await cancelling
        connection = pool._connections["probe"]
        asking, callers = connection._asking, dict(connection.callers)
    finally:
        await pool.close_all()

    assert result == "not booked: cancel", result
    assert (asking, callers) == (0, {}), "the connection still counts a question or a call"
    assert broker.pending() == []


async def test_outside_an_agents_tool_call_nobody_is_asked():
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer")
    pool = await _pool(asker.ask)
    try:
        result = await pool.call_tool("probe", "book", {})     # no current_external_call
    finally:
        await pool.close_all()

    assert result == "not booked: cancel", result
    assert broker.pending() == []


async def test_a_call_without_its_runs_token_asks_nobody(monkeypatch):
    """Watched or not: a question that the run's cancellation could not end is not put."""
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer", timeout=30)
    call = ExternalCall(request_id="run1_001", session_id="s1", user_id="alice", agent_name="coder", token=None)
    params = types.SimpleNamespace(mode="form", message="How many?", requested_schema={})

    result = await asyncio.wait_for(asker.ask("probe", call, params), 5)

    assert result.action == "cancel" and broker.pending() == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://ok.example/\nFake line", "file:///etc/passwd",
                                 "https://" + "a" * 3000, "https://a/\u202emoc.knab", "https://a\u200b.com",
                                 "https://good.example@evil.example/", "https:///etc/passwd", "https://"])
async def test_an_address_that_is_no_plain_http_one_is_not_shown(monkeypatch, url):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer")

    result = await asker.ask("probe", _call(), types.SimpleNamespace(mode="url", url=url, message="Log in"))

    assert result.action == "cancel" and broker.pending() == []


def _question(spec, required=True):
    return ElicitQuestion(id="q", owner=None, session_id="s", request_id="r", agent_name="a", asked_at=0,
                          timeout=1, answer=None, server="probe", message="Which?", field=("pick", spec),
                          required=required)


def test_one_field_is_answered_by_its_options():
    def choices(spec, required=True):
        return [c["value"] for c in _question(spec, required).form()["choices"]]

    assert choices({"type": "string", "enum": ["red", "blue"]}) == ["#0", "#1", "decline"]
    assert choices({"type": "string", "oneOf": [{"const": "r", "title": "Red"}]}) == ["#0", "decline"]
    assert [c["label"] for c in _question({"type": "string", "oneOf": [{"const": "r", "title": "Red"}]}).form()["choices"]] \
        == ["Red", "Decline"]
    assert choices({"type": "boolean"}, required=False) == ["yes", "no", "skip", "decline"]
    assert choices({"type": "string"}) == ["decline"]


@pytest.mark.parametrize("spec, raw, value", [
    ({"type": "number"}, "2.5", 2.5),
    ({"type": "integer"}, "9007199254740993", 9007199254740993),
    ({"type": "boolean"}, "no", False),
])
def test_an_answer_is_typed_as_the_schema_asks(spec, raw, value):
    assert elicitation._value("x", spec, raw) == value


@pytest.mark.parametrize("spec, raw", [({"type": "number"}, "nan"), ({"type": "number"}, "1e309"),
                                       ({"type": "number"}, "inf"), ({"type": "integer"}, "2.5"),
                                       ({"type": "string", "enum": ["a"]}, "b")])
def test_an_answer_the_schema_cannot_hold_is_refused(spec, raw):
    with pytest.raises(AnswerRejected):
        elicitation._value("x", spec, raw)


async def test_an_address_is_answered_without_content(monkeypatch):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer", timeout=30)

    async def person():
        question = await _waiting(broker)
        assert "https://login.example/x?y=1" in question.form()["detail"]
        broker.take(question.id, ["done"], "")

    answering = asyncio.create_task(person())
    result = await asker.ask("probe", _call(), types.SimpleNamespace(mode="url", url="https://login.example/x?y=1",
                                                                     message="Log in"))
    await answering

    assert result.action == "accept" and result.content is None, result


async def test_a_question_does_not_outlive_its_call(monkeypatch):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer", timeout=120)
    pool = await _pool(asker.ask)
    try:
        result = await _run(pool, _call(), "ask_later")
        for _ in range(100):
            if not broker.pending():
                break
            await asyncio.sleep(0.05)
        connection = pool._connections["probe"]
        left = (broker.pending(), connection._asking, dict(connection._questions))
    finally:
        await pool.close_all()

    assert result == "answered before the question", result
    assert left == ([], 0, {}), f"the question of a finished call waits on: {left}"


def _form(spec, required=True):
    return _question(spec, required).form()


def test_a_multiple_choice_is_offered_as_one():
    spec = {"type": "array", "items": {"enum": ["red", "blue", "green"]}, "maxItems": 2}
    form = _form(spec)

    assert form["multi_select"] is True and [c["value"] for c in form["choices"]] == ["#0", "#1", "#2", "decline"]
    assert [c["label"] for c in form["choices"]][:3] == ["red", "blue", "green"]


async def test_typed_choices_and_lists_come_back_as_the_schema_has_them():
    broker = ElicitBroker()

    async def answer(spec, choices, text=""):
        question = broker.open_question(ElicitQuestion, owner=None, session_id="s", request_id="r", agent_name="a",
                                        timeout=5, server="probe", message="m", field=("pick", spec), required=True)
        broker.take(question.id, choices, text)
        return (await question.answer).content

    assert await answer({"type": "integer", "enum": [1, 2]}, ["#1"]) == {"pick": 2}
    assert await answer({"type": "array", "items": {"anyOf": [{"const": "r", "title": "Red"}, {"const": "b"}]}},
                        ["#1", "#0"]) == {"pick": ["r", "b"]}
    # an option may be called like a button: it is still the option
    assert await answer({"type": "string", "enum": ["accept", "decline"]}, ["#1"]) == {"pick": "decline"}
    assert await answer({"type": "string", "enum": ["retry", "skip"]}, ["#1"]) == {"pick": "skip"}
    assert await answer({"enum": [1, "1"]}, ["#1"]) == {"pick": "1"}
    question = broker.open_question(ElicitQuestion, owner=None, session_id="s", request_id="r", agent_name="a",
                                    timeout=5, server="probe", message="m", required=True,
                                    field=("pick", {"type": "array", "items": {"enum": ["a", "b", "c"]}, "maxItems": 2}))
    with pytest.raises(AnswerRejected, match="at most 2"):
        broker.take(question.id, ["#0", "#1", "#2"], "")
    with pytest.raises(AnswerRejected, match="at least 1"):
        broker.take(question.id, [], "")
    with pytest.raises(AnswerRejected, match="options"):
        broker.take(question.id, ["a"], "")
    broker.close(question)
    optional = broker.open_question(ElicitQuestion, owner=None, session_id="s", request_id="r", agent_name="a",
                                    timeout=5, server="probe", message="m", required=False,
                                    field=("pick", {"type": "array", "items": {"enum": ["a", "b"]}}))
    with pytest.raises(AnswerRejected, match="Skip goes alone"):
        broker.take(optional.id, ["#0", "skip"], "")
    broker.close(optional)


@pytest.mark.parametrize("spec, raw, why", [
    ({"type": "string", "minLength": 5}, "ab", "at least 5"),
    ({"type": "string", "maxLength": 2}, "abc", "at most 2"),
    ({"type": "string", "format": "email"}, "not an address", "email"),
    ({"type": "string", "format": "date"}, "06.11.2026", "date"),
    ({"type": "string", "format": "date"}, "2026-W45-5", "date"),
    ({"type": "string", "format": "date"}, "20261106", "date"),
    ({"type": "string", "format": "date-time"}, "2026-11-06", "date-time"),
    ({"type": "string", "format": "date-time"}, "2026-11-06T25:00:00Z", "date-time"),
    ({"type": "integer", "minimum": 1}, "0", "at least 1"),
    ({"type": "number", "maximum": 10}, "10.5", "at most 10"),
])
def test_the_schemas_limits_are_checked_where_the_person_can_correct(spec, raw, why):
    with pytest.raises(AnswerRejected, match=why):
        elicitation._value("x", spec, raw)


@pytest.mark.parametrize("spec", [
    {"type": "object", "properties": {}},
    {"type": "array", "items": {"type": "string"}},
    {"type": "string", "enum": [f"o{i}" for i in range(elicitation.MAX_OPTIONS + 1)]},
    {"type": "string", "enum": ["x" * (elicitation.MAX_OPTION_CHARS + 1)]},
    {"type": "array", "items": {"enum": [1, 2]}},
])
async def test_a_form_the_chat_cannot_ask_is_not_put(monkeypatch, spec):
    _watched(monkeypatch)
    broker = ElicitBroker()
    asker = Elicitation("mcp_client", broker, "/plugins/mcp_client/answer")
    params = types.SimpleNamespace(mode="form", message="m", requested_schema={"properties": {"f": spec}})

    result = await asyncio.wait_for(asker.ask("probe", _call(), params), 5)

    assert result.action == "cancel" and broker.pending() == []


def test_a_field_shows_its_default():
    assert "(default: 3)" in _form({"type": "integer", "default": 3})["detail"]


@pytest.mark.parametrize("raw", ["2026-11-06T10:00:00Z", "2026-11-06t10:00:00z", "2026-11-06T10:00:00.123+01:00",
                                 "2016-12-31T23:59:60Z"])
def test_an_rfc3339_date_time_is_taken(raw):
    assert elicitation._value("x", {"type": "string", "format": "date-time"}, raw) == raw
