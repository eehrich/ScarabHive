"""
Size-bounded auto-retention for the message_debugger DB.

Goal (user): the SQLite file must stop growing — it should plateau at the cap.
SQLite DELETE does not shrink the file, but freed pages are reused, so the file
stays bounded. We measure REAL data size ((page_count - freelist) * page_size),
prune oldest rows with hysteresis (high/low watermark), and never VACUUM.
"""

import os

import pytest

from plugins.message_debugger.database import MessageDebuggerDB

# ~20 KB of message JSON per turn -> a 1 MB cap is reached in ~50 turns.
_BIG_MESSAGES = [{"role": "user", "content": "x" * 5000} for _ in range(4)]


def _insert_turns(db, n, start=0):
    for i in range(n):
        db.insert_turn(
            timestamp_ms=float(start + i),
            snapshot_type="pre_llm",
            agent_name="a",
            session_id="s",
            messages=_BIG_MESSAGES,
        )


def _count(db):
    return db._get_conn().execute("SELECT COUNT(*) FROM turns").fetchone()[0]


def _ts_range(db):
    row = db._get_conn().execute(
        "SELECT MIN(timestamp_ms), MAX(timestamp_ms) FROM turns"
    ).fetchone()
    return row[0], row[1]


@pytest.fixture
def db(tmp_path):
    # 1 MB cap -> high ~0.9 MB, low ~0.75 MB; tiny so the test is fast.
    d = MessageDebuggerDB(tmp_path / "ret.db", wal_mode=True, max_size_mb=1)
    yield d
    d.close()


class TestEnforceRetention:
    def test_prunes_down_to_low_watermark(self, db):
        db._retention_check_interval = 10**9  # disable auto; drive it manually
        _insert_turns(db, 300)
        conn = db._get_conn()
        assert db._used_bytes(conn) > db._high_bytes, "setup: should be over HIGH"

        res = db.enforce_retention()

        assert res["turns_deleted"] > 0
        assert db._used_bytes(conn) <= db._low_bytes, "must prune down to LOW"

    def test_oldest_pruned_newest_kept(self, db):
        db._retention_check_interval = 10**9
        _insert_turns(db, 300)  # timestamps 0..299
        db.enforce_retention()

        lo, hi = _ts_range(db)
        assert hi == 299.0, "newest turn must survive"
        assert lo > 0.0, "oldest turns must be pruned"

    def test_noop_below_high_watermark(self, db):
        db._retention_check_interval = 10**9
        _insert_turns(db, 5)  # tiny, well under cap
        before = _count(db)
        res = db.enforce_retention()
        assert res == {"turns_deleted": 0, "requests_deleted": 0}
        assert _count(db) == before


class TestAutoRetentionKeepsFileBounded:
    def test_file_plateaus_under_continuous_inserts(self, db):
        db._retention_check_interval = 25  # check often for the test
        # Insert ~16 MB worth of turns into a 1 MB-capped DB.
        _insert_turns(db, 800)

        conn = db._get_conn()
        used = db._used_bytes(conn)
        file_bytes = os.path.getsize(db.db_path)

        # Real data is bounded near the cap (NOT the ~16 MB that unbounded
        # growth would produce).
        assert used < db._max_size_bytes * 2, f"data not bounded: {used}"
        # The file itself plateaued well below unbounded growth.
        assert file_bytes < db._max_size_bytes * 3, f"file not bounded: {file_bytes}"
        # And capture still works (rows are present, just bounded).
        assert _count(db) > 0

    def test_reuse_after_prune_does_not_grow_file(self, db):
        db._retention_check_interval = 25
        _insert_turns(db, 500)
        file_after_first = os.path.getsize(db.db_path)
        # Insert another large batch — freed pages must be reused, file ~flat.
        _insert_turns(db, 500, start=10_000)
        file_after_second = os.path.getsize(db.db_path)

        # Allow a little slack but the file must NOT have grown by another batch.
        assert file_after_second <= file_after_first * 1.5, (
            f"file kept growing: {file_after_first} -> {file_after_second}"
        )


class TestRetentionDisabled:
    def test_cap_zero_disables_pruning(self, tmp_path):
        d = MessageDebuggerDB(tmp_path / "nocap.db", wal_mode=True, max_size_mb=0)
        try:
            d._retention_check_interval = 10  # would fire often if enabled
            _insert_turns(d, 120)
            # No retention: every row is kept.
            assert d._get_conn().execute(
                "SELECT COUNT(*) FROM turns"
            ).fetchone()[0] == 120
        finally:
            d.close()
