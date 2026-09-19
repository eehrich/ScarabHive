"""Tests for the Decisions client.

The wire contract under test, from OpenRouter's OpenAPI document and their own
worked example: POST /api/alpha/decisions with {model, state, questions}, and
{answers, usage} comes back -- one probability for ``noul``, a named option for
``choice``, a point on a scale for ``score``.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.core.cancellation import CancellationToken
from plugins_llm.llm_decisions.openrouter import (
    DECISIONS_URL,
    DecisionsClient,
    DecisionsError,
)

#: OpenRouter's own example for the safety gate: one noul question.
SAFE_TO_RUN = {
    "safe_to_run": {
        "type": "noul",
        "instructions": "Is this action safe to run without a human approving it first?",
        "criteria": {"true": "Reversible or low-impact.", "false": "Destructive or irreversible."},
    },
}
STATE = ('Task: clean up inactive accounts.\n'
         'Proposed tool call: delete_rows(table="customers", where="last_login < 2023-01-01")')
ANSWER = {
    "id": "gen-dec-1789856037-EBZPtgbUSbMjoIdhKokz",
    "model": "typesafe/jev-1.13-20260917",
    "provider": "TypeSafe",
    "answers": {"safe_to_run": {"type": "noul", "noul": 0.05}},
    "usage": {"input_tokens": 384, "output_tokens": 22, "cost": 0.000016128},
}


def _client(**kwargs) -> DecisionsClient:
    defaults = dict(model="~typesafe/jev-latest", api_key="sk-or-test", max_retries=0)
    defaults.update(kwargs)
    return DecisionsClient(**defaults)


def _respond(*responses, record=None):
    """Patch httpx.AsyncClient.post; each call takes the next response."""
    queue = list(responses)

    async def fake_post(self, url, json=None, headers=None):
        if record is not None:
            record.append({"url": url, "json": json, "headers": headers})
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return patch.object(httpx.AsyncClient, "post", fake_post)


_UNSET = object()  # `payload=None` means the body is JSON null, not "use the default"


def _response(status=200, payload=_UNSET, text=None):
    if text is not None:
        return httpx.Response(status, text=text)
    return httpx.Response(status, json=ANSWER if payload is _UNSET else payload)


async def test_the_question_goes_out_and_the_probability_comes_back():
    sent = []
    with _respond(_response(), record=sent):
        result = await _client().decide(STATE, SAFE_TO_RUN, session_id="s-1")

    assert sent[0]["url"] == DECISIONS_URL
    assert sent[0]["headers"]["Authorization"] == "Bearer sk-or-test"
    assert sent[0]["json"] == {"model": "~typesafe/jev-latest", "state": STATE,
                               "questions": SAFE_TO_RUN, "session_id": "s-1"}
    assert result["safe_to_run"].value == 0.05
    assert result.cost == 0.000016128 and result.input_tokens == 384 and result.output_tokens == 22
    # the alias was asked, a dated version answered: the cost belongs to that one
    assert result.model == "typesafe/jev-1.13-20260917" and result.provider == "TypeSafe"


async def test_each_type_keeps_the_field_that_decides():
    """choice and score carry their probabilities; the deciding value is the same field the API names."""
    payload = {
        "model": "typesafe/jev-1.13",
        "answers": {
            "team": {"type": "choice", "choice": "payments", "confidence": 0.75,
                     "probabilities": {"payments": 0.84, "frontend": 0.16, "account": 0}},
            "urgency": {"type": "score", "score": 1.99, "confidence": 0.99,
                        "legend": {"0": "next release", "1": "this week", "2": "now"},
                        "probabilities": {"0": 0, "1": 0.01, "2": 0.99}},
        },
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 2e-05},
    }
    questions = {
        "team": {"type": "choice", "instructions": "Which team?",
                 "criteria": {"payments": "Billing.", "frontend": "Rendering.", "account": "Login."}},
        "urgency": {"type": "score", "instructions": "How urgent?",
                    "criteria": ["next release", "this week", "now"]},
    }
    with _respond(_response(payload=payload)):
        result = await _client().decide("a ticket", questions)

    assert result["team"].value == "payments" and result["team"].confidence == 0.75
    assert result["team"].probabilities["payments"] == 0.84
    assert result["urgency"].value == 1.99 and result["urgency"].legend["2"] == "now"
    assert result["urgency"].confidence == 0.99 and result["urgency"].probabilities["2"] == 0.99
    # a noul answer has neither: the schema makes them optional, so None is the honest value
    with _respond(_response()):
        plain = await _client().decide(STATE, SAFE_TO_RUN)
    assert plain["safe_to_run"].confidence is None and plain["safe_to_run"].probabilities is None


@pytest.mark.parametrize("questions, says", [
    ({}, "at least one question"),
    ({"q": {"type": "guess", "instructions": "?"}}, "type 'guess'"),
    ({"q": {"type": "noul"}}, "no instructions"),
    ({"q": {"type": "score", "instructions": "?", "criteria": {"a": "x", "b": "y"}}}, "ordered list"),
    ({"q": {"type": "score", "instructions": "?", "criteria": ["only one"]}}, "ordered list"),
    ({"q": {"type": "choice", "instructions": "?", "criteria": ["a", "b"]}}, "mapping of at least two"),
    ({"q": {"type": "noul", "instructions": "?", "criteria": ["true", "false"]}}, "criteria is optional"),
    ({"q": "just text"}, "must be a mapping"),
])
async def test_a_question_the_endpoint_would_refuse_never_leaves_the_machine(questions, says):
    """The three types differ in the SHAPE of criteria -- the 400 nobody sees coming."""
    sent = []
    with _respond(_response(), record=sent):
        with pytest.raises(ValueError, match=says):
            await _client().decide("something", questions)
    assert sent == [], "the request went out although the question was malformed"


async def test_the_key_follows_the_endpoint_not_the_provider_name(monkeypatch):
    """A client pointed at another host must not carry the OpenRouter secret there."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "***REMOVED***")

    assert DecisionsClient(model="m").api_key == "***REMOVED***"
    # a proxy: no variable is configured for that host, so it has to bring its own key
    with pytest.raises(ValueError, match="no environment variable is configured"):
        DecisionsClient(model="m", url="https://decisions.example.com/v1/decide")
    named = DecisionsClient(model="m", api_key="sk-proxy", url="https://decisions.example.com/v1/decide")
    assert named.api_key == "sk-proxy"


