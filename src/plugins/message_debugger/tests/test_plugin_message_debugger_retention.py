"""
Size-bounded, cost-preserving auto-retention for the message_debugger DB.

The user keeps the debugger DB for COST calculation, so retention must not throw
away the (tiny) cost columns (provider/model/usage/request_id/timestamps). The
payload/response BLOBs are ~all the bytes, so retention STRIPS the oldest
payloads (keeping the cost row) and drops the oldest turn snapshots; it only
deletes cost rows as a last resort. Work is time-budgeted so it never delays a
capture write past the 5s hook timeout.
"""

import os
import time

import pytest

from plugins.message_debugger.database import MessageDebuggerDB

_BIG = {"messages": [{"role": "user", "content": "x" * 5000}], "model": "gpt-5"}
_BIG_MESSAGES = [{"role": "user", "content": "x" * 5000} for _ in range(4)]


def _insert_requests(db, n, start=0):
    for i in range(n):
        db.insert_llm_request(
            timestamp_ms=float(start + i), direction="response", agent_name="a",
            request_id=f"req_{start + i}", session_id="s",
            provider="openai", model="gpt-5",
            payload=_BIG, response_data=_BIG,
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            duration_ms=123.0, finish_reason="stop",
        )


def _insert_turns(db, n, start=0):
    for i in range(n):
        db.insert_turn(timestamp_ms=float(start + i), snapshot_type="pre_llm",
                       agent_name="a", session_id="s", messages=_BIG_MESSAGES)


def _count_requests(db):
    return db._get_conn().execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0]


def _req(db, request_id):
    return db._get_conn().execute(
        "SELECT payload_json, response_json, usage_json, model, provider "
        "FROM llm_requests WHERE request_id = ?", (request_id,),
    ).fetchone()


def _turns_ts_range(db):
    return db._get_conn().execute(
        "SELECT MIN(timestamp_ms), MAX(timestamp_ms) FROM turns"
    ).fetchone()


@pytest.fixture
def db(tmp_path):
    # 1 MB cap -> HIGH ~0.9 MB, LOW ~0.75 MB; tiny so the test is fast.
    d = MessageDebuggerDB(tmp_path / "ret.db", wal_mode=True, max_size_mb=1)
    d._retention_check_interval = 10**9  # default: drive retention manually
    d._retention_batch = 20
    d._retention_budget_s = 30           # generous so one call reaches LOW
    yield d
    d.close()


class TestCostPreserving:
    def test_strips_old_payloads_but_keeps_cost_rows(self, db):
        _insert_requests(db, 120)  # ~1.2 MB of payloads, over the 1 MB cap
        conn = db._get_conn()
        assert db._used_bytes(conn) > db._high_bytes

        res = db.enforce_retention()

        assert res["stripped"] > 0
        assert _count_requests(db) == 120, "cost rows must NOT be deleted"
        old = _req(db, "req_0")
        assert old["payload_json"] is None and old["response_json"] is None
        assert old["usage_json"] is not None and old["model"] == "gpt-5", "cost cols kept"
        new = _req(db, "req_119")
        assert new["payload_json"] is not None, "newest payload kept for debugging"
        assert db._used_bytes(conn) <= db._low_bytes

    def test_last_resort_deletes_oldest_cost_rows_when_nothing_else(self, db):
        # All payloads already stripped (cost-only rows) but still over cap:
        # the only way to honour the cap is to delete the oldest cost rows.
        _insert_requests(db, 200)
        db._get_conn().execute("UPDATE llm_requests SET payload_json=NULL, response_json=NULL")
        db._get_conn().commit()
        db._get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # force a tiny cap so the cost-only rows still exceed it
        db._max_size_bytes = 32 * 1024
        db._high_bytes = int(db._max_size_bytes * 0.90)
        db._low_bytes = int(db._max_size_bytes * 0.75)

        res = db.enforce_retention(force=True)
        assert res["requests_deleted"] > 0
        assert _count_requests(db) < 200  # oldest cost rows removed as last resort

    def test_noop_below_high(self, db):
        _insert_requests(db, 5)
        assert db.enforce_retention() == {"stripped": 0, "turns_deleted": 0, "requests_deleted": 0}


