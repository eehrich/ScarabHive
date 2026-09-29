"""AgentCaller — the SAM tool boundary is faked, everything before it is real."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from pydantic import BaseModel

from agent_system.core.agent_caller import AgentCaller, parse_json_object, parse_json_value
from agent_system.core.cancellation import CancellationToken


class FakeAgent:
    """Records every SAM call; answers from a scripted queue."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> Any:
        self.calls.append((tool_name, params))
        return self.replies.pop(0)


def ok(text: str, instance_id: str = "inst-1") -> dict[str, Any]:
    return {"status": "completed", "instance_id": instance_id, "result": text}


def caller(agent: FakeAgent, **kw: Any) -> AgentCaller:
    return AgentCaller(
        agent, sam_instance="t_sam", session_id="s1", user_id="u1", request_id="r1", **kw,
    )


class Verdict(BaseModel):
    score: int
    issues: list[str]


def test_call_returns_dict_and_forwards_context() -> None:
    agent = FakeAgent([ok('{"a": 1}')])
    c = caller(agent)
    assert asyncio.run(c.call("checker", "task")) == {"a": 1}
    tool, params = agent.calls[0]
    assert tool == "t_sam_manage_sub_agent"
    assert params["operation"] == "create"
    assert params["agent_type"] == "checker"
    assert (params["_session_id"], params["_user_id"], params["_request_id"]) == ("s1", "u1", "r1")
    assert params["_agent"] is agent
    assert c.last_instance_id == "inst-1"
    assert c.counters["checker"] == 1 and c.counters["total"] == 1


def test_schema_failure_is_fed_back_once_to_the_same_instance() -> None:
    agent = FakeAgent([
        ok('{"score": "hoch", "issues": []}', instance_id="inst-7"),
        ok('{"score": 4, "issues": ["x"]}'),
    ])
    c = caller(agent)
    result = asyncio.run(c.call("judge", "task", schema=Verdict))
    assert result == Verdict(score=4, issues=["x"])
    assert len(agent.calls) == 2, "expected exactly one follow-up"
    _, follow = agent.calls[1]
    assert follow["operation"] == "continue"
    assert follow["instance_id"] == "inst-7"
    assert "score" in follow["message"], "feedback must name the failing field"
    assert c.counters["schema_retries"] == 1


def test_schema_failure_twice_counts_as_failed_attempt_and_retries_fresh() -> None:
    agent = FakeAgent([
        ok('{"score": "a", "issues": []}', instance_id="i1"),
        ok('{"score": "b", "issues": []}'),          # follow-up still invalid
        ok('{"score": 1, "issues": []}', instance_id="i2"),  # fresh attempt
    ])
    c = caller(agent, retries=1)
    result = asyncio.run(c.call("judge", "task", schema=Verdict))
    assert result.score == 1
    ops = [p["operation"] for _, p in agent.calls]
    assert ops == ["create", "continue", "create"]


def test_error_status_retries_then_raises() -> None:
    agent = FakeAgent([
        {"status": "error", "error": "boom"},
        {"status": "limit_reached", "error": "full"},
    ])
    c = caller(agent, retries=1)
    with pytest.raises(RuntimeError, match="full"):
        asyncio.run(c.call("x", "task"))
    assert len(agent.calls) == 2


def test_model_fallback_runs_once_with_advanced_model() -> None:
    agent = FakeAgent([
        {"status": "error", "error": "e1"},
        ok('{"ok": true}'),
    ])
    c = caller(agent, retries=0, model_fallback=True)
    assert asyncio.run(c.call("x", "task")) == {"ok": True}
    assert "use_advanced_model" not in agent.calls[0][1]
    assert agent.calls[1][1]["use_advanced_model"] is True


def test_failed_as_content_is_counted_and_retried() -> None:
    agent = FakeAgent([ok("Error: DNS lookup failed"), ok('{"b": 2}')])
    c = caller(agent, retries=1)
    assert asyncio.run(c.call("x", "task")) == {"b": 2}
    assert c.counters["transport_failures"] == 1
    assert c.counters["transport_failures:x"] == 1


def test_text_level_calls_count_transport_failures_too() -> None:
    """The count lives at the transport seam (_create/follow_up_text), not
    in call(): text-level users — the v4 wrappers — must be covered, and a
    failed continue counts under ``follow_up`` (it used to count nowhere).

    Both seams now REFUSE the failure as well as counting it. The counting is
    the half that has to survive the refusal: it is what tells an unhealthy
    run from a quiet one afterwards, and what distinguishes "the book was
    clean" from "the reviewer died".
    """
    agent = FakeAgent([
        ok("Error: DNS lookup failed"),
        ok("Cancelled: run aborted"),
    ])
    c = caller(agent)
    with pytest.raises(RuntimeError, match="did not answer"):
        asyncio.run(c.call_text("x", "task"))
    with pytest.raises(RuntimeError, match="did not answer"):
        asyncio.run(c.follow_up_text("inst-1", "again"))
    assert c.counters["transport_failures"] == 2
    assert c.counters["transport_failures:x"] == 1
    assert c.counters["transport_failures:follow_up"] == 1


