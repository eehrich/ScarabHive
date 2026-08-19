"""AgentCaller — the SAM tool boundary is faked, everything before it is real."""
from __future__ import annotations

import asyncio
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
    failed continue counts under ``follow_up`` (it used to count nowhere)."""
    agent = FakeAgent([
        ok("Error: DNS lookup failed"),
        ok("Cancelled: run aborted"),
    ])
    c = caller(agent)
    assert asyncio.run(c.call_text("x", "task")).startswith("Error: ")
    assert asyncio.run(c.follow_up_text("inst-1", "again")).startswith("Cancelled: ")
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