class TestTurns:
    def test_turns_pruned_oldest_first(self, db):
        _insert_turns(db, 300)
        db.enforce_retention()
        lo, hi = _turns_ts_range(db)
        assert hi == 299.0, "newest turn kept"
        assert lo and lo > 0.0, "oldest turns pruned"


class TestBudget:
    def test_call_is_time_bounded_and_stays_armed(self, db, monkeypatch):
        """The budget is read off the database module's clock: spent after the first slice, whatever
        the machine's speed -- a fast one finished all 300 rows within a real 0.05 s, and the call
        this test is about never stopped early."""
        import plugins.message_debugger.database as database

        class _OneSlice:
            """time, but the monotonic clock jumps past any budget once the first slice began."""

            def __init__(self):
                self.readings = 0

            def monotonic(self):
                self.readings += 1
                return 0.0 if self.readings <= 2 else 1000.0  # 1: the deadline, 2: the first check

            def __getattr__(self, name):
                return getattr(time, name)

        monkeypatch.setattr(database, "time", _OneSlice())
        db._retention_batch = 5
        _insert_requests(db, 300)
        stats = db.enforce_retention(budget=0.05)
        assert stats["stripped"] == db._retention_batch, f"not exactly one slice: {stats}"
        assert db._pruning is True, "not finished -> still armed, continues next call"


class TestManualPrune:
    def test_force_prune_then_vacuum_shrinks_and_keeps_cost(self, db):
        _insert_requests(db, 120)
        before = os.path.getsize(db.db_path)
        db.enforce_retention(force=True)
        db.vacuum()
        after = os.path.getsize(db.db_path)
        assert after < before * 0.7, f"file did not shrink: {before} -> {after}"
        assert _count_requests(db) == 120, "cost rows preserved through manual prune"

    @pytest.mark.asyncio
    async def test_prune_endpoint_strips_reports_and_keeps_cost(self, db, monkeypatch):
        import plugins.message_debugger.web_endpoints as endpoints
        from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory

        # an admin asks; that only one may is test_plugin_message_debugger_access.py
        monkeypatch.setattr(endpoints, "require_everything", lambda *args: None)

        _insert_requests(db, 120)
        factory = MessageDebuggerWebFactory(db=db)
        res = await factory.prune(None, vacuum=True)

        assert res["stripped"] > 0
        assert res["vacuumed"] is True
        assert res["freed_mb"] > 0
        assert "size_after_mb" in res
        assert _count_requests(db) == 120


class TestDisabled:
    def test_cap_zero_disables(self, tmp_path):
        d = MessageDebuggerDB(tmp_path / "nocap.db", wal_mode=True, max_size_mb=0)
        try:
            d._retention_check_interval = 5
            _insert_requests(d, 120)
            assert d._get_conn().execute(
                "SELECT COUNT(*) FROM llm_requests"
            ).fetchone()[0] == 120
        finally:
            d.close()


class TestAutoRetentionBounds:
    @pytest.mark.asyncio
    async def test_auto_keeps_data_bounded(self, db):
        # Re-enable the auto trigger with a short interval; insert far over cap.
        db._retention_check_interval = 25
        db._retention_budget_s = 30
        _insert_requests(db, 600)
        # Drain any remaining armed pruning (auto fires inside inserts; finish it).
        for _ in range(50):
            if not db._pruning:
                break
            db.enforce_retention()
        used = db._used_bytes(db._get_conn())
        assert used < db._max_size_bytes * 2, f"data not bounded: {used}"
        assert _count_requests(db) > 0  # cost rows retained
