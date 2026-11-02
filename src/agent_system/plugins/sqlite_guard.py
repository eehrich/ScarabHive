"""SQLite for plugin stores that more than one process opens at the same time.

The agent-api, an agent-cli run, a seed script and a panel's schedule may all write one store. A thread lock keeps
only one process in order; between processes SQLite must do it:

- WAL journal: readers do not block the writer, the writer does not block readers.
- busy_timeout: a writer waits for the other one instead of failing at once with "database is locked".
- BEGIN IMMEDIATE: a transaction takes the write lock at its start. A read-then-write (next ticket number, merge of
  a scan) cannot interleave with another process's, and two deferred transactions cannot deadlock upgrading their
  read locks -- the one case busy_timeout does not wait out.

``connection(path)`` is one transaction on its own connection, closed afterwards (``with sqlite3.connect(...)``
commits but never closes). ``Guard`` is the same for a connection a store keeps open: re-entrant, one transaction
for the outermost ``with``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: How long a writer waits for another one, in milliseconds.
BUSY_MS = 15_000


def open_db(path: Path | str, *, foreign_keys: bool = False) -> sqlite3.Connection:
    """A connection in autocommit mode (transactions are explicit), WAL, waiting for a busy database."""
    db = sqlite3.connect(str(path), timeout=BUSY_MS / 1000, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
    _wal(db)
    if foreign_keys:
        db.execute("PRAGMA foreign_keys = ON")   # only outside a transaction
    return db


def _wal(db: sqlite3.Connection) -> None:
    """Switch the file to WAL (kept in the file, so once). The switch does not wait for busy_timeout: processes
    opening a new file at the same moment get "database is locked" at once, so it is retried until BUSY_MS."""
    deadline = time.monotonic() + BUSY_MS / 1000
    while True:
        try:
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                db.close()
                raise
            time.sleep(0.02)


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on an exception. An executescript inside commits on its own."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if db.in_transaction:
        db.execute("COMMIT")


@contextmanager
def connection(path: Path | str, *, foreign_keys: bool = False) -> Iterator[sqlite3.Connection]:
    """One transaction on a connection of its own, closed afterwards."""
    db = open_db(path, foreign_keys=foreign_keys)
    try:
        with transaction(db):
            yield db
    finally:
        db.close()


class Guard:
    """A long-lived connection's lock: threads take turns, and the outermost ``with`` is one IMMEDIATE transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.RLock()
        self._depth = 0

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        if self._depth == 0:
            try:
                self.db.execute("BEGIN IMMEDIATE")
            except BaseException:
                self._lock.release()
                raise
        self._depth += 1
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        self._depth -= 1
        try:
            if self._depth == 0 and self.db.in_transaction:
                self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._lock.release()