def test_empty_result_is_an_error() -> None:
    agent = FakeAgent([ok("   ")])
    c = caller(agent, retries=0)
    with pytest.raises(RuntimeError, match="empty"):
        asyncio.run(c.call_text("x", "task"))


def test_cancellation_is_checked_before_each_attempt() -> None:
    token = CancellationToken("r1")
    token.cancel()
    agent = FakeAgent([ok("{}")])
    c = caller(agent, cancellation_token=token)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(c.call("x", "task"))
    assert agent.calls == [], "must not spawn after cancel"


def test_follow_up_uses_continue_and_parses() -> None:
    agent = FakeAgent([ok('{"n": 1}')])
    c = caller(agent)
    assert asyncio.run(c.follow_up("inst-9", "again")) == {"n": 1}
    _, p = agent.calls[0]
    assert p["operation"] == "continue" and p["instance_id"] == "inst-9" and p["message"] == "again"


def test_progress_hook_receives_start_and_end() -> None:
    lines: list[str] = []

    async def note(s: str) -> None:
        lines.append(s)

    agent = FakeAgent([ok("{}")])
    asyncio.run(caller(agent, progress=note).call("writer", "t"))
    assert [ln[0] for ln in lines] == ["▶", "✓"]


# -- parsing ------------------------------------------------------------------

def test_parse_html_escaped_json_is_healed() -> None:
    text = "<pre><code>{&quot;issues&quot;: [{&quot;code&quot;: &quot;X1&quot;}]}</code></pre>"
    assert parse_json_object(text) == {"issues": [{"code": "X1"}]}


def test_parse_html_paragraph_form_is_healed() -> None:
    text = '<p>{<br>  "a": 1<br>}</p>'
    assert parse_json_object(text) == {"a": 1}


def test_parse_json_in_prose_and_fences() -> None:
    assert parse_json_object('Here you go:\n```json\n{"a": 1}\n```\nDone.') == {"a": 1}


def test_parse_array_first_object_or_error() -> None:
    assert parse_json_object('[{"a": 1}, {"a": 2}]') == {"a": 1}
    assert parse_json_value('[1, 2]') == [1, 2]
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
    with pytest.raises(ValueError):
        parse_json_object("no json here at all")


# -- review findings pinned ------------------------------------------------------

def test_cancelled_status_is_a_failure() -> None:
    agent = FakeAgent([{"status": "cancelled", "instance_id": "i", "result": "Cancelled: user"}])
    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(caller(agent, retries=0).call("x", "t"))


def test_follow_up_retries_on_bad_json_and_empty() -> None:
    agent = FakeAgent([
        {"status": "completed", "result": ""},
        {"status": "completed", "result": "not json"},
        {"status": "completed", "result": '{"ok": 1}'},
    ])
    c = caller(agent, retries=2)
    assert asyncio.run(c.follow_up("inst", "again")) == {"ok": 1}
    assert len(agent.calls) == 3
    assert all(p["operation"] == "continue" for _, p in agent.calls)


def test_counters_mapping_is_shared_with_the_run() -> None:
    shared: dict[str, int] = {}
    agent = FakeAgent([ok("Error: net"), ok("{}")])
    asyncio.run(caller(agent, counters=shared, retries=1).call("x", "t"))
    assert shared["total"] == 1 and shared["transport_failures"] == 1


def test_negative_retries_still_make_one_attempt() -> None:
    agent = FakeAgent([{"status": "error", "error": "boom"}])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(caller(agent, retries=-3).call("x", "t"))


