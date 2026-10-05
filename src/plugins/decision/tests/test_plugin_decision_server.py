"""Tests for the Decision Tool Server Batch Capabilities."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.decision.server import DecisionServer
from plugins.llm_decisions.system_one import Answer, DecisionsResult


def _make_server(**server_cfg_kwargs) -> DecisionServer:
    system_cfg = MagicMock(spec=AgentSystemConfig)
    server_cfg = MagicMock(spec=ToolServerConfig)
    for k, v in server_cfg_kwargs.items():
        setattr(server_cfg, k, v)
    return DecisionServer("decision", system_cfg, server_cfg)


async def _run_tool(server: DecisionServer, tool_name: str, params: dict):
    bus = get_status_bus()
    method = (
        tool_name[len(server.name) + 1 :]
        if tool_name.startswith(server.name + "_")
        else tool_name
    )
    queue = await bus.subscribe(server=f"{server.name}.{method}()")
    try:
        result = await server.call_with_status(tool_name, params)
    finally:
        bus.unsubscribe(queue)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert (
        len(closing) == 1
    ), f"{tool_name}: expected exactly one closing event, got {[(e.phase, e.message) for e in events]}"
    return result, closing[0]


@pytest.mark.asyncio
async def test_server_init_config_defaults():
    server = _make_server()
    assert server.decision_profile is None
    assert server.max_batch_size == 250
    assert server.max_concurrency == 10
    assert server.max_questions == 20
    assert server.max_context_length == 50000
    assert server.default_scale == ["1", "2", "3", "4", "5"]


@pytest.mark.asyncio
async def test_server_init_config_overrides():
    server = _make_server(
        decision_profile="custom_jev",
        max_batch_size=50,
        max_concurrency=4,
        max_questions=5,
        max_context_length=1000,
        default_scale=["low", "high"],
    )
    assert server.decision_profile == "custom_jev"
    assert server.max_batch_size == 50
    assert server.max_concurrency == 4
    assert server.max_questions == 5
    assert server.max_context_length == 1000
    assert server.default_scale == ["low", "high"]


@pytest.mark.asyncio
async def test_evaluate_probabilities_batch_happy_path():
    server = _make_server()
    mock_client = MagicMock()

    async def fake_decide(state, questions, **kwargs):
        # Return probability depending on state content
        val = 0.95 if "delete" in state else 0.15
        return DecisionsResult(
            answers={
                "is_risky": Answer(name="is_risky", type="noul", value=val),
            },
            model="typesafe/jev-1.13",
            provider="TypeSafe",
            id="dec_batch",
            input_tokens=100,
            output_tokens=1,
            cost=0.00001,
            duration_ms=20.0,
        )

    mock_client.decide = AsyncMock(side_effect=fake_decide)
    server._client = mock_client
    server._cached_profile = None

    params = {
        "items": [
            {"id": "cmd_1", "context": "delete temporary cache files"},
            {"id": "cmd_2", "context": "list current directory files"},
        ],
        "questions": [
            {
                "id": "is_risky",
                "question": "Is this action destructive or risky?",
            }
        ],
    }

    result, closing = await _run_tool(server, "decision_evaluate_probabilities", params)

    assert result["status"] == "success"
    assert "results" in result
    assert result["results"]["cmd_1"]["is_risky"] == 0.95
    assert result["results"]["cmd_2"]["is_risky"] == 0.15
    assert result["summary"]["total_items"] == 2
    assert result["summary"]["successful_items"] == 2
    assert result["summary"]["failed_items"] == 0
    assert result["summary"]["total_input_tokens"] == 200
    assert result["summary"]["model"] == "typesafe/jev-1.13"

    assert closing.phase is StatusPhase.END
    assert "2 items evaluated" in closing.message
    assert len(closing.message) <= 140


@pytest.mark.asyncio
async def test_evaluate_probabilities_single_item_backward_compatibility():
    server = _make_server()
    mock_client = MagicMock()
    fake_result = DecisionsResult(
        answers={"q1": Answer(name="q1", type="noul", value=0.88)},
        model="typesafe/jev-1.13",
        provider="TypeSafe",
        id="d1",
        input_tokens=50,
        output_tokens=1,
        cost=0.000005,
        duration_ms=10.0,
    )
    mock_client.decide = AsyncMock(return_value=fake_result)
    server._client = mock_client
    server._cached_profile = None

    # Calling with context instead of items
    params = {
        "context": "Single context text to evaluate",
        "questions": ["Is this valid?"],
    }
    result, closing = await _run_tool(server, "decision_evaluate_probabilities", params)

    assert result["status"] == "success"
    assert result["summary"]["total_items"] == 1
    assert result["results"]["item_1"]["q1"] == 0.88
    # Backward compatibility convenience field
    assert result["probabilities"]["q1"] == 0.88
    assert closing.phase is StatusPhase.END


@pytest.mark.asyncio
async def test_evaluate_probabilities_plain_string_items():
    server = _make_server()
    mock_client = MagicMock()
    fake_result = DecisionsResult(
        answers={"q1": Answer(name="q1", type="noul", value=0.5)},
        model="typesafe/jev-1.13",
        provider="TypeSafe",
        id="d1",
        input_tokens=50,
        output_tokens=1,
        cost=0.000005,
        duration_ms=10.0,
    )
    mock_client.decide = AsyncMock(return_value=fake_result)
    server._client = mock_client
    server._cached_profile = None

    params = {
        "items": ["First item text", "Second item text"],
        "questions": ["Is this a test?"],
    }
    result, closing = await _run_tool(server, "decision_evaluate_probabilities", params)

    assert result["status"] == "success"
    assert "item_1" in result["results"]
    assert "item_2" in result["results"]
    assert result["summary"]["total_items"] == 2


@pytest.mark.asyncio
async def test_evaluate_probabilities_partial_success():
    server = _make_server()
    mock_client = MagicMock()

    async def decide_with_partial_fail(state, questions, **kwargs):
        if "error_trigger" in state:
            raise RuntimeError("API timeout on this specific document")
        return DecisionsResult(
            answers={"q1": Answer(name="q1", type="noul", value=0.75)},
            model="typesafe/jev-1.13",
            provider="TypeSafe",
            id="d1",
            input_tokens=50,
            output_tokens=1,
            cost=0.000005,
            duration_ms=10.0,
        )

    mock_client.decide = AsyncMock(side_effect=decide_with_partial_fail)
    server._client = mock_client
    server._cached_profile = None

    params = {
        "items": [
            {"id": "doc_ok_1", "context": "Normal document 1"},
            {"id": "doc_bad", "context": "This has error_trigger in it"},
            {"id": "doc_ok_2", "context": "Normal document 2"},
        ],
        "questions": ["Is it clear?"],
    }
    result, closing = await _run_tool(server, "decision_evaluate_probabilities", params)

    assert result["status"] == "partial_success"
    assert result["summary"]["total_items"] == 3
    assert result["summary"]["successful_items"] == 2
    assert result["summary"]["failed_items"] == 1
    assert "doc_ok_1" in result["results"]
    assert "doc_ok_2" in result["results"]
    assert "doc_bad" in result["errors"]
    assert "API timeout" in result["errors"]["doc_bad"]

    assert closing.phase is StatusPhase.END
    assert "2/3 items evaluated (1 failed)" in closing.message


@pytest.mark.asyncio
async def test_evaluate_probabilities_all_fail():
    server = _make_server()
    mock_client = MagicMock()
    mock_client.decide = AsyncMock(side_effect=RuntimeError("OpenRouter 503 Outage"))
    server._client = mock_client
    server._cached_profile = None

    params = {
        "items": [
            {"id": "doc1", "context": "First document"},
            {"id": "doc2", "context": "Second document"},
        ],
        "questions": ["Is it valid?"],
    }
    result, closing = await _run_tool(server, "decision_evaluate_probabilities", params)

    assert result["status"] == "error"
    assert "All 2 items failed" in result["error"]
    assert "errors" in result
    assert closing.phase is StatusPhase.ERROR


@pytest.mark.asyncio
async def test_evaluate_scores_batch_happy_path():
    server = _make_server()
    mock_client = MagicMock()

    async def fake_score_decide(state, questions, **kwargs):
        val = 4.5 if "clean" in state else 2.0
        return DecisionsResult(
            answers={
                "cleanliness": Answer(
                    name="cleanliness",
                    type="score",
                    value=val,
                    confidence=0.9,
                    probabilities={"0": 0.0, "1": 0.1, "2": 0.9},
                )
            },
            model="typesafe/jev-1.13",
            provider="TypeSafe",
            id="dec_score_batch",
            input_tokens=120,
            output_tokens=2,
            cost=0.000012,
            duration_ms=25.0,
        )

    mock_client.decide = AsyncMock(side_effect=fake_score_decide)
    server._client = mock_client
    server._cached_profile = None

    params = {
        "items": [
            {"id": "code_clean", "context": "clean and modular python code"},
            {"id": "code_messy", "context": "messy monolithic script"},
        ],
        "criteria": [
            {
                "id": "cleanliness",
                "question": "Rate the code cleanliness and modularity",
                "scale": ["poor", "acceptable", "excellent"],
            }
        ],
        "include_details": True,
    }

    result, closing = await _run_tool(server, "decision_evaluate_scores", params)

    assert result["status"] == "success"
    assert result["results"]["code_clean"]["cleanliness"] == 4.5
    assert result["results"]["code_messy"]["cleanliness"] == 2.0
    assert result["details"]["code_clean"]["cleanliness"]["scale"] == [
        "poor",
        "acceptable",
        "excellent",
    ]
    assert result["summary"]["total_items"] == 2
    assert closing.phase is StatusPhase.END
    assert "2 items evaluated" in closing.message


TWO_ITEMS = [{"id": "a", "context": "one"}, {"id": "b", "context": "two"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, params, answer", [
    ("decision_evaluate_probabilities", {"items": TWO_ITEMS, "questions": ["Is it valid?"]},
     Answer(name="q1", type="noul", value=0.5)),
    ("decision_evaluate_scores",
     {"items": TWO_ITEMS, "criteria": [{"id": "q1", "question": "How good?", "scale": ["low", "high"]}]},
     Answer(name="q1", type="score", value=1.0)),
])
async def test_a_cost_the_host_did_not_report_is_unknown_not_zero(tool, params, answer):
    """A local Laya and TypeSafe direct answer without a cost (DecisionsResult:
    None is not free). Summing the known part would tell the agent the batch
    cost less than it did -- or nothing at all."""

    async def run(costs):
        server = _make_server()
        remaining = list(costs)

        async def decide(state, questions, **kwargs):
            return DecisionsResult(answers={"q1": answer}, model="m", provider=None, id=None,
                                   input_tokens=10, output_tokens=0, cost=remaining.pop(),
                                   duration_ms=1.0)

        server._client = MagicMock(decide=AsyncMock(side_effect=decide))
        server._cached_profile = None
        result, _ = await _run_tool(server, tool, params)
        assert result["summary"]["successful_items"] == 2, result
        return result["summary"]["total_cost"]

    assert await run([0.00002, None]) is None
    # control: every answer priced, and the sum stands
    assert await run([0.00002, 0.00003]) == pytest.approx(0.00005)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, params", [
    ("decision_evaluate_probabilities", {"items": TWO_ITEMS, "questions": ["Is it valid?"]}),
    ("decision_evaluate_scores",
     {"items": TWO_ITEMS, "criteria": [{"id": "q1", "question": "How good?", "scale": ["low", "high"]}]}),
])
async def test_an_answer_the_client_refused_is_still_part_of_the_spend(tool, params):
    """Item b's answer was billed and then refused (a question left
    unanswered): the item fails, its cost does not vanish. A failure that
    never reached an answer carries no usage and adds nothing."""
    from plugins.llm_decisions.system_one import DecisionsError

    async def run(failure):
        server = _make_server()

        async def decide(state, questions, **kwargs):
            if state == "two":
                raise failure
            return DecisionsResult(answers={"q1": Answer(name="q1", type="noul", value=0.5)},
                                   model="m", provider=None, id=None, input_tokens=10,
                                   output_tokens=1, cost=0.00002, duration_ms=1.0)

        server._client = MagicMock(decide=AsyncMock(side_effect=decide))
        server._cached_profile = None
        result, _ = await _run_tool(server, tool, params)
        assert result["status"] == "partial_success", result
        return result["summary"]

    billed = await run(DecisionsError("left q1 unanswered",
                                      usage={"input_tokens": 30, "output_tokens": 0, "cost": 0.00001}))
    assert billed["total_cost"] == pytest.approx(0.00003)
    assert (billed["total_input_tokens"], billed["total_output_tokens"]) == (40, 1)
    # control: nothing answered, nothing billed
    unbilled = await run(DecisionsError("Decisions call failed: ConnectError"))
    assert unbilled["total_cost"] == pytest.approx(0.00002) and unbilled["total_input_tokens"] == 10


@pytest.mark.asyncio
async def test_items_validation_errors():
    server = _make_server(max_batch_size=2)

    # Empty items
    res, closing = await _run_tool(
        server, "decision_evaluate_probabilities", {"items": [], "questions": ["Q1?"]}
    )
    assert res["status"] == "error"
    assert "non-empty list" in res["error"]
    assert closing.phase is StatusPhase.ERROR

    # Exceeds max_batch_size
    res, closing = await _run_tool(
        server,
        "decision_evaluate_probabilities",
        {"items": ["A", "B", "C"], "questions": ["Q1?"]},
    )
    assert res["status"] == "error"
    assert "exceeds maximum allowed" in res["error"]
    assert closing.phase is StatusPhase.ERROR

    # Duplicate item ID
    res, closing = await _run_tool(
        server,
        "decision_evaluate_probabilities",
        {
            "items": [
                {"id": "dup", "context": "Text 1"},
                {"id": "dup", "context": "Text 2"},
            ],
            "questions": ["Q1?"],
        },
    )
    assert res["status"] == "error"
    assert "Duplicate item ID 'dup'" in res["error"]
    assert closing.phase is StatusPhase.ERROR

    # Invalid item format (not string or dict)
    res, closing = await _run_tool(
        server,
        "decision_evaluate_probabilities",
        {"items": [12345], "questions": ["Q1?"]},
    )
    assert res["status"] == "error"
    assert "must be a string or object" in res["error"]
    assert closing.phase is StatusPhase.ERROR

    # Empty context inside item
    res, closing = await _run_tool(
        server,
        "decision_evaluate_probabilities",
        {"items": [{"id": "item1", "context": "   "}], "questions": ["Q1?"]},
    )
    assert res["status"] == "error"
    assert "context validation error" in res["error"].lower()
    assert closing.phase is StatusPhase.ERROR


@pytest.mark.asyncio
async def test_batch_concurrency_bounded():
    server = _make_server(max_concurrency=2)
    mock_client = MagicMock()

    active_calls = 0
    max_active_observed = 0

    async def slow_decide(state, questions, **kwargs):
        nonlocal active_calls, max_active_observed
        active_calls += 1
        max_active_observed = max(max_active_observed, active_calls)
        await asyncio.sleep(0.05)
        active_calls -= 1
        return DecisionsResult(
            answers={"q": Answer(name="q", type="noul", value=0.5)},
            model="typesafe/jev-1.13",
            provider="TypeSafe",
            id="d",
            input_tokens=10,
            output_tokens=1,
            cost=0.000001,
            duration_ms=50.0,
        )

    mock_client.decide = AsyncMock(side_effect=slow_decide)
    server._client = mock_client
    server._cached_profile = None

    params = {
        "items": [f"Item {i}" for i in range(6)],
        "questions": ["Q?"],
        "max_concurrency": 2,
    }
    result, _ = await _run_tool(server, "decision_evaluate_probabilities", params)

    assert result["status"] == "success"
    assert result["summary"]["successful_items"] == 6
    # Concurrency must not have exceeded 2
    assert max_active_observed <= 2


@pytest.mark.asyncio
async def test_whitespace_only_question_and_criterion_ids():
    server = _make_server()
    mock_client = MagicMock()
    fake_prob_result = DecisionsResult(
        answers={"q1": Answer(name="q1", type="noul", value=0.9)},
        model="typesafe/jev-1.13",
        provider="TypeSafe",
        id="d1",
        input_tokens=10,
        output_tokens=1,
        cost=0.000001,
        duration_ms=10.0,
    )
    fake_score_result = DecisionsResult(
        answers={"c1": Answer(name="c1", type="score", value=4.0)},
        model="typesafe/jev-1.13",
        provider="TypeSafe",
        id="d2",
        input_tokens=10,
        output_tokens=1,
        cost=0.000001,
        duration_ms=10.0,
    )
    mock_client.decide = AsyncMock(side_effect=[fake_prob_result, fake_score_result])
    server._client = mock_client
    server._cached_profile = None

    # Whitespace question id -> defaults to q1
    res_prob, _ = await _run_tool(
        server,
        "decision_evaluate_probabilities",
        {"items": ["Context"], "questions": [{"id": "   ", "question": "Is valid?"}]},
    )
    assert res_prob["status"] == "success"
    assert "q1" in res_prob["results"]["item_1"]

    # Whitespace criterion id -> defaults to c1
    res_score, _ = await _run_tool(
        server,
        "decision_evaluate_scores",
        {"items": ["Context"], "criteria": [{"id": "   ", "question": "Rate quality"}]},
    )
    assert res_score["status"] == "success"
    assert "c1" in res_score["results"]["item_1"]


@pytest.mark.asyncio
async def test_cancellation_during_batch():
    server = _make_server()
    mock_client = MagicMock()
    server._client = mock_client
    server._cached_profile = None

    token = MagicMock()
    token.is_cancelled = True

    params = {
        "items": ["Item 1", "Item 2"],
        "questions": ["Q?"],
        "_cancellation_token": token,
    }

    with pytest.raises(asyncio.CancelledError):
        await server.call_with_status("decision_evaluate_probabilities", params)


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [{"criteria_true": "it deletes data"},
                                   {"criteria_false": "it only reads"}])
async def test_one_sided_question_criteria_are_refused_not_dropped(given):
    """criteria_true alone used to vanish before the call: the caller thought it
    steered the answer, and the model never saw it."""
    server = _make_server()
    server._client = MagicMock(decide=AsyncMock(return_value=DecisionsResult(
        answers={"q1": Answer(name="q1", type="noul", value=0.9)}, model="m", provider=None,
        id=None, input_tokens=1, output_tokens=1, cost=0.0, duration_ms=1.0)))
    server._cached_profile = None
    res, closing = await _run_tool(server, "decision_evaluate_probabilities", {
        "items": ["rm -rf /tmp/x"], "questions": [{"question": "Risky?", **given}]})
    assert res["status"] == "error" and "criteria_true and criteria_false" in res["error"], res
    assert closing.phase is StatusPhase.ERROR
    server._client.decide.assert_not_called()


@pytest.mark.asyncio
async def test_both_question_criteria_reach_the_model():
    """Control for the refusal above: both sides given, both are sent."""
    server = _make_server()
    server._client = MagicMock(decide=AsyncMock(return_value=DecisionsResult(
        answers={"q1": Answer(name="q1", type="noul", value=0.9)}, model="m", provider=None,
        id=None, input_tokens=1, output_tokens=1, cost=0.0, duration_ms=1.0)))
    server._cached_profile = None
    res, _ = await _run_tool(server, "decision_evaluate_probabilities", {
        "items": ["rm -rf /tmp/x"],
        "questions": [{"question": "Risky?", "criteria_true": "deletes", "criteria_false": "reads"}]})
    assert res["status"] == "success", res
    sent = server._client.decide.call_args.kwargs["questions"]
    assert sent["q1"]["criteria"] == {"true": "deletes", "false": "reads"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, params", [
    ("decision_evaluate_probabilities", {"items": TWO_ITEMS, "questions": ["Is it valid?"]}),
    ("decision_evaluate_scores",
     {"items": TWO_ITEMS, "criteria": [{"id": "q1", "question": "How good?", "scale": ["low", "high"]}]}),
])
async def test_a_batch_where_every_answer_was_refused_still_reports_its_spend(tool, params):
    """Every item failed, but the refused answers were billed: the error answer
    carries the summary, so the agent sees what the failed batch cost."""
    from plugins.llm_decisions.system_one import DecisionsError

    server = _make_server()
    server._client = MagicMock(decide=AsyncMock(side_effect=DecisionsError(
        "left q1 unanswered", usage={"input_tokens": 30, "output_tokens": 0, "cost": 0.00001})))
    server._cached_profile = None
    res, closing = await _run_tool(server, tool, params)
    assert res["status"] == "error" and closing.phase is StatusPhase.ERROR
    summary = res["summary"]
    assert (summary["failed_items"], summary["total_input_tokens"]) == (2, 60), summary
    assert summary["total_cost"] == pytest.approx(0.00002)
