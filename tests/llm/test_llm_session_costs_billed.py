"""session_costs: a billed cost is the price of its own calls, 0 included.

Ollama reports cost 0 and needs no price row; a group mixing billed and
unbilled calls used to let the billed part's sum stand for all of them.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from agent_system.llm.pricing import estimate_cost

REPO = Path(__file__).resolve().parents[2]
PRICED = "claude-opus-5"  # direct API: no billed cost field


def _session_costs():
    spec = importlib.util.spec_from_file_location("session_costs_billed", REPO / "scripts" / "session_costs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _costs(tmp_path, calls):
    module = _session_costs()
    conn = sqlite3.connect(tmp_path / "debugger.db")
    conn.execute("""CREATE TABLE llm_requests (id INTEGER PRIMARY KEY, request_id TEXT,
                    agent_name TEXT, provider TEXT, model TEXT, direction TEXT,
                    usage_json TEXT, created_at TEXT, session_id TEXT)""")
    for model, usage in calls:
        conn.execute("INSERT INTO llm_requests (request_id, agent_name, provider, model, direction, usage_json,"
                     " created_at) VALUES ('root1', 'agent', 'p', ?, 'response', ?, '2026-10-03 10:00:00')",
                     (model, json.dumps(usage)))
    conn.commit()
    try:
        return module.calc_tree_costs(conn, ["root1"], module.load_pricing(module.DEFAULT_PRICING))
    finally:
        conn.close()


def test_an_ollama_call_billed_at_0_costs_0_and_is_not_unpriced(tmp_path):
    rows, unpriced = _costs(tmp_path, [("some-ollama-model:7b", {"prompt_tokens": 500, "cost": 0.0})])

    assert [(r["calls"], r["cost"]) for r in rows] == [(1, 0.0)]
    assert unpriced == []


def test_a_billed_0_is_not_replaced_by_the_table_estimate(tmp_path):
    rows, _ = _costs(tmp_path, [(PRICED, {"prompt_tokens": 1_000_000, "cost": 0.0})])

    assert estimate_cost(PRICED, 1_000_000, 0, 0) > 0, "fixture: the model has no price"
    assert rows[0]["cost"] == 0.0


def test_billed_and_unbilled_calls_of_one_group_are_each_priced(tmp_path):
    rows, _ = _costs(tmp_path, [(PRICED, {"prompt_tokens": 1_000_000, "cost": 0.5}),
                                (PRICED, {"prompt_tokens": 1_000_000})])

    assert len(rows) == 1 and rows[0]["calls"] == 2 and rows[0]["prompt_tokens"] == 2_000_000
    assert rows[0]["cost"] == pytest.approx(0.5 + estimate_cost(PRICED, 1_000_000, 0, 0))


def test_a_model_without_price_or_cost_is_still_named_but_not_for_its_errors(tmp_path):
    _, unpriced = _costs(tmp_path, [("no-such-model", {"prompt_tokens": 10}),
                                    ("billed-model", {"prompt_tokens": 10, "cost": 0.1}),
                                    ("billed-model", {})])

    assert unpriced == ["no-such-model"]