def test_concurrent_calls_feed_schema_errors_to_their_own_instance() -> None:
    """Two calls under gather. A's create returns first, then A yields (progress
    hook) and B's create returns — so ``last_instance_id`` is B's when A
    validates. A's schema feedback must still go to A's own instance."""

    box: dict[str, AgentCaller] = {}

    class ScriptedAgent:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.a_done = asyncio.Event()

        async def call_tool(self, tool_name: str, params: dict[str, Any]) -> Any:
            self.calls.append(params)
            if params["operation"] == "create":
                if params["agent_type"] == "A":
                    self.a_done.set()
                    return ok('{"score": "bad", "issues": []}', instance_id="inst-A")
                await self.a_done.wait()
                return ok('{"score": 2, "issues": []}', instance_id="inst-B")
            # Precondition of this test: by the time A asks for its follow-up,
            # B's create has already overwritten the shared field. Without this
            # the assertion below would hold trivially.
            assert box["c"].last_instance_id == "inst-B", "interleaving did not happen"
            return ok('{"score": 1, "issues": []}', instance_id=params["instance_id"])

    async def yield_once(_: str) -> None:
        await asyncio.sleep(0)

    agent = ScriptedAgent()
    c = caller(agent, progress=yield_once)  # type: ignore[arg-type]
    box["c"] = c

    async def run() -> tuple[Any, Any]:
        return await asyncio.gather(
            c.call("A", "t", schema=Verdict), c.call("B", "t", schema=Verdict),
        )

    a, b = asyncio.run(run())
    assert (a.score, b.score) == (1, 2)
    follow_ups = [p for p in agent.calls if p["operation"] == "continue"]
    assert [p["instance_id"] for p in follow_ups] == ["inst-A"]


class TestParseJsonValueRepairGuard:
    """Review 2026-08-19 (v4 switch to the core parser): repair_json extracts
    dict-free number lists out of prose ("see items [1] and [2]") — accepting
    those as a result means no retry and a silent "0 findings"."""

    def test_repaired_dict_free_list_raises(self):
        with pytest.raises(ValueError):
            parse_json_value("I checked the scene. See items [1] and [2] in the text.")

    def test_repaired_list_with_objects_passes_through(self):
        value = parse_json_value('Findings: [{"code": "W1"}, {"code": "W2"}] done.')
        assert isinstance(value, list) and len(value) == 2

    def test_bare_dict_free_array_still_valid_via_fast_path(self):
        assert parse_json_value("[1, 2, 3]") == [1, 2, 3]


class TestAFailureIsNeverAResult:
    """The transport seam refuses it, so no caller can persist it.

    ``ok()`` is the production shape: the manager labels a failed run
    ``status="completed"`` and puts the failure in ``result``, so the status
    check waves it through. The text callers have no retry
    (``_call_sub_agent`` is a single attempt) and write what they get --
    ``phase_4_5_finalize`` into ``books.metadata["book_summary"]`` and the
    series state, ``phase_3_prosa`` into ``chapters.metadata.$.summary``.
    One 429 and the transport's complaint is a book's published summary.
    """

    FAILURE = "Error: Agent execution failed: Error code: 429 - {'error': {'x': 1}}"

    @pytest.mark.asyncio
    async def test_call_text_refuses_it_instead_of_returning_it(self):
        agent = FakeAgent([ok(self.FAILURE)])

        with pytest.raises(RuntimeError, match="did not answer"):
            await caller(agent).call_text("v4_book_summarizer", "fasse zusammen")

    @pytest.mark.asyncio
    async def test_follow_up_text_refuses_it_too(self):
        agent = FakeAgent([ok("{}"), ok(self.FAILURE)])
        c = caller(agent)
        await c.call_text("v4_x", "erst ein guter Lauf")

        with pytest.raises(RuntimeError, match="did not answer"):
            await c.follow_up_text("inst-1", "und jetzt weiter")

    @pytest.mark.asyncio
    async def test_the_structured_path_retries_it(self):
        """``call()`` wraps the attempt, so the raise becomes a retry -- which
        is what a transient network fault deserves. Second attempt answers."""
        agent = FakeAgent([ok(self.FAILURE), ok('{"score": 1, "issues": []}')])

        result = await caller(agent, retries=1).call("v4_x", "task", schema=Verdict)

        assert result.score == 1
        assert len(agent.calls) == 2, "the failure was not retried"

    @pytest.mark.asyncio
    async def test_a_real_answer_is_untouched(self):
        """The guard is narrow: only the two markers, and only at the START.

        The witness has to CONTAIN the marker without beginning with it, or
        the boundary is untested -- an earlier version used "Error codes are
        covered in chapter 3.", which has no colon at all, and the mutation
        "match the marker anywhere" stayed green. A chapter summary quoting a
        machine is the ordinary case, not a contrived one.
        """
        summary = 'Kapitel 3: Der Funk meldet "Error: Kein Signal", und Nele stutzt.'
        agent = FakeAgent([ok(summary)])

        text = await caller(agent).call_text("v4_chapter_summarizer", "task")

        assert text == summary