async def test_a_state_of_nothing_is_refused():
    sent = []
    with _respond(_response(), record=sent):
        for empty in ("", {}, []):
            with pytest.raises(ValueError, match="needs a state"):
                await _client().decide(empty, SAFE_TO_RUN)
        # None is not "empty", it is a different kind of nothing
        with pytest.raises(ValueError, match="not NoneType"):
            await _client().decide(None, SAFE_TO_RUN)
    assert sent == []


async def test_an_answer_this_client_cannot_read_is_reported_not_dropped():
    """A dropped answer reads as "the model did not answer that question"."""
    payload = {"model": "m", "answers": {"safe_to_run": {"type": "vibes", "vibes": 1}}, "usage": {}}
    with _respond(_response(payload=payload)):
        with pytest.raises(DecisionsError, match="vibes"):
            await _client().decide(STATE, SAFE_TO_RUN)


async def test_a_question_left_unanswered_is_an_error_that_names_it():
    """Silence is the one thing a decision may not be -- and result["x"] alone would say KeyError."""
    with _respond(_response(payload={"model": "m", "answers": {}, "usage": {}})):
        with pytest.raises(DecisionsError, match="left safe_to_run unanswered"):
            await _client().decide(STATE, SAFE_TO_RUN)

    two = {**SAFE_TO_RUN, "urgency": {"type": "score", "instructions": "?", "criteria": ["a", "b"]}}
    with _respond(_response()):  # answers only safe_to_run
        with pytest.raises(DecisionsError, match="left urgency unanswered.*answered: safe_to_run"):
            await _client().decide(STATE, two)


@pytest.mark.parametrize("body, says", [
    ({"model": "m", "answers": {"safe_to_run": {"type": "vibes", "vibes": 1}}, "usage": {}}, "no usable value"),
    ({"model": "m", "answers": {"safe_to_run": {"type": "noul"}}, "usage": {}}, "no usable value"),
    ({"model": "m", "answers": {"safe_to_run": {"type": "noul", "noul": None}}, "usage": {}}, "no usable value"),
])
async def test_an_answer_without_a_usable_value_is_reported(body, says):
    """Unknown type, missing field, present-but-null: all three, none of them guessed."""
    with _respond(_response(payload=body)):
        with pytest.raises(DecisionsError, match=says):
            await _client().decide(STATE, SAFE_TO_RUN)


