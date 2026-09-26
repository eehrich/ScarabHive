"""The run store: one SQLite file with runs and their step journal.

Journal rows are keyed by ``(run_id, kind, key)``; ``seq`` orders them for
display. Kinds (docs/stategraph_design.md §4.3):

* ``activity`` -- key = deterministic step path (``s3``, ``s3/b:style``,
  ``s3/m/s1``); status ``started`` -> ``done`` | ``error``; data = result or
  error plus meta. Replay serves ``done``/``error`` rows instead of running.
* ``event`` -- an external event; key ``pending:<n>`` until a frame consumes
  it, then the step key where it was consumed.
* ``edit`` -- a debugger edit of a frame's context, keyed by step and hook.
* ``trace`` -- enter/exit/transition records for history and the canvas.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ACTIVE_STATUSES = ("running", "paused", "waiting")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    machine_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    params TEXT,
    mocks TEXT,
    output TEXT,
    error TEXT,
    final_state TEXT,
    definition TEXT NOT NULL,
    view TEXT,
    debug TEXT,
    user_id TEXT,
    session_id TEXT,
    parent_run TEXT,
    fork_step INTEGER,
    run_key TEXT,
    owner TEXT,
    lease_until TEXT,
    journal_format INTEGER,
    nesting TEXT
);
CREATE INDEX IF NOT EXISTS runs_key ON runs(run_key);
CREATE INDEX IF NOT EXISTS runs_machine ON runs(machine_id, created_at);
CREATE TABLE IF NOT EXISTS journal (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    state TEXT,
    status TEXT,
    data TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS journal_key ON journal(run_id, kind, key);
-- the agent facade: which run a caller's session belongs to (a continue in any process finds it)
CREATE TABLE IF NOT EXISTS callers (
    caller TEXT PRIMARY KEY,
    run_id TEXT NOT NULL
);
"""

_JSON_COLUMNS = ("params", "mocks", "output", "error", "definition", "view", "debug", "nesting")
#: Columns a runs.db from before them lacks: added when it is opened.
_ADDED_COLUMNS = {"nesting": "TEXT"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Optional[str]) -> Any:
    return None if value is None else json.loads(value)


