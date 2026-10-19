"""
Size-bounded, cost-preserving auto-retention for the message_debugger DB.

The user keeps the debugger DB for COST calculation, so retention must not throw
away the (tiny) cost rows (provider/model/usage/request_id/timestamps). The
payloads, responses and turn snapshots are ~all the bytes, so retention deletes
the oldest of them -- whole rows, payloads (llm_payloads) and turns by age -- and
deletes cost rows only as a last resort. Work is time-budgeted so it never delays
a capture write past the 5s hook timeout.

The data is random: it is stored compressed, and repeated text would shrink to
nothing.
"""

import base64
import os
import time

import pytest

from plugins.message_debugger.database import EVERYONE, MessageDebuggerDB


def _noise(n=3000):
    return base64.b64encode(os.urandom(n)).decode()


_BIG = {"messages": [{"role": "user", "content": _noise()}], "model": "gpt-5"}
_BIG_MESSAGES = [{"role": "user", "content": _noise(1500)} for _ in range(4)]


def _insert_requests(db, n, start=0, step=1.0):
    for i in range(n):
        db.insert_llm_request(
            timestamp_ms=float(start + i * step), direction="response", agent_name="a",
            request_id=f"req_{start + i}", session_id="s",
            provider="openai", model="gpt-5",
            payload=_BIG, response_data=_BIG,
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            duration_ms=123.0, finish_reason="stop",
        )


def _insert_turns(db, n, start=0, step=1.0):
    for i in range(n):
        db.insert_turn(timestamp_ms=float(start + i * step), snapshot_type="pre_llm",
                       agent_name="a", session_id="s", messages=_BIG_MESSAGES)


def _count(db, table):
    return db._get_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _req(db, request_id):
    (row_id,) = db._get_conn().execute("SELECT id FROM llm_requests WHERE request_id = ?", (request_id,)).fetchone()
    return db.get_llm_request(row_id, owner=EVERYONE)


def _turns_ts_range(db):
    return db._get_conn().execute(
        "SELECT MIN(timestamp_ms), MAX(timestamp_ms) FROM turns"
    ).fetchone()


def _oldest_payload_ts(db):
    row = db._get_conn().execute("SELECT MIN(r.timestamp_ms) FROM llm_payloads p "
                                 "JOIN llm_requests r ON r.id = p.id").fetchone()
    return row[0]


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
    def test_deletes_old_payloads_but_keeps_cost_rows(self, db):
        _insert_requests(db, 150)  # ~1.2 MB of payloads, over the 1 MB cap
        conn = db._get_conn()
        assert db._used_bytes(conn) > db._high_bytes, "fixture: not over the cap"

        res = db.enforce_retention()

        assert res["payloads_deleted"] > 0
        assert _count(db, "llm_requests") == 150, "cost rows must NOT be deleted"
        old = _req(db, "req_0")
        assert old["payload_json"] is None and old["response_json"] is None
        assert old["usage_json"] is not None and old["model"] == "gpt-5", "cost cols kept"
        assert _req(db, "req_149")["payload_json"] == _BIG, "newest payload kept for debugging"
        assert db._used_bytes(conn) <= db._low_bytes

    def test_a_deleted_payload_gives_its_space_back(self, db):
        """The fault this layout fixes: payloads NULLed in place in their cost rows left each row its page --
        on the server 1.3 GB of empty page space under the cap, growing with every pruned row, until the cap
        held two days of captures. Deleted from llm_payloads, their pages go free: the cost rows of far more
        content than the cap holds all stay, and the file gets under its low watermark."""
        _insert_requests(db, 300)  # ~2.4 MB: NULLed in place, its 300 cost rows alone would keep ~1.2 MB
        res = db.enforce_retention()

        assert db._used_bytes(db._get_conn()) <= db._low_bytes
        assert res["requests_deleted"] == 0 and _count(db, "llm_requests") == 300, \
            "a cost row went: the pruned payloads did not give their space back"

    def test_last_resort_deletes_oldest_cost_rows_when_nothing_else(self, db):
        # No content left but still over the cap: the only way to honour it is
        # to delete the oldest cost rows.
        _insert_requests(db, 200)
        db._get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # force a tiny cap so the cost-only rows still exceed it
        db._max_size_bytes = 32 * 1024
        db._high_bytes = int(db._max_size_bytes * 0.90)
        db._low_bytes = int(db._max_size_bytes * 0.75)

        res = db.enforce_retention(force=True)
        assert res["payloads_deleted"] == 200 and _count(db, "llm_payloads") == 0
        assert res["requests_deleted"] > 0
        assert _count(db, "llm_requests") < 200
        oldest = db._get_conn().execute("SELECT MIN(timestamp_ms) FROM llm_requests").fetchone()[0]
        assert oldest is None or oldest >= res["requests_deleted"], "not the oldest cost rows went"

    def test_noop_below_high(self, db):
        _insert_requests(db, 5)
        assert db.enforce_retention() == {"payloads_deleted": 0, "turns_deleted": 0, "requests_deleted": 0}