@pytest.mark.parametrize("body, says", [
    ("null", "with NoneType, not an object"),
    ("[1, 2]", "with list, not an object"),
    ('"a string"', "with str, not an object"),
    ("<html>gateway timeout</html>", "with no JSON"),
    ("", "with no JSON"),
])
async def test_a_body_that_is_not_an_answer_is_refused(body, says):
    """`null` parses fine and would only fail deeper, with a message about NoneType."""
    with _respond(_response(text=body)):
        with pytest.raises(DecisionsError, match=says):
            await _client().decide(STATE, SAFE_TO_RUN)


async def test_a_cost_that_arrives_as_text_is_still_a_number():
    """A caller adding costs up must not get a TypeError from one gateway's JSON."""
    body = {"model": "m", "answers": {"safe_to_run": {"type": "noul", "noul": 0.5}},
            "usage": {"input_tokens": "12", "output_tokens": "3", "cost": "0.000016128"}}
    with _respond(_response(payload=body)):
        result = await _client().decide(STATE, SAFE_TO_RUN)
    assert result.cost == 0.000016128 and result.input_tokens == 12 and result.output_tokens == 3


async def test_an_answer_without_usage_still_comes_back():
    with _respond(_response(payload={"model": "m", "answers": {"safe_to_run": {"type": "noul", "noul": 0.9}}})):
        result = await _client().decide(STATE, SAFE_TO_RUN)
    assert result.input_tokens == 0 and result.output_tokens == 0 and result.cost is None


async def test_a_refusal_is_not_tried_again():
    """A 400 is our mistake, not the endpoint's mood -- retrying it only costs time."""
    sent = []
    with _respond(_response(status=400, text='{"error":{"message":"questions[q]: unknown type"}}'), record=sent):
        with pytest.raises(DecisionsError, match="unknown type"):
            await _client(max_retries=2).decide(STATE, SAFE_TO_RUN)
    assert len(sent) == 1, "the request was repeated although the endpoint refused it"


async def test_a_broken_connection_is_tried_again(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda delay: real_sleep(0))
    sent = []
    with _respond(httpx.ConnectError("connection reset"), _response(), record=sent):
        result = await _client(max_retries=1).decide(STATE, SAFE_TO_RUN)
    assert len(sent) == 2 and result["safe_to_run"].value == 0.05


class _Registry:
    """Stands in for the global hook registry and keeps what it was told."""

    def __init__(self):
        self.seen = []

    async def execute_hooks(self, hook_type, context, **kwargs):
        self.seen.append((hook_type.value, context))
        return context


def _watching_hooks():
    registry = _Registry()
    return registry, patch("agent_system.hooks.get_hook_registry", lambda: registry)


async def test_the_call_reaches_the_hooks_with_what_this_api_has():
    """A decision skips the agent's hook wiring, so it dispatches itself -- or the
    debugger never sees a whole class of calls (the bug tts.py names)."""
    registry, watching = _watching_hooks()
    with watching, _respond(_response()):
        await _client().decide(STATE, SAFE_TO_RUN, session_id="s-7")

    kinds = [kind for kind, _ in registry.seen]
    assert kinds == ["pre_llm_request", "post_llm_response"]
    request = registry.seen[0][1]
    assert request.llm_request_payload["questions"] == SAFE_TO_RUN
    assert request.llm_provider == "openrouter_decisions" and request.session_id == "s-7"
    answer = registry.seen[1][1]
    assert answer.llm_response_data["answers"] == {"safe_to_run": 0.05}
    # what TTS has no field for, and this API sends: the tokens, the cost, the backend
    assert answer.llm_usage == {"input_tokens": 384, "output_tokens": 22, "cost": 0.000016128}
    assert answer.metadata["served_by"] == "typesafe/jev-1.13-20260917"
    assert answer.llm_error is None and answer.llm_finish_reason == "stop"


@pytest.mark.parametrize("fail, says", [
    (dict(payload={"model": "m", "answers": {}, "usage": {}}), "unanswered"),
    (dict(status=400, text="no"), "400"),
    (dict(text="<html>"), "no JSON"),
])
async def test_every_way_the_call_can_end_reaches_the_hooks(fail, says):
    """A request the debugger never sees an answer to is how calls went missing before."""
    registry, watching = _watching_hooks()
    with watching, _respond(_response(**fail)):
        with pytest.raises(DecisionsError):
            await _client().decide(STATE, SAFE_TO_RUN)

    kinds = [kind for kind, _ in registry.seen]
    assert kinds[0] == "pre_llm_request" and kinds[-1] == "post_llm_response"
    ended = registry.seen[-1][1]
    assert says in ended.llm_error and ended.llm_finish_reason == "error"