class TestATransportFailureIsNotAFormatProblem:
    """A sub-agent that never answered hands its failure back AS its answer.

    Measured on the server, book 77 (2026-09-04 21:21:45): a model burned
    131_072 output tokens, 131_070 of them on reasoning, and produced nothing.
    ``_count_transport_failure`` logged "did NOT answer (transport/lifecycle,
    NOT a format problem)" -- and one millisecond later the parser logged
    "JSON parse failed after all strategies". What reached the pipeline, and
    the operator reading it, was "Could not parse JSON from sub-agent": the
    wrong end of the problem.
    """

    #: verbatim from logs/api.log, the whole message -- an earlier version of
    #: this witness quoted only its first sentence and still called itself
    #: verbatim
    REAL = ("Error: LLM hit its output token limit without producing any content "
            "(finish_reason=length, completion_tokens=131072, of which "
            "reasoning=131070). The model exhausted its budget before answering "
            "— lower the thinking/reasoning level, raise max_tokens, or use "
            "a different model.")

    #: The reason the guard is the FIRST thing the parser does. Every one of
    #: these carries a brace span, so ``repair_json`` used to return the
    #: provider's error body as if the sub-agent had answered it -- and the
    #: caller's ``parsed.get("issues", [])`` then read "0 findings" with no
    #: exception and no retry. Sources: sub_agent_manager/server.py wraps the
    #: agent's message, servers/agent/server.py stringifies the provider
    #: exception, and provider SDKs stringify with their JSON body.
    ERROR_BODIES_THAT_USED_TO_PARSE = [
        "Error: Agent execution failed: Error code: 429 - "
        "{'error': {'message': 'Rate limit', 'type': 'rate_limit_error'}}",
        'Error: LLM call failed after 3 retries: {"type": "overloaded_error"}',
        'Cancelled: [{"issue": 1}]',
    ]

    def test_the_message_names_the_cause_not_the_json(self):
        with pytest.raises(ValueError) as excinfo:
            parse_json_value(self.REAL)

        message = str(excinfo.value)
        assert "did not answer" in message, message
        assert "token limit" in message, "the cause itself has to survive into the message"
        assert "parse JSON" not in message, message

    def test_it_does_not_log_a_parse_failure(self, caplog):
        """The misleading half: a warning that contradicts the one two
        functions up, in the same millisecond."""
        with caplog.at_level(logging.WARNING, logger="agent_system.core.agent_caller"):
            with pytest.raises(ValueError):
                parse_json_value(self.REAL)

        assert not [r for r in caplog.records if "JSON parse failed" in r.getMessage()],             [r.getMessage() for r in caplog.records]

    def test_a_real_parse_failure_still_says_so(self):
        """The other way, or the branch above would swallow the case it is
        named after. Text that is simply not JSON keeps its own message."""
        with pytest.raises(ValueError, match="Could not parse JSON"):
            parse_json_value("the reviewer thought about it and gave up")

    @pytest.mark.parametrize("body", ERROR_BODIES_THAT_USED_TO_PARSE)
    def test_an_error_body_is_never_read_as_an_answer(self, body):
        """The placement test, and the one that measures the real damage.

        Move the guard back behind the parse attempts and every one of these
        comes out as a dict again -- silently, because a transport failure
        that PARSES raises nothing, retries nothing, and reads downstream as
        an empty finding list. The version of this guard that sat at the end
        of the function caught only the brace-free minority.
        """
        with pytest.raises(ValueError, match="did not answer"):
            parse_json_value(body)

    @pytest.mark.parametrize("answer,expected", [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"b": 2}\n```', {"b": 2}),
        ("Here it is: {\"c\": 3}, done.", {"c": 3}),
        ('[{"d": 4}]', [{"d": 4}]),
    ])
    def test_an_early_return_must_not_swallow_a_real_answer(self, answer, expected):
        """The other direction. The guard is now the first thing the parser
        does, so it has to be narrow: only the two markers the sub-agent
        manager itself prepends, nothing that merely mentions an error."""
        assert parse_json_value(answer) == expected

    def test_an_answer_that_only_talks_about_errors_still_parses(self):
        """A reviewer reporting on errors is not a failed reviewer -- the
        marker has to be at the START, not anywhere in the text."""
        assert parse_json_value('{"issues": ["Error: the ship sank twice"]}') == {
            "issues": ["Error: the ship sank twice"]
        }


def test_a_schema_call_hands_the_sam_exactly_what_it_always_did() -> None:
    """Structured output (F11) is opt-in for the run, and AgentCaller does not opt in: its schema
    stays a prompt-and-validate contract. What reaches the SAM for a schema call -- and so what the
    sub-agent's run is started with -- is the same key set as before F11, create and follow-up."""
    agent = FakeAgent([
        ok('{"score": "hoch", "issues": []}', instance_id="inst-7"),
        ok('{"score": 4, "issues": []}'),
    ])
    asyncio.run(caller(agent).call("judge", "task", schema=Verdict))
    (_, create), (_, follow) = agent.calls
    runtime = {"_session_id", "_user_id", "_request_id", "_agent"}
    assert set(create) == {"operation", "agent_type", "task", "blocking"} | runtime
    assert set(follow) == {"operation", "instance_id", "message"} | runtime