class RunStore:
    """Thread-safe access to ``runs.db``; opened lazily so tests can point it at tmp_path."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- plumbing
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            present = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for column, kind in _ADDED_COLUMNS.items():
                if column not in present:
                    try:
                        conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
                    except sqlite3.OperationalError as exc:  # another process added it meanwhile
                        if "duplicate column" not in str(exc):
                            raise
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for column in _JSON_COLUMNS:
            data[column] = _loads(data.get(column))
        return data

    @staticmethod
    def _journal_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["data"] = _loads(data.get("data"))
        return data

    # ---------------------------------------------------------------- runs
    def create_run(self, run_id: str, machine_id: str, definition: dict[str, Any], *,
                   params: Any = None, mocks: Any = None, debug: Any = None, user_id: Optional[str] = None,
                   session_id: Optional[str] = None, parent_run: Optional[str] = None,
                   fork_step: Optional[int] = None, run_key: Optional[str] = None, owner: Optional[str] = None,
                   lease_until: Optional[str] = None, journal_format: int = 1, status: str = "running",
                   nesting: Any = None) -> None:
        now = utc_now()
        with self._lock:
            self._db().execute(
                "INSERT INTO runs (id, machine_id, status, created_at, updated_at, params, mocks, definition, debug,"
                " user_id, session_id, parent_run, fork_step, run_key, owner, lease_until, journal_format, nesting)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, machine_id, status, now, now, _dumps(params), _dumps(mocks), _dumps(definition),
                 _dumps(debug), user_id, session_id, parent_run, fork_step, run_key, owner, lease_until,
                 journal_format, _dumps(nesting)))

    def update_run(self, run_id: str, *, fence: Optional[str] = None, **fields: Any) -> int:
        """Update a run; with ``fence`` only while that owner still holds it. Returns the rows changed."""
        if not fields:
            return 0
        fields["updated_at"] = utc_now()
        columns = ", ".join(f"{name} = ?" for name in fields)
        values = [(_dumps(v) if name in _JSON_COLUMNS else v) for name, v in fields.items()]
        sql = f"UPDATE runs SET {columns} WHERE id = ?"
        args: list[Any] = [*values, run_id]
        if fence is not None:
            sql += " AND owner = ?"
            args.append(fence)
        with self._lock:
            return self._db().execute(sql, args).rowcount

    def renew(self, run_id: str, owner: str, until: str, status: str) -> bool:
        """The owner's heartbeat: extend the lease and undo a sweep that marked the live run interrupted."""
        with self._lock:
            cursor = self._db().execute(
                "UPDATE runs SET lease_until = ?, updated_at = ?,"
                " status = CASE WHEN status = 'interrupted' THEN ? ELSE status END"
                " WHERE id = ? AND owner = ?", (until, utc_now(), status, run_id, owner))
            return cursor.rowcount == 1

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, machine_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = ("SELECT id, machine_id, status, created_at, updated_at, finished_at, final_state, error, user_id,"
               " parent_run, fork_step, run_key FROM runs")
        args: list[Any] = []
        if machine_id:
            sql += " WHERE machine_id = ?"
            args.append(machine_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db().execute(sql, args).fetchall()
        runs = []
        for row in rows:
            data = dict(row)
            data["error"] = _loads(data.get("error"))
            runs.append(data)
        return runs

    def mark_expired(self, *, now: str) -> list[str]:
        """Active runs whose owner stopped renewing the lease: ``interrupted``, resumable.

        Only an expired lease counts -- a process that merely starts never takes
        a run that another live process owns (docs/stategraph_design.md §5.7).
        """
        active = ",".join("?" * len(ACTIVE_STATUSES))
        with self._lock:
            rows = self._db().execute(
                f"SELECT id FROM runs WHERE status IN ({active}) AND (lease_until IS NULL OR lease_until < ?)",
                (*ACTIVE_STATUSES, now)).fetchall()
            swept = []
            for row in rows:  # the condition again AT WRITE TIME: a run renewed or finished meanwhile is left alone
                cursor = self._db().execute(
                    f"UPDATE runs SET status = 'interrupted', updated_at = ? WHERE id = ? AND status IN ({active})"
                    " AND (lease_until IS NULL OR lease_until < ?)", (utc_now(), row["id"], *ACTIVE_STATUSES, now))
                if cursor.rowcount == 1:
                    swept.append(row["id"])
        return swept

    def update_debug_unowned(self, run_id: str, debug: Any, *, now: str) -> bool:
        """Change a run's breakpoints from outside its owner -- only while nobody holds a live lease on it.

        The same rule as ``take_lease``: the status is no escape (a swept run's owner may still be alive).
        """
        with self._lock:
            cursor = self._db().execute(
                "UPDATE runs SET debug = ?, updated_at = ? WHERE id = ? AND (lease_until IS NULL OR lease_until < ?)",
                (_dumps(debug), utc_now(), run_id, now))
            return cursor.rowcount == 1

    def take_lease(self, run_id: str, owner: str, until: str, *, now: str) -> bool:
        """Take the run for ``owner`` if nobody holds a live lease on it (one conditional update)."""
        with self._lock:
            cursor = self._db().execute(
                "UPDATE runs SET owner = ?, lease_until = ?, updated_at = ? WHERE id = ?"
                " AND (lease_until IS NULL OR lease_until < ? OR owner = ?)",
                (owner, until, utc_now(), run_id, now, owner))
            return cursor.rowcount == 1

    def set_caller(self, caller: str, run_id: str) -> None:
        """Remember the run of a caller (``<agent>:<session id>``)."""
        with self._lock:
            self._db().execute("INSERT INTO callers (caller, run_id) VALUES (?, ?) ON CONFLICT(caller) DO UPDATE "
                               "SET run_id = excluded.run_id", (caller, run_id))

    def run_of_caller(self, caller: str) -> Optional[str]:
        with self._lock:
            row = self._db().execute("SELECT run_id FROM callers WHERE caller = ?", (caller,)).fetchone()
        return row["run_id"] if row else None

    def latest_by_key(self, run_key: str) -> Optional[dict[str, Any]]:
        """The newest run with this key, whatever its status: the same request again gets it (service.start_run)."""
        with self._lock:
            row = self._db().execute("SELECT * FROM runs WHERE run_key = ? ORDER BY created_at DESC LIMIT 1",
                                     (run_key,)).fetchone()
        return self._run_row(row) if row else None

    # ---------------------------------------------------------------- journal
    def record(self, run_id: str, kind: str, key: str, *, state: Optional[str] = None,
               status: Optional[str] = None, data: Any = None, fence: Optional[str] = None) -> bool:
        """Insert or update the row ``(run_id, kind, key)``.

        With ``fence`` the write happens only while that owner holds the run -- checked and
        written in one IMMEDIATE transaction, so a process that lost the run cannot overwrite
        the new owner's rows (docs/stategraph_design.md §5.7). Returns whether it wrote.
        """
        with self._lock:
            db = self._db()
            if fence is not None:
                db.execute("BEGIN IMMEDIATE")
            try:
                if fence is not None and not db.execute("SELECT 1 FROM runs WHERE id = ? AND owner = ?",
                                                        (run_id, fence)).fetchone():
                    db.execute("ROLLBACK")
                    return False
                existing = db.execute("SELECT seq FROM journal WHERE run_id = ? AND kind = ? AND key = ?",
                                      (run_id, kind, key)).fetchone()
                if existing:
                    db.execute("UPDATE journal SET ts = ?, state = COALESCE(?, state), status = ?, data = ?"
                               " WHERE run_id = ? AND seq = ?",
                               (utc_now(), state, status, _dumps(data), run_id, existing["seq"]))
                else:
                    seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM journal WHERE run_id = ?",
                                     (run_id,)).fetchone()[0]
                    db.execute("INSERT INTO journal (run_id, seq, ts, kind, key, state, status, data)"
                               " VALUES (?,?,?,?,?,?,?,?)",
                               (run_id, seq, utc_now(), kind, key, state, status, _dumps(data)))
                if fence is not None:
                    db.execute("COMMIT")
                return True
            except BaseException:
                if fence is not None and db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def rekey(self, run_id: str, kind: str, old_key: str, new_key: str, *, state: Optional[str] = None,
              status: Optional[str] = None, fence: Optional[str] = None) -> bool:
        sql = ("UPDATE journal SET key = ?, ts = ?, state = COALESCE(?, state), status = COALESCE(?, status)"
               " WHERE run_id = ? AND kind = ? AND key = ?")
        args: list[Any] = [new_key, utc_now(), state, status, run_id, kind, old_key]
        if fence is not None:
            sql += " AND EXISTS (SELECT 1 FROM runs WHERE id = ? AND owner = ?)"
            args += [run_id, fence]
        with self._lock:
            return self._db().execute(sql, args).rowcount == 1

    def rows(self, run_id: str, kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM journal WHERE run_id = ?"
        args: list[Any] = [run_id]
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        with self._lock:
            return [self._journal_row(r) for r in self._db().execute(sql + " ORDER BY seq", args).fetchall()]

    def has_row(self, run_id: str, kind: str, key: str) -> bool:
        with self._lock:
            return self._db().execute("SELECT 1 FROM journal WHERE run_id = ? AND kind = ? AND key = ?",
                                      (run_id, kind, key)).fetchone() is not None

    def page(self, run_id: str, *, after: int = 0, limit: int = 200,
             kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM journal WHERE run_id = ? AND seq > ?"
        args: list[Any] = [run_id, int(after)]
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        with self._lock:
            return [self._journal_row(r) for r in
                    self._db().execute(sql + " ORDER BY seq LIMIT ?", (*args, int(limit))).fetchall()]

    def tail(self, run_id: str, limit: int = 50, kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM journal WHERE run_id = ?"
        args: list[Any] = [run_id]
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        with self._lock:
            rows = self._db().execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [self._journal_row(r) for r in reversed(rows)]

    def copy_rows(self, source_run: str, target_run: str, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.record(target_run, row["kind"], row["key"], state=row.get("state"), status=row.get("status"),
                        data=row.get("data"))