async def test_a_cancel_reaches_the_hooks_too():
    """The user's cancel ends the call -- and the hooks are told, not left hanging."""
    registry, watching = _watching_hooks()
    token = CancellationToken("req-4")
    token.cancel()
    with watching, _respond(_response()):
        with pytest.raises(asyncio.CancelledError):
            await _client().decide(STATE, SAFE_TO_RUN, cancellation_token=token)

    kinds = [kind for kind, _ in registry.seen]
    assert kinds == ["pre_llm_request", "post_llm_response"]
    assert "CancelledError" in registry.seen[-1][1].llm_error


async def test_a_hook_that_throws_cannot_break_a_decision(caplog):
    """And it says so once, above DEBUG: this exact silence hid a bug in the TTS clients."""
    from plugins_llm.llm_decisions import openrouter as module
    module._reported_hook_failures.clear()

    class _Broken:
        async def execute_hooks(self, *args, **kwargs):
            raise ImportError("the registry moved")

    with patch("agent_system.hooks.get_hook_registry", lambda: _Broken()), _respond(_response()):
        with caplog.at_level("WARNING"):
            result = await _client().decide(STATE, SAFE_TO_RUN)

    assert result["safe_to_run"].value == 0.05
    assert any("hook dispatch failed" in r.message for r in caplog.records if r.levelname == "WARNING")


async def test_a_busy_endpoint_is_tried_again(monkeypatch):
    waited = []
    real_sleep = asyncio.sleep  # the patch must not call itself
    monkeypatch.setattr(asyncio, "sleep", lambda delay: (waited.append(delay), real_sleep(0))[1])
    sent = []
    with _respond(_response(status=429, text="rate limited"),
                  _response(status=503, text="upstream"), _response(), record=sent):
        result = await _client(max_retries=2).decide(STATE, SAFE_TO_RUN)

    assert len(sent) == 3 and result["safe_to_run"].value == 0.05
    assert waited == [2.0, 4.0], "the wait between attempts does not grow"


async def test_a_state_the_endpoint_cannot_take_is_refused():
    """A number is not content: the 400 would name the field, not the mistake."""
    sent = []
    with _respond(_response(), record=sent):
        with pytest.raises(ValueError, match="not int"):
            await _client().decide(42, SAFE_TO_RUN)
    assert sent == []


async def test_a_cancel_during_the_wait_between_attempts_ends_the_call():
    """Cancelled while the client is waiting to try again: that must not sit out the delay."""
    token = CancellationToken("req-3")
    waiting = asyncio.Event()
    real_sleep = asyncio.sleep

    async def watched_sleep(delay):
        if delay >= 1:  # the backoff, not an internal yield
            waiting.set()
            await real_sleep(30)
        await real_sleep(0)

    with _respond(_response(status=429, text="rate limited"), _response()):
        with patch.object(asyncio, "sleep", watched_sleep):
            call = asyncio.ensure_future(
                _client(max_retries=1).decide(STATE, SAFE_TO_RUN, cancellation_token=token))
            await waiting.wait()
            token.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, timeout=5)


async def test_a_cancelled_run_sends_nothing():
    token = CancellationToken("req-1")
    token.cancel()
    sent = []
    with _respond(_response(), record=sent):
        with pytest.raises(asyncio.CancelledError):
            await _client().decide(STATE, SAFE_TO_RUN, cancellation_token=token)
    assert sent == [], "the request went out although the run was already cancelled"


async def test_a_cancel_in_flight_ends_the_call():
    """The user cancels while the endpoint is thinking: that is a cancel, not a failure."""
    token = CancellationToken("req-2")
    started = asyncio.Event()

    async def slow_post(self, url, json=None, headers=None):
        started.set()
        await asyncio.sleep(30)  # only the cancel can end this

    with patch.object(httpx.AsyncClient, "post", slow_post):
        call = asyncio.ensure_future(_client().decide(STATE, SAFE_TO_RUN, cancellation_token=token))
        await started.wait()
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=5)
