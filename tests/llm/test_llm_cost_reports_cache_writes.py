"""Both cost reports price Anthropic cache writes, as the live estimate does.

The Anthropic client records writes as prompt_tokens_details.cache_creation_tokens
(OpenRouter: cache_write_tokens). A report that does not read them bills the
writes at plain input and loses the 0.25x premium.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from agent_system.llm.pricing import estimate_cost

REPO = Path(__file__).resolve().parents[2]
MODEL = "claude-opus-5"  # direct API: no billed cost field


def _debugger_db(path, details_key):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE llm_requests (id INTEGER PRIMARY KEY, request_id TEXT,
                    agent_name TEXT, provider TEXT, model TEXT, direction TEXT,
                    usage_json TEXT, created_at TEXT, session_id TEXT)""")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 0, details_key: 400_000}}
    conn.execute("INSERT INTO llm_requests (request_id, agent_name, provider, model, direction,"
                 " usage_json, created_at) VALUES ('root1', 'agent', 'anthropic', ?, 'response', ?,"
                 " '2026-09-15 10:00:00')", (MODEL, json.dumps(usage)))
    conn.commit()
    return conn


def _expected():
    with_writes = estimate_cost(MODEL, 1_000_000, 0, 0, cache_write_tokens=400_000)
    assert with_writes > estimate_cost(MODEL, 1_000_000, 0, 0), "fixture: no cache_write rate for the model"
    return with_writes


@pytest.mark.parametrize("details_key", ["cache_creation_tokens", "cache_write_tokens"])
def test_session_costs_prices_cache_writes(tmp_path, details_key):
    spec = importlib.util.spec_from_file_location("session_costs_probe", REPO / "scripts" / "session_costs.py")
    session_costs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(session_costs)
    conn = _debugger_db(tmp_path / "debugger.db", details_key)

    rows, _ = session_costs.calc_tree_costs(conn, ["root1"], session_costs.load_pricing(session_costs.DEFAULT_PRICING))

    assert rows[0]["cost"] == pytest.approx(_expected())


@pytest.mark.parametrize("details_key", ["cache_creation_tokens", "cache_write_tokens"])
def test_analyze_costs_prices_cache_writes(tmp_path, monkeypatch, details_key):
    import plugins_writer.writer_pipeline_v4.analyze_costs as ac

    db = tmp_path / "debugger.db"
    _debugger_db(db, details_key).close()
    monkeypatch.setattr(ac, "_connect_debugger", lambda: sqlite3.connect(db))

    report = ac.calculate_costs("root1")

    assert report.rows[0].cost == pytest.approx(_expected())
