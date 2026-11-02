"""sqlite_guard: stores that several processes write at once lose nothing and do not fail with "database is locked".

The writers are real processes, as in production (agent-api, agent-cli, a seed script): each does a read-then-write
many times. Without BEGIN IMMEDIATE two of them read the same number and one increment is lost, or a deferred
transaction fails to upgrade its lock.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_system.plugins.sqlite_guard import Guard, connection, open_db

SRC = Path(__file__).resolve().parents[2] / "src"
WRITERS, ROUNDS = 4, 60

COUNTER = """
import sys
from agent_system.plugins.sqlite_guard import connection
for _ in range({rounds}):
    with connection(sys.argv[1]) as db:
        value = db.execute("SELECT value FROM counter").fetchone()[0]
        db.execute("UPDATE counter SET value = ?", (value + 1,))
"""

def run_writers(script: str, *args: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    processes = [subprocess.Popen([sys.executable, "-c", script, *args, str(n)], env=env, stderr=subprocess.PIPE,
                                  text=True) for n in range(WRITERS)]
    errors = [p.communicate(timeout=120)[1] for p in processes]
    assert all(p.returncode == 0 for p in processes), "\n".join(errors)


def test_parallel_processes_lose_no_update(tmp_path):
    path = tmp_path / "counter.db"
    with connection(path) as db:
        db.execute("CREATE TABLE counter (value INTEGER NOT NULL)")
        db.execute("INSERT INTO counter VALUES (0)")
    run_writers(COUNTER.format(rounds=ROUNDS), str(path))
    with connection(path) as db:
        assert db.execute("SELECT value FROM counter").fetchone()[0] == WRITERS * ROUNDS
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_an_exception_rolls_the_transaction_back(tmp_path):
    path = tmp_path / "x.db"
    with connection(path) as db:
        db.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(RuntimeError), connection(path) as db:
        db.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")
    with connection(path) as db:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_the_guard_is_one_transaction_for_nested_use_and_threads_take_turns(tmp_path):
    db = open_db(tmp_path / "g.db")
    db.execute("CREATE TABLE t (v INTEGER)")
    guard = Guard(db)
    with guard:
        with guard:
            db.execute("INSERT INTO t VALUES (1)")
        assert db.in_transaction                     # the inner exit does not commit
    assert not db.in_transaction

    def add():
        for _ in range(50):
            with guard:
                value = db.execute("SELECT COUNT(*) FROM t").fetchone()[0]
                db.execute("INSERT INTO t VALUES (?)", (value,))

    threads = [threading.Thread(target=add) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    values = [row[0] for row in db.execute("SELECT v FROM t ORDER BY rowid")]
    assert values == [1, *range(1, 201)]
    with pytest.raises(sqlite3.OperationalError), guard:
        db.execute("INSERT INTO nowhere VALUES (1)")
    assert not db.in_transaction
    db.close()
