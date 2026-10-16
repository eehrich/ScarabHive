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
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

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
    nesting TEXT,
    control TEXT,
    control_answer TEXT,
    runner TEXT,
    caller_run TEXT,
    caller_request TEXT
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
-- callback URLs (the callback kind): a token's hash, the run and event it sends, until when, and whether it was used
CREATE TABLE IF NOT EXISTS callbacks (
    hash TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event TEXT NOT NULL,
    frame TEXT,
    expires_at REAL NOT NULL,
    used_at TEXT
);
-- the submachine frames each run started (prefix: its journal keys' start, path: the activity it runs under) -- a
-- machine's runs list the runs it ran in as a submachine too
CREATE TABLE IF NOT EXISTS frames (
    run_id TEXT NOT NULL,
    prefix TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    path TEXT,
    PRIMARY KEY (run_id, prefix)
);
CREATE INDEX IF NOT EXISTS frames_machine ON frames(machine_id, run_id);
-- the acquire kind (FEAT-2): who holds which slot of a semaphore shared by every run of the runs.db -- the
-- acquiring activity's key, the frame that holds it (its end releases it), and since when; sem_key: the acquire's
-- own key (one run per ticket): the capacity counts per (name, sem_key)
CREATE TABLE IF NOT EXISTS semaphore_holders (
    name TEXT NOT NULL,
    sem_key TEXT NOT NULL DEFAULT '',
    slot INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    frame TEXT NOT NULL,
    since TEXT NOT NULL,
    PRIMARY KEY (run_id, key)
);
CREATE INDEX IF NOT EXISTS semaphore_holders_name ON semaphore_holders(name, slot);
-- schedules.py: per stategraph instance, the one process that starts the slots of its schedules
CREATE TABLE IF NOT EXISTS scheduler_leases (
    instance TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    lease_until TEXT NOT NULL
);
-- a run's tags (FEAT-7): flat name -> value given at its start, value as text for the filter, raw its JSON
CREATE TABLE IF NOT EXISTS run_tags (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    raw TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);
CREATE INDEX IF NOT EXISTS run_tags_value ON run_tags(name, value);
"""
#: Trace rows that only keep the engine's books (step hashes, request counters, joins): a reader's default leaves
#: them out (DBG-7).
BOOKKEEPING = ("step", "request_seq", "vars_from", "wait", "joined")


def tag_text(value: Any) -> str:
    """A tag value as the filter compares it: text as it is, a number as JSON writes it (95, 2.5)."""
    return value if isinstance(value, str) else json.dumps(value)

#: The key of the activity that started a submachine frame: its prefix without "/m/" (and a retry's "/a<n>").
_STARTED_BY = re.compile(r"(/a\d+)?/m/$")


def _frames_from_journal(conn: sqlite3.Connection) -> None:
    """A runs.db from before ``frames``: the submachine frames its runs ended, from their journal's end rows, each
    with the path of the activity that started it and in the order they started (that activity's seq)."""
    found = []
    for row in conn.execute("SELECT run_id, seq, data FROM journal WHERE kind = 'trace' AND status = 'end'"):
        try:
            end = json.loads(row["data"] or "{}")
        except ValueError:  # a row it cannot read names no frame
            continue
        prefix, machine = (end.get("frame"), end.get("machine")) if isinstance(end, dict) else (None, None)
        if not isinstance(prefix, str) or not prefix or not isinstance(machine, str):  # the root's own end, or none
            continue
        started = conn.execute("SELECT seq, data FROM journal WHERE run_id = ? AND kind = 'activity' AND key = ?",
                               (row["run_id"], _STARTED_BY.sub("", prefix))).fetchone()
        try:
            data = json.loads(started["data"] or "{}") if started else None
        except ValueError:
            data = None
        path = data.get("path") if isinstance(data, dict) and isinstance(data.get("path"), str) else None
        found.append((row["run_id"], started["seq"] if started else row["seq"], prefix, machine, path))
    found.sort(key=lambda frame: (frame[0], frame[1]))
    conn.execute("BEGIN")  # one write, not one per frame
    try:
        conn.executemany("INSERT OR IGNORE INTO frames (run_id, prefix, machine_id, path) VALUES (?, ?, ?, ?)",
                         [(run_id, prefix, machine, path) for run_id, _, prefix, machine, path in found])
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


_JSON_COLUMNS = ("params", "mocks", "output", "error", "definition", "view", "debug", "nesting", "control",
                 "control_answer")
#: Columns a runs.db from before them lacks: added when it is opened.
_ADDED_COLUMNS = {"nesting": "TEXT", "control": "TEXT", "runner": "TEXT", "control_answer": "TEXT",
                  "caller_run": "TEXT", "caller_request": "TEXT"}
#: What a run list shows of each run (list_runs): small columns only -- the covering indexes below hold them, so a
#: list never reads a run's definition or view (they precede these columns in the row: SQLite walks their pages).
_LISTED = ("id, machine_id, status, created_at, updated_at, finished_at, final_state, error, user_id, parent_run,"
           " fork_step, run_key, caller_run")
#: The key prefix of a row a resume set aside (runner.REWOUND + its rewind number).
_REWOUND_KEY = re.compile(r"^rewound\.\d+/")
#: Answers to event requests kept per run: one request at a time, each taken within a second, and its asker reads
#: for a few seconds at most -- the last 16 hold every answer still read.
ANSWERS_KEPT = 16


def resumable_failure(error: Any) -> bool:
    """Whether a run that failed with ``error`` is not over: its activity called the failure resumable, or it is
    the engine's own (``internal``) -- a fixed engine may get past it (API-10). A resume may take it on, so the runs
    it started stay and retention keeps its journal."""
    error = error if isinstance(error, dict) else {}
    return bool(error.get("resumable")) or error.get("type") == "internal"


#: resumable_failure as SQL over a runs row.
_RESUMABLE_FAILURE = ("(status = 'failed' AND (COALESCE(json_extract(error, '$.resumable'), 0)"
                      " OR json_extract(error, '$.type') = 'internal'))")


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

    @classmethod
    def read_only(cls, path: str | Path) -> "RunStore":
        """A store that only reads -- a report on a server's runs.db: no schema, no migration, and a write fails.
        A file that is not there raises here, instead of an empty database appearing in its place."""
        store = cls(path)
        conn = sqlite3.connect(f"{store.path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        store._conn = conn
        return store

    # ---------------------------------------------------------------- plumbing
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            had_frames = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'frames'").fetchone()
            conn.executescript(_SCHEMA)
            if not had_frames:
                try:  # for the panel only: a journal it cannot read must not keep the runs from opening
                    _frames_from_journal(conn)
                except sqlite3.Error:
                    logger.warning("stategraph: frames of the runs before %s not filled from the journal", self.path,
                                   exc_info=True)
            present = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for column, kind in _ADDED_COLUMNS.items():
                if column not in present:
                    try:
                        conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
                    except sqlite3.OperationalError as exc:  # another process added it meanwhile
                        if "duplicate column" not in str(exc):
                            raise
            # the owners' poll for control requests (request_control) reads this, not the rows: it holds only
            # the few runs with a request open, and control lies behind the payloads of the rows
            conn.execute("CREATE INDEX IF NOT EXISTS runs_control ON runs(owner) WHERE control IS NOT NULL")
            # the run lists, newest first -- of all runs and of one machine's -- without a run's blobs (PROD-5)
            conn.execute(f"CREATE INDEX IF NOT EXISTS runs_list ON runs(created_at, {_LISTED})")
            conn.execute(f"CREATE INDEX IF NOT EXISTS runs_list_machine ON runs(machine_id, created_at, {_LISTED})")
            # a semaphore_holders table from before the acquire's key (FEAT-2): every hold is of the key ''
            if "sem_key" not in {row["name"] for row in conn.execute("PRAGMA table_info(semaphore_holders)")}:
                try:
                    conn.execute("ALTER TABLE semaphore_holders ADD COLUMN sem_key TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError as exc:  # another process added it meanwhile
                    if "duplicate column" not in str(exc):
                        raise
            conn.execute("CREATE INDEX IF NOT EXISTS semaphore_holders_key ON semaphore_holders(name, sem_key, slot)")
            # the runs a run started from inside its call activities (service.start_run): its children
            conn.execute("CREATE INDEX IF NOT EXISTS runs_caller ON runs(caller_run) WHERE caller_run IS NOT NULL")
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
                   nesting: Any = None, runner: Optional[str] = None) -> None:
        """A fork's row comes with its lifecycle row ``forked`` in one transaction: the row that tells a fork whose
        prefix copy broke off (no ``fork_resources`` trace row after it) from a whole one (RES-6)."""
        now = utc_now()
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO runs (id, machine_id, status, created_at, updated_at, params, mocks, definition,"
                    " debug, user_id, session_id, parent_run, fork_step, run_key, owner, lease_until, journal_format,"
                    " nesting, runner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, machine_id, status, now, now, _dumps(params), _dumps(mocks), _dumps(definition),
                     _dumps(debug), user_id, session_id, parent_run, fork_step, run_key, owner, lease_until,
                     journal_format, _dumps(nesting), runner))
                if parent_run:
                    self._put_lifecycle(db, run_id, "forked", {"from": parent_run, "at_step": fork_step,
                                                               "user": user_id, "owner": owner})
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def half_made_fork(self, run_id: str) -> bool:
        """A fork whose making broke off after its row (RES-6): its ``forked`` row, but no ``fork_resources`` row,
        which the engine writes once the source's prefix is copied. A resume would run the rest of that prefix
        live."""
        with self._lock:
            db = self._db()
            forked = db.execute("SELECT 1 FROM journal WHERE run_id = ? AND kind = 'lifecycle' AND status = 'forked'",
                                (run_id,)).fetchone()
            return bool(forked) and not db.execute(
                "SELECT 1 FROM journal WHERE run_id = ? AND kind = 'trace' AND key = 'fork_resources'",
                (run_id,)).fetchone()

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

    def list_runs(self, machine_id: Optional[str] = None, limit: int = 50, *, status: Optional[str] = None,
                  user_id: Optional[str] = None, all_users: bool = True,
                  before: Optional[str] = None, nested: bool = False,
                  tags: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """The newest runs; ``all_users=False``: only ``user_id``'s (None: the runs of nobody) -- what that user may
        see; ``before``: a run id -- the runs listed after it (older, or as old and of a smaller id); ``nested``: with
        ``machine_id``, also the runs of other machines it ran in as a submachine; ``tags``: only runs with every
        one of these tags (FEAT-7). Each run comes with its ``tags``."""
        sql = f"SELECT {_LISTED} FROM runs"
        where: list[str] = []
        args: list[Any] = []
        if machine_id and nested:
            where.append("(machine_id = ? OR id IN (SELECT run_id FROM frames WHERE machine_id = ?))")
            args += [machine_id, machine_id]
        elif machine_id:
            where.append("machine_id = ?")
            args.append(machine_id)
        if status:
            where.append("status = ?")
            args.append(status)
        if not all_users:  # a run of nobody is not every user's (API-3): only a caller without a user sees it
            where.append("user_id IS ?")
            args.append(user_id)
        for name, value in (tags or {}).items():
            where.append("id IN (SELECT run_id FROM run_tags WHERE name = ? AND value = ?)")
            args += [name, tag_text(value)]
        if before:
            where.append("(created_at, id) < (SELECT created_at, id FROM runs WHERE id = ?)")
            args.append(before)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db().execute(sql, args).fetchall()
        runs = []
        for row in rows:
            data = dict(row)
            data["error"] = _loads(data.get("error"))
            runs.append(data)
        tagged = self.tags_of([run["id"] for run in runs])
        for run in runs:
            run["tags"] = tagged.get(run["id"], {})
        return runs

    def set_tags(self, run_id: str, tags: dict[str, Any]) -> None:
        """A run's tags (FEAT-7), given at its start; a fork copies its source's."""
        with self._lock:
            self._db().executemany("INSERT OR REPLACE INTO run_tags (run_id, name, value, raw) VALUES (?, ?, ?, ?)",
                                   [(run_id, name, tag_text(value), json.dumps(value)) for name, value in tags.items()])

    def tags_of(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        """{run id: {name: value}} of these runs, the ones with tags."""
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(run_ids), 500):  # SQLite's variable limit
            chunk = run_ids[start:start + 500]
            with self._lock:
                rows = self._db().execute(f"SELECT run_id, name, raw FROM run_tags WHERE run_id IN "
                                          f"({','.join('?' * len(chunk))})", chunk).fetchall()
            for row in rows:
                found.setdefault(row["run_id"], {})[row["name"]] = json.loads(row["raw"])
        return found

    def mark_expired(self, *, now: str) -> list[str]:
        """Active runs whose owner stopped renewing the lease: ``interrupted``, resumable.

        Only an expired lease counts -- a process that merely starts never takes
        a run that another live process owns (docs/stategraph_design.md §5.7).
        """
        return [run_id for run_id, _ in self.sweep(now=now)]

    def sweep(self, *, now: str, gone: Optional[Callable[[str, Optional[str]], bool]] = None
              ) -> list[tuple[str, str]]:
        """``mark_expired``, and also the active runs whose owner ``gone(owner, lease_until)`` says is dead (a
        process of this host that is no longer there): those lose their lease at once, so a resume can take them
        now, not once it ran out. ``[(run id, why)]``."""
        active = ",".join("?" * len(ACTIVE_STATUSES))
        with self._lock:
            rows = self._db().execute(f"SELECT id, owner, lease_until FROM runs WHERE status IN ({active})",
                                      ACTIVE_STATUSES).fetchall()
            swept = []
            for row in rows:  # the condition again AT WRITE TIME: a run renewed or finished meanwhile is left alone
                if row["lease_until"] is None or row["lease_until"] < now:
                    cursor = self._db().execute(
                        f"UPDATE runs SET status = 'interrupted', control = NULL, updated_at = ?"  # nobody takes it now
                        f" WHERE id = ? AND status IN ({active})"
                        " AND (lease_until IS NULL OR lease_until < ?)", (utc_now(), row["id"], *ACTIVE_STATUSES, now))
                    why = "its lease ran out"
                elif gone is not None and row["owner"] and gone(row["owner"], row["lease_until"]):
                    cursor = self._db().execute(
                        f"UPDATE runs SET status = 'interrupted', control = NULL, lease_until = ?, updated_at = ?"
                        f" WHERE id = ? AND status IN ({active}) AND owner = ? AND lease_until = ?",
                        ("", utc_now(), row["id"], *ACTIVE_STATUSES, row["owner"], row["lease_until"]))
                    why = f"its process is gone ({row['owner']})"
                else:
                    continue
                if cursor.rowcount == 1:
                    swept.append((row["id"], why))
        return swept

    def lifecycle(self, run_id: str, what: str, data: dict[str, Any]) -> None:
        """A row of kind ``lifecycle`` -- a start, resume, fork, sweep, close: who did it and why (DBG-8). In one
        write transaction: whoever writes it need not hold the run, and its seq must not race the owner's rows.
        Never read by a replay or a fork."""
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                self._put_lifecycle(db, run_id, what, data)
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _put_lifecycle(db: sqlite3.Connection, run_id: str, what: str, data: dict[str, Any]) -> None:
        seq, count = db.execute("SELECT COALESCE(MAX(seq), 0) + 1, (SELECT COUNT(*) FROM journal WHERE run_id = ?"
                                " AND kind = 'lifecycle') FROM journal WHERE run_id = ?", (run_id, run_id)).fetchone()
        now = utc_now()
        db.execute("INSERT INTO journal (run_id, seq, ts, kind, key, state, status, data) VALUES (?,?,?,?,?,?,?,?)",
                   (run_id, seq, now, "lifecycle", f"{what}:{count + 1}", None, what, _dumps({"at": now, **data})))

    def children(self, run_id: str, limit: int = 200, *, unended: bool = False) -> list[dict[str, Any]]:
        """The runs started from inside this run's call activities (service.start_run), oldest first.
        ``unended``: only those that have not ended -- all of them, no limit (what a caller's end or pause acts on)."""
        sql = ("SELECT id, machine_id, status, created_at, finished_at, caller_request FROM runs WHERE caller_run = ?"
               + (f" AND status NOT IN ({', '.join('?' * len(TERMINAL_STATUSES))})" if unended else "")
               + " ORDER BY created_at, id LIMIT ?")
        args = (run_id, *(TERMINAL_STATUSES if unended else ()), -1 if unended else int(limit))
        with self._lock:
            rows = self._db().execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def ended_activity_keys(self, run_id: str) -> set[str]:
        """The keys of the run's activities that have an outcome (done, error): what ended_without_outcome reads."""
        with self._lock:
            return {key for (key,) in self._db().execute(
                "SELECT key FROM journal WHERE run_id = ? AND kind = 'activity' AND status IN ('done', 'error')",
                (run_id,)).fetchall()}

    def compact_ended(self, *, before: str, limit: int = 50) -> list[str]:
        """Retention (FEAT-11): the journal of up to ``limit`` runs that ended before ``before`` dropped -- their
        runs row stays (status, output, error, run_key: a key's answer, a fork's origin), so do their lifecycle
        rows, and a row ``compacted`` that keeps what they spent. Never a run that is not over: active,
        interrupted, or failed resumably (resumable_failure: a resume runs it on). The run ids compacted."""
        done = []
        for run_id in self._compactable(before, limit):
            with self._lock:  # per run, not per batch: the event loop's journal writes wait one run at most
                db = self._db()
                db.execute("BEGIN IMMEDIATE")
                try:  # another process may have compacted or resumed it since the selection
                    if db.execute("SELECT 1 FROM runs WHERE id = ? AND status IN ('succeeded', 'failed', 'cancelled')"
                                  f" AND NOT {_RESUMABLE_FAILURE}"
                                  " AND EXISTS (SELECT 1 FROM journal WHERE run_id = runs.id AND kind != 'lifecycle')",
                                  (run_id,)).fetchone() is None:
                        db.execute("COMMIT")
                        continue
                    spent = self.spend(run_id)
                    dropped = db.execute("DELETE FROM journal WHERE run_id = ? AND kind != 'lifecycle'",
                                         (run_id,)).rowcount
                    self._put_lifecycle(db, run_id, "compacted", {"rows": dropped, "spend": spent,
                                                                  "older_than": before})
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
            done.append(run_id)
        return done

    def _compactable(self, before: str, limit: int) -> list[str]:
        with self._lock:
            return [row[0] for row in self._db().execute(
                "SELECT id FROM runs WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at < ?"
                f" AND NOT {_RESUMABLE_FAILURE}"
                " AND EXISTS (SELECT 1 FROM journal WHERE run_id = runs.id AND kind != 'lifecycle')"
                " ORDER BY finished_at LIMIT ?", (before, int(limit))).fetchall()]

    def compacted(self, run_id: str) -> bool:
        with self._lock:
            return self._db().execute("SELECT 1 FROM journal WHERE run_id = ? AND kind = 'lifecycle'"
                                      " AND status = 'compacted'", (run_id,)).fetchone() is not None

    def update_debug_unowned(self, run_id: str, debug: Any, *, now: str) -> bool:
        """Change a run's breakpoints from outside its owner -- only while nobody holds a live lease on it.

        The same rule as ``take_lease``: the status is no escape (a swept run's owner may still be alive).
        """
        with self._lock:
            cursor = self._db().execute(
                "UPDATE runs SET debug = ?, updated_at = ? WHERE id = ? AND (lease_until IS NULL OR lease_until < ?)",
                (_dumps(debug), utc_now(), run_id, now))
            return cursor.rowcount == 1

    def request_control(self, run_id: str, request: dict[str, Any], *, now: str) -> str:
        """Ask the process that holds a live lease on the run for a control action: it takes the request within a
        second (take_controls). ``requested``; ``open`` while another request waits for it (one at a time: a second
        would replace the first unseen); ``unheld`` when nobody holds a live lease -- no process would take it."""
        with self._lock:
            db = self._db()
            if db.execute("UPDATE runs SET control = ?, updated_at = ? WHERE id = ? AND owner IS NOT NULL"
                          " AND lease_until >= ? AND control IS NULL",
                          (_dumps(request), utc_now(), run_id, now)).rowcount:
                return "requested"
            row = db.execute("SELECT control, lease_until FROM runs WHERE id = ?", (run_id,)).fetchone()
            return "open" if row and row["control"] is not None and (row["lease_until"] or "") >= now else "unheld"

    def take_controls(self, owner: str) -> list[tuple[str, dict[str, Any]]]:
        """The control requests for the runs ``owner`` holds, each cleared as it is taken -- an event's in one
        transaction with ``{"taken": true}`` under its id: a sweep or a new owner clears a request too, and its
        asker tells the event dropped from one taken that is still to be answered."""
        with self._lock:
            db = self._db()
            rows = db.execute("SELECT id, control FROM runs WHERE owner = ? AND control IS NOT NULL",
                              (owner,)).fetchall()
            taken = []
            for row in rows:
                request = _loads(row["control"])
                try:
                    db.execute("BEGIN IMMEDIATE")
                    cleared = db.execute("UPDATE runs SET control = NULL WHERE id = ? AND control = ?",
                                         (row["id"], row["control"])).rowcount
                    if cleared and isinstance(request, dict) and request.get("action") == "event":
                        self._put_answer(db, row["id"], str(request.get("id")), {"taken": True})
                    db.execute("COMMIT")
                except BaseException as exc:
                    if db.in_transaction:  # SQLite may have rolled it back itself
                        db.execute("ROLLBACK")
                    if not taken or not isinstance(exc, Exception):
                        raise
                    # the ones taken before are cleared for good: they are carried out, this one waits for the next look
                    logger.warning("stategraph: control request for run %s left for the next look: %s", row["id"], exc)
                    break
                if cleared:
                    taken.append((row["id"], request))
            return taken

    def answer_control(self, run_id: str, request_id: str, answer: dict[str, Any]) -> None:
        """What the process that took a request wanting an answer (an event) made of it, kept under the request's id
        with the last few: the next request's answer does not replace one its asker has not read yet. One write
        transaction, as the mark that it was taken: a process that lost the run since answers too, and must not
        drop the new owner's mark."""
        with self._lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                self._put_answer(db, run_id, request_id, answer)
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _put_answer(db: sqlite3.Connection, run_id: str, request_id: str, answer: dict[str, Any]) -> None:
        row = db.execute("SELECT control_answer FROM runs WHERE id = ?", (run_id,)).fetchone()
        answers = _loads(row["control_answer"]) if row is not None else None
        answers = answers if isinstance(answers, dict) else {}
        answers.pop(request_id, None)
        answers[request_id] = answer
        db.execute("UPDATE runs SET control_answer = ? WHERE id = ?",
                   (_dumps(dict(list(answers.items())[-ANSWERS_KEPT:])), run_id))

    def control_answer(self, run_id: str, request_id: str) -> Optional[dict[str, Any]]:
        """The owner's answer to the request ``request_id``, once it is there."""
        with self._lock:
            row = self._db().execute("SELECT control_answer FROM runs WHERE id = ?", (run_id,)).fetchone()
        answers = _loads(row["control_answer"]) if row is not None else None
        answer = answers.get(request_id) if isinstance(answers, dict) else None
        return answer if isinstance(answer, dict) else None

    def withdraw_control(self, run_id: str, request: dict[str, Any]) -> bool:
        """Take back a request its owner has not taken: whether it was still there."""
        with self._lock:
            return self._db().execute("UPDATE runs SET control = NULL WHERE id = ? AND control = ?",
                                      (run_id, _dumps(request))).rowcount == 1

    def take_lease(self, run_id: str, owner: str, until: str, *, now: str) -> bool:
        """Take the run for ``owner`` if nobody holds a live lease on it (one conditional update)."""
        with self._lock:
            cursor = self._db().execute(
                "UPDATE runs SET control = CASE WHEN owner = ? THEN control END,"  # asked of the owner before
                " owner = ?, lease_until = ?, updated_at = ? WHERE id = ?"
                " AND (lease_until IS NULL OR lease_until < ? OR owner = ?)",
                (owner, owner, until, utc_now(), run_id, now, owner))
            return cursor.rowcount == 1

    def add_callback(self, digest: str, run_id: str, event: str, frame: Optional[str], expires_at: float, *,
                     now: float) -> None:
        """Keep a callback -- and drop the ones that expired: no URL of theirs holds any more."""
        with self._lock:
            db = self._db()
            db.execute("DELETE FROM callbacks WHERE expires_at <= ?", (now,))
            db.execute("INSERT INTO callbacks (hash, run_id, event, frame, expires_at) VALUES (?, ?, ?, ?, ?)",
                       (digest, run_id, event, frame, expires_at))

    def callback(self, digest: str, now: float) -> Optional[dict[str, Any]]:
        """A callback that still holds (not used, not expired), or None."""
        with self._lock:
            row = self._db().execute("SELECT * FROM callbacks WHERE hash = ? AND used_at IS NULL AND expires_at > ?",
                                     (digest, now)).fetchone()
        return dict(row) if row else None

    def callback_row(self, digest: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db().execute("SELECT * FROM callbacks WHERE hash = ?", (digest,)).fetchone()
        return dict(row) if row else None

    def use_callback(self, digest: str, now: float) -> bool:
        """Claim a callback for one use (one conditional update: two requests at once, one wins)."""
        with self._lock:
            cursor = self._db().execute("UPDATE callbacks SET used_at = ? WHERE hash = ? AND used_at IS NULL AND "
                                        "expires_at > ?", (utc_now(), digest, now))
            return cursor.rowcount == 1

    def move_callback(self, digest: str, run_id: str, now: float) -> bool:
        """A callback that still holds, sent to ``run_id`` from now on (a fork of an ended run takes its URL)."""
        with self._lock:
            return self._db().execute("UPDATE callbacks SET run_id = ? WHERE hash = ? AND used_at IS NULL AND "
                                      "expires_at > ?", (run_id, digest, now)).rowcount == 1

    def unuse_callback(self, digest: str) -> None:
        """The event was not taken (its data did not fit): the URL holds again."""
        with self._lock:
            self._db().execute("UPDATE callbacks SET used_at = NULL WHERE hash = ?", (digest,))

    def take_scheduler(self, instance: str, owner: str, until: str, *, now: str) -> bool:
        """Hold the scheduler of ``instance`` for ``owner`` until ``until``, if nobody else holds a live lease on
        it -- instances that share a runs.db schedule each their own."""
        with self._lock:
            db = self._db()
            db.execute("INSERT OR IGNORE INTO scheduler_leases (instance, owner, lease_until) VALUES (?, ?, ?)",
                       (instance, owner, until))
            cursor = db.execute("UPDATE scheduler_leases SET owner = ?, lease_until = ? WHERE instance = ? AND "
                                "(owner = ? OR lease_until < ?)", (owner, until, instance, owner, now))
            return cursor.rowcount == 1

    def release_scheduler(self, instance: str, owner: str) -> None:
        """Give up the lease ``owner`` holds: a restarted process schedules at once, not after it ran out."""
        with self._lock:
            self._db().execute("UPDATE scheduler_leases SET lease_until = '' WHERE instance = ? AND owner = ?",
                               (instance, owner))

    # ---------------------------------------------------------------- semaphores (FEAT-2)
    #: A holder row holds while its run is active and its lease has not run out longer ago than the hold period
    #: (a process that died), or while its run is interrupted for less than the hold period; a row of an ended or
    #: deleted run holds nothing. ``cutoff`` is now minus the hold period.
    _HOLDING = ("SELECT h.slot, h.run_id, h.key, h.since FROM semaphore_holders h JOIN runs r ON r.id = h.run_id"
                " WHERE h.name = ? AND h.sem_key = ? AND ((r.status IN ('running', 'paused', 'waiting') AND r.lease_until > ?)"
                " OR (r.status = 'interrupted' AND r.updated_at > ?))")

    def acquire_slot(self, name: str, capacity: int, run_id: str, key: str, frame: str, *, cutoff: str,
                     fence: Optional[str] = None, sem_key: str = "") -> Optional[int]:
        """Take the lowest free slot of semaphore ``name`` -- of its ``sem_key``: ``capacity`` counts per key --
        for the activity ``key`` of ``run_id`` -- the slot it
        took already, when it did (a crash before its outcome was journaled). None while ``capacity`` slots are
        held. A held row is kept even when its hold ran out: its run, resumed, waits for its slot (reclaim_slots).
        Raises PermissionError when ``fence`` no longer owns the run."""
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                if fence is not None and not db.execute("SELECT 1 FROM runs WHERE id = ? AND owner = ?",
                                                        (run_id, fence)).fetchone():
                    raise PermissionError(run_id)
                own = db.execute("SELECT slot FROM semaphore_holders WHERE run_id = ? AND key = ?",
                                 (run_id, key)).fetchone()
                if own is not None:
                    db.execute("COMMIT")
                    return int(own["slot"])
                # a frame holds one slot of a semaphore's key: an acquire it reaches again (a loop back) gets it
                mine = db.execute("SELECT slot FROM semaphore_holders WHERE run_id = ? AND frame = ? AND name = ?"
                                  " AND sem_key = ? LIMIT 1", (run_id, frame, name, sem_key)).fetchone()
                if mine is not None:
                    db.execute("INSERT INTO semaphore_holders (name, sem_key, slot, run_id, key, frame, since)"
                               " VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (name, sem_key, int(mine["slot"]), run_id, key, frame, utc_now()))
                    db.execute("COMMIT")
                    return int(mine["slot"])
                # rows of ended or deleted runs hold nothing: gone, so the table stays small. NOT EXISTS looks up
                # each holder's run by its key; a NOT IN list walked every run on each try of every waiter (RT-1)
                db.execute("DELETE FROM semaphore_holders WHERE name = ? AND NOT EXISTS (SELECT 1 FROM runs"
                           " WHERE runs.id = semaphore_holders.run_id"
                           " AND runs.status NOT IN ('succeeded', 'failed', 'cancelled'))", (name,))
                busy = {int(row["slot"]) for row in db.execute(self._HOLDING, (name, sem_key, cutoff, cutoff))}
                free = [slot for slot in range(capacity) if slot not in busy]
                if len(busy) >= capacity or not free:
                    db.execute("COMMIT")
                    return None
                db.execute("INSERT INTO semaphore_holders (name, sem_key, slot, run_id, key, frame, since)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)", (name, sem_key, free[0], run_id, key, frame, utc_now()))
                db.execute("COMMIT")
                return free[0]
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def reclaim_slots(self, run_id: str, *, cutoff: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """A resumed run's holds ``[{name, slot, key}]`` (``sem_key`` too when the acquire had one), and of them the ones another run took over meanwhile --
        its hold had run out, and a holder that still holds took the slot after it (a later ``since``). When none
        was taken over, the run holds every one again from now (``since``: a holder that took one over before and
        resumes later waits for it in turn)."""
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                own = db.execute("SELECT name, sem_key, slot, key, since FROM semaphore_holders WHERE run_id = ?",
                                 (run_id,)).fetchall()
                held = [{"name": row["name"], "slot": int(row["slot"]), "key": row["key"],
                         **({"sem_key": row["sem_key"]} if row["sem_key"] else {})} for row in own]
                taken = [hold for hold, row in zip(held, own)
                         if any(other["run_id"] != run_id and int(other["slot"]) == int(row["slot"])
                                and other["since"] > row["since"]
                                for other in db.execute(self._HOLDING,
                                                        (row["name"], row["sem_key"], cutoff, cutoff)))]
                if own and not taken:
                    db.execute("UPDATE semaphore_holders SET since = ? WHERE run_id = ?", (utc_now(), run_id))
                db.execute("COMMIT")
                return held, taken
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def claim_slot(self, name: str, slot: int, run_id: str, key: str, frame: str, sem_key: str = "") -> bool:
        """A replayed acquire's slot, held by the run unless it holds it already -- with the oldest ``since``: a
        holder of that slot now keeps it (reclaim_slots lets the run wait for it). Whether it was claimed now."""
        with self._lock:
            return self._db().execute("INSERT OR IGNORE INTO semaphore_holders (name, sem_key, slot, run_id, key,"
                                      " frame, since) VALUES (?, ?, ?, ?, ?, ?, '')",
                                      (name, sem_key, slot, run_id, key, frame)).rowcount == 1

    def release_slots(self, run_id: str, frame: str, name: Optional[str] = None, *, below: bool = False,
                      sem_key: Optional[str] = None) -> list[dict[str, Any]]:
        """Release what ``frame`` of the run holds of semaphore ``name`` (of its ``sem_key`` only, when given), or
        of every one; ``below``: and what the
        frames below it hold (the root frame's end: everything the run holds). The released holds, ``[{name,
        slot}]``."""
        where = "run_id = ? AND " + ("substr(frame, 1, ?) = ?" if below else "frame = ?")
        args: list[Any] = [run_id, *((len(frame),) if below else ()), frame]
        if name is not None:
            where += " AND name = ?"
            args.append(name)
        if sem_key is not None:
            where += " AND sem_key = ?"
            args.append(sem_key)
        with self._lock:
            db = self._db()
            rows = db.execute(f"SELECT name, slot FROM semaphore_holders WHERE {where}", args).fetchall()
            if rows:
                db.execute(f"DELETE FROM semaphore_holders WHERE {where}", args)
        return [{"name": row["name"], "slot": int(row["slot"])} for row in rows]

    def semaphore_holders(self, name: str) -> list[dict[str, Any]]:
        """Every row of semaphore ``name`` (holding or not), oldest first: for a reader -- the panel, a test."""
        with self._lock:
            rows = self._db().execute("SELECT slot, run_id, key, frame, since, sem_key FROM semaphore_holders"
                                      " WHERE name = ?"
                                      " ORDER BY since", (name,)).fetchall()
        return [dict(row) for row in rows]

    def count_by_key(self, run_key: str) -> int:
        """How many runs had this key: a schedule's slot tries at most so many (schedules.ATTEMPTS)."""
        with self._lock:
            return self._db().execute("SELECT COUNT(*) FROM runs WHERE run_key = ?", (run_key,)).fetchone()[0]

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
        """Insert or update the row ``(run_id, kind, key)``. A row written again as it stands (a resume's replay)
        is left alone: its ``ts`` stays the time it was first written.

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
                existing = db.execute("SELECT seq, state, status, data FROM journal WHERE run_id = ? AND kind = ?"
                                      " AND key = ?", (run_id, kind, key)).fetchone()
                if existing and existing["status"] == status and existing["data"] == _dumps(data) and (
                        state is None or existing["state"] == state):
                    pass  # a replay writing the row again: it keeps its time -- when it happened (RES-3)
                elif existing:
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

    def set_aside(self, run_id: str, rows: list[tuple[str, str]], prefix: str, *, status: str, data: Any,
                  fence: str) -> None:
        """Move the journal rows ``(kind, key)`` to ``<prefix>/<key>`` and record the trace row ``prefix`` that
        names them -- all or nothing, in one IMMEDIATE transaction, while ``fence`` holds the run (a resume's
        rewind, docs/stategraph_design.md §5.3). Raises ValueError when the owner lost the run or a row is gone."""
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                if not db.execute("SELECT 1 FROM runs WHERE id = ? AND owner = ?", (run_id, fence)).fetchone():
                    raise ValueError(f"run {run_id}: not held by {fence}")
                now = utc_now()
                for kind, key in rows:
                    if db.execute("UPDATE journal SET key = ?, ts = ? WHERE run_id = ? AND kind = ? AND key = ?",
                                  (f"{prefix}/{key}", now, run_id, kind, key)).rowcount != 1:
                        raise ValueError(f"run {run_id}: no journal row {kind} {key} to set aside")
                seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM journal WHERE run_id = ?",
                                 (run_id,)).fetchone()[0]
                db.execute("INSERT INTO journal (run_id, seq, ts, kind, key, state, status, data)"
                           " VALUES (?,?,?,?,?,?,?,?)", (run_id, seq, now, "trace", prefix, None, status, _dumps(data)))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def rows(self, run_id: str, kinds: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM journal WHERE run_id = ?"
        args: list[Any] = [run_id]
        if kinds:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        with self._lock:
            return [self._journal_row(r) for r in self._db().execute(sql + " ORDER BY seq", args).fetchall()]

    def callback_urls(self, run_id: str) -> list[str]:
        """The URLs the run's callback activities answered with -- read from those rows only, not every activity's."""
        sql = ("SELECT json_extract(data, '$.out.url') FROM journal WHERE run_id = ? AND kind = 'activity'"
               " AND json_extract(data, '$.kind') = 'callback'")
        with self._lock:
            return [url for (url,) in self._db().execute(sql, (run_id,)).fetchall() if isinstance(url, str)]

    def has_row(self, run_id: str, kind: str, key: str) -> bool:
        with self._lock:
            return self._db().execute("SELECT 1 FROM journal WHERE run_id = ? AND kind = ? AND key = ?",
                                      (run_id, kind, key)).fetchone() is not None

    @staticmethod
    def _filters(kinds: Optional[Iterable[str]], state: Optional[str], statuses: Optional[Iterable[str]],
                 bookkeeping: bool) -> tuple[str, list[Any]]:
        """The WHERE tail of a journal read: kinds, state, statuses; ``bookkeeping=False`` leaves BOOKKEEPING out."""
        sql, args = "", []
        for column, values in (("kind", kinds), ("status", statuses)):
            if values:
                values = list(values)
                sql += f" AND {column} IN ({','.join('?' * len(values))})"
                args += values
        if state is not None:
            sql += " AND state = ?"
            args.append(state)
        if not bookkeeping:
            sql += f" AND NOT (kind = 'trace' AND status IN ({','.join('?' * len(BOOKKEEPING))}))"
            args += BOOKKEEPING
        return sql, args

    def page(self, run_id: str, *, after: int = 0, limit: int = 200,
             kinds: Optional[Iterable[str]] = None, state: Optional[str] = None,
             statuses: Optional[Iterable[str]] = None, bookkeeping: bool = True) -> list[dict[str, Any]]:
        where, extra = self._filters(kinds, state, statuses, bookkeeping)
        sql = "SELECT * FROM journal WHERE run_id = ? AND seq > ?" + where
        args: list[Any] = [run_id, int(after), *extra]
        with self._lock:
            return [self._journal_row(r) for r in
                    self._db().execute(sql + " ORDER BY seq LIMIT ?", (*args, int(limit))).fetchall()]

    def row_at(self, run_id: str, seq: int) -> Optional[dict[str, Any]]:
        """One journal row, whole."""
        with self._lock:
            row = self._db().execute("SELECT * FROM journal WHERE run_id = ? AND seq = ?", (run_id, int(seq))).fetchone()
        return self._journal_row(row) if row else None

    def tail(self, run_id: str, limit: int = 50, kinds: Optional[Iterable[str]] = None,
             state: Optional[str] = None, *, before: Optional[int] = None,
             bookkeeping: bool = True) -> list[dict[str, Any]]:
        """The last ``limit`` rows -- with ``before``, the last ones before that seq (paging backwards)."""
        where, args = self._filters(kinds, state, None, bookkeeping)
        sql = "SELECT * FROM journal WHERE run_id = ?" + where
        args = [run_id, *args]
        if before is not None:
            sql += " AND seq < ?"
            args.append(int(before))
        with self._lock:
            rows = self._db().execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [self._journal_row(r) for r in reversed(rows)]

    def add_frame(self, run_id: str, prefix: str, machine_id: str, path: Optional[str]) -> None:
        """A submachine frame the run started -- again on a replay: kept once."""
        with self._lock:
            self._db().execute("INSERT OR IGNORE INTO frames (run_id, prefix, machine_id, path) VALUES (?, ?, ?, ?)",
                               (run_id, prefix, machine_id, path))

    def frames(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """The submachine frames the run started, the first ``limit`` in the order they started."""
        with self._lock:
            rows = self._db().execute("SELECT prefix, machine_id AS machine, path FROM frames WHERE run_id = ?"
                                      " ORDER BY rowid LIMIT ?", (run_id, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def copy_rows(self, source_run: str, target_run: str, rows: Iterable[dict[str, Any]]) -> None:
        """A fork's prefix: the source run's rows, which the fork replays. An activity's says whose it is
        (``meta.copied_from``): what it cost, the source run spent (spend) -- or, a row the source had copied
        itself, the run that did."""
        for row in rows:
            data = row.get("data")
            if row["kind"] == "activity" and isinstance(data, dict):
                meta = data.get("meta") or {}
                data = {**data, "meta": {**meta, "copied_from": meta.get("copied_from") or source_run}}
            self.record(target_run, row["kind"], row["key"], state=row.get("state"), status=row.get("status"),
                        data=data)

    def run_cost(self, run_id: str) -> Optional[float]:
        """The run's run.cost as its next resume would count it (RunContext.cost): ``meta.cost`` over its activity
        outcomes (done, error) -- a fork's copied ones too, the rows a resume set aside not. A ``started`` row's
        cost is not in it: an attempt in flight counts once its outcome takes it over, and one a stop cut (a
        fail-fast join's sibling) never ends -- ``spend`` counts that money. None without activity rows (none ran,
        or retention dropped them): ``spend`` answers then."""
        with self._lock:
            count, cost = self._db().execute(
                "SELECT COUNT(*), TOTAL(CASE WHEN status IN ('done', 'error') THEN json_extract(data, '$.meta.cost')"
                " END) FROM journal WHERE run_id = ? AND kind = 'activity' AND key NOT GLOB 'rewound.*'",
                (run_id,)).fetchone()
        return cost if count else None

    def spend(self, run_id: str, *, children: bool = False, _seen: Optional[set[str]] = None
              ) -> Optional[dict[str, Any]]:
        """What the run's agent and decide activities cost, its submachines' included: the sum of their
        ``meta`` (backend._Spend), read from the journal itself -- so a resume, which replays rows instead of
        running them, counts nothing twice, and nothing has to be kept beside it. Rows a fork copied from its
        source are the source's spend. ``children=True``: with what the runs it started from its call activities spent
        (service.start_run; their spend stays their own too) -- under the state of the calling activity; off by
        default, so a sum over a run and its children (a flow's report, tracker's cap) counts each once. A compacted run (retention) answers what it had spent. None while nothing
        reported any (mock-only, tools only)."""
        seen = (_seen or set()) | {run_id}
        sql = ("SELECT COUNT(json_extract(data, '$.meta.cost')), TOTAL(json_extract(data, '$.meta.cost')),"
               " TOTAL(json_extract(data, '$.meta.tokens.prompt')), TOTAL(json_extract(data, '$.meta.tokens.completion')),"
               " TOTAL(json_extract(data, '$.meta.tokens.cached')), MAX(json_extract(data, '$.meta.cost_is_estimate')),"
               " TOTAL(json_extract(data, '$.meta.cost_unpriced_calls'))"
               " FROM journal WHERE run_id = ? AND kind = 'activity'"
               " AND json_extract(data, '$.meta.copied_from') IS NULL")
        with self._lock:
            db = self._db()
            priced, cost, prompt, completion, cached, estimated, unpriced = db.execute(sql, (run_id,)).fetchone()
            kept = db.execute("SELECT json_extract(data, '$.spend') FROM journal WHERE run_id = ? AND kind ="
                              " 'lifecycle' AND status = 'compacted' ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
            calls = (db.execute("SELECT id, caller_request FROM runs WHERE caller_run = ?", (run_id,)).fetchall()
                     if children else [])
        compacted = _loads(kept[0]) if kept and kept[0] else None
        own_by_state: Optional[list[dict[str, Any]]] = None
        if not priced and not unpriced and isinstance(compacted, dict):  # its journal is gone: what it kept
            tokens = compacted.get("tokens") or {}
            priced, cost, unpriced = compacted.get("activities", 0), compacted.get("cost", 0.0), \
                compacted.get("cost_unpriced_calls", 0)
            prompt, completion, cached = tokens.get("prompt", 0), tokens.get("completion", 0), tokens.get("cached", 0)
            estimated = compacted.get("cost_is_estimate")
            own_by_state = list(compacted.get("by_state") or [])
        extra = []  # each child's spend, under the activity that started it
        for child_id, request in calls:
            if child_id in seen:
                continue
            spent = self.spend(child_id, children=True, _seen=seen)
            if not spent:
                continue
            priced += spent["activities"]
            cost += spent["cost"]
            prompt += spent["tokens"]["prompt"]
            completion += spent["tokens"]["completion"]
            cached += spent["tokens"].get("cached", 0)
            unpriced += spent.get("cost_unpriced_calls", 0)
            estimated = estimated or spent.get("cost_is_estimate")
            extra.append((request, spent))
        if not priced and not unpriced:
            return None
        by_state = own_by_state if own_by_state is not None else self._spend_by_state(run_id, extra)
        return {"cost": round(cost, 6), "activities": priced,
                "tokens": {"prompt": int(prompt), "completion": int(completion), "cached": int(cached)},
                "cost_is_estimate": bool(estimated), "cost_unpriced_calls": int(unpriced),
                **({"by_state": by_state} if len(by_state) > 1 else {})}  # one state: the total says it all

    def _spend_by_state(self, run_id: str, extra: Iterable[tuple[Optional[str], dict[str, Any]]] = ()
                        ) -> list[dict[str, Any]]:
        """The spend per state, by its path through the submachines (``phase/deeper/wait``): an activity counts to
        the state it ran in -- a parallel's or a sequence's children to theirs -- and a state that runs a submachine
        holds what that machine's states cost: the cost of a phase. ``extra``: a child run's spend, counted to the
        state of the activity whose request id started it (none while that call runs: its row names the id only
        once it ended). Ordered as the paths sort, a state above its children."""
        sql = ("SELECT key, json_extract(data, '$.state'), json_extract(data, '$.meta.cost'),"
               " json_extract(data, '$.meta.tokens.prompt'), json_extract(data, '$.meta.tokens.completion'),"
               " json_extract(data, '$.meta.cost_unpriced_calls'), json_extract(data, '$.meta.cost_is_estimate')"
               " FROM journal WHERE run_id = ? AND kind = 'activity'"
               " AND json_extract(data, '$.meta.copied_from') IS NULL"
               " AND (json_extract(data, '$.meta.cost') IS NOT NULL"
               " OR json_extract(data, '$.meta.cost_unpriced_calls') IS NOT NULL)")
        with self._lock:
            db = self._db()
            rows = [(*row, 1) for row in db.execute(sql, (run_id,)).fetchall()]
            frames = dict(db.execute("SELECT prefix, path FROM frames WHERE run_id = ?", (run_id,)).fetchall())
            for request, spent in extra:
                caller = db.execute(
                    "SELECT journal.key, json_extract(journal.data, '$.state') FROM journal,"
                    " json_each(journal.data, '$.meta.request_ids') AS ids WHERE journal.run_id = ?"
                    " AND journal.kind = 'activity' AND ids.value = ? LIMIT 1", (run_id, request)).fetchone() \
                    if request else None
                rows.append((caller[0] if caller else "", caller[1] if caller else None, spent["cost"],
                             spent["tokens"]["prompt"], spent["tokens"]["completion"],
                             spent.get("cost_unpriced_calls"), spent.get("cost_is_estimate"), spent["activities"]))
        totals: dict[str, dict[str, Any]] = {}
        for key, state, cost, prompt, completion, unpriced, estimated, count in rows:
            key = _REWOUND_KEY.sub("", key, count=1)  # a row a resume set aside costs where it ran (runner.REWOUND)
            prefix = key[:key.rfind("/m/") + 3] if "/m/" in key else ""  # the frame the activity ran in
            where = frames.get(prefix) or ""
            parts = [part for part in f"{where}/{state or '?'}".split("/") if part]
            for depth in range(1, len(parts) + 1):  # the state and every state above it
                entry = totals.setdefault("/".join(parts[:depth]), {"cost": 0.0, "prompt": 0, "completion": 0,
                                                                   "activities": 0, "unpriced": 0,
                                                                   "estimated": False})
                entry["cost"] += cost or 0.0
                entry["prompt"] += int(prompt or 0)
                entry["completion"] += int(completion or 0)
                entry["activities"] += count
                entry["unpriced"] += int(unpriced or 0)
                entry["estimated"] = entry["estimated"] or bool(estimated)
        return [{"path": path, "cost": round(e["cost"], 6), "activities": e["activities"],
                 "tokens": {"prompt": e["prompt"], "completion": e["completion"]},
                 **({"cost_is_estimate": True} if e["estimated"] else {}),
                 **({"cost_unpriced_calls": e["unpriced"]} if e["unpriced"] else {})}
                for path, e in sorted(totals.items())]