class TestTurns:
    def test_turns_pruned_oldest_first(self, db):
        _insert_turns(db, 300)
        db.enforce_retention()
        lo, hi = _turns_ts_range(db)
        assert hi == 299.0, "newest turn kept"
        assert lo and lo > 0.0, "oldest turns pruned"

    def test_payloads_and_turns_keep_the_same_days(self, db):
        """Whichever reaches further back goes first: a window of payloads ending where the turns' starts
        would show a run's requests without its turns, or the other way round."""
        _insert_requests(db, 100, start=0, step=2.0)
        _insert_turns(db, 100, start=1, step=2.0)
        res = db.enforce_retention()

        assert res["payloads_deleted"] and res["turns_deleted"], f"fixture: not both pruned: {res}"
        payload_from, (turn_from, _) = _oldest_payload_ts(db), _turns_ts_range(db)
        # one batch apart at most: 20 rows, 2 ms each
        assert abs(payload_from - turn_from) <= 2 * db._retention_batch * 2.0, (payload_from, turn_from)


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
        assert stats["payloads_deleted"] == db._retention_batch, f"not exactly one slice: {stats}"
        assert db._pruning is True, "not finished -> still armed, continues next call"


class TestManualPrune:
    def test_force_prune_then_vacuum_shrinks_and_keeps_cost(self, db):
        _insert_requests(db, 150)
        db._get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the rows into the file, which then holds them all
        before = os.path.getsize(db.db_path)
        db.enforce_retention(force=True)
        db.vacuum()
        after = os.path.getsize(db.db_path)
        assert after < before * 0.7, f"file did not shrink: {before} -> {after}"
        assert _count(db, "llm_requests") == 150, "cost rows preserved through manual prune"

    @pytest.mark.asyncio
    async def test_prune_endpoint_deletes_reports_and_keeps_cost(self, db, monkeypatch):
        import plugins.message_debugger.web_endpoints as endpoints
        from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory

        # an admin asks; that only one may is test_plugin_message_debugger_access.py
        monkeypatch.setattr(endpoints, "require_everything", lambda *args: None)

        _insert_requests(db, 150)
        factory = MessageDebuggerWebFactory(db=db)
        res = await factory.prune(None, vacuum=True)

        assert res["payloads_deleted"] > 0
        assert res["vacuumed"] is True
        assert res["freed_mb"] > 0
        assert "size_after_mb" in res
        assert _count(db, "llm_requests") == 150

    @pytest.mark.asyncio
    async def test_prune_and_clear_run_on_the_writer_thread(self, db, monkeypatch):
        """A VACUUM or clear of a multi-GB file holds the write lock for minutes; run on another connection,
        every capture made meanwhile gave up after the 10 s busy timeout and was lost. Behind the writer they
        wait in its queue instead."""
        import threading

        import plugins.message_debugger.web_endpoints as endpoints
        from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory

        monkeypatch.setattr(endpoints, "require_everything", lambda *args: None)
        ran_on = {}
        for name in ("vacuum", "clear_all"):
            real = getattr(db, name)

            def spy(real=real, name=name):
                ran_on[name] = threading.current_thread().name
                return real()
            monkeypatch.setattr(db, name, spy)

        _insert_requests(db, 150)
        factory = MessageDebuggerWebFactory(db=db)
        assert (await factory.prune(None, vacuum=True))["vacuumed"] is True
        assert (await factory.clear_all(None))["requests_deleted"] == 150
        assert _count(db, "llm_payloads") == 0, "a clear left the payloads"
        assert ran_on == {"vacuum": "msgdbg-writer", "clear_all": "msgdbg-writer"}

    def test_a_failure_on_the_writer_reaches_the_caller(self, db):
        def fails():
            raise ValueError("disk full")
        with pytest.raises(ValueError, match="disk full"):
            db.call(fails)
        assert db.call(lambda: 7) == 7  # the writer lives on

    def test_a_call_behind_a_stopped_writer_does_not_wait_forever(self, db):
        import threading

        db._write_q.put(None)  # the writer stops, as a close() between the check and the put leaves it
        db._writer_thread.join(timeout=3)
        outcome = {}

        def ask():
            try:
                db.call(lambda: 7)
            except RuntimeError as e:
                outcome["error"] = str(e)
        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        asker.join(timeout=5)
        assert outcome == {"error": "the message debugger database was closed"}


class TestDisabled:
    def test_cap_zero_disables(self, tmp_path):
        d = MessageDebuggerDB(tmp_path / "nocap.db", wal_mode=True, max_size_mb=0)
        try:
            d._retention_check_interval = 5
            _insert_requests(d, 150)
            assert _count(d, "llm_requests") == _count(d, "llm_payloads") == 150
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
        assert _count(db, "llm_requests") == 600, "a cost row went while content was left"
