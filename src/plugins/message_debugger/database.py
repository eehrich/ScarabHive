"""Message Debugger Plugin - SQLite database storage.

Persistent storage for LLM conversation turns and raw API request/response logs.
Uses SQLite with WAL mode for concurrent read/write access.
"""
from __future__ import annotations

import atexit
import concurrent.futures
import json
import logging
import queue
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class _Everyone:
    """Whose rows an admin reads: all of them, the ones nobody owns too."""

    def __repr__(self) -> str:
        return "EVERYONE"


#: The owner a read passes to get every row. Any other read names the one user
#: whose rows it gets; a read that names nobody is refused (see _owner_clause).
EVERYONE = _Everyone()


@dataclass(frozen=True)
class Account:
    """A signed-in user's own rows: those under their name captured since their
    account was made. Names come free again when an account is deleted, and a
    new account under the name must not read what the old one left."""

    user_id: str
    since_ms: float


BUSY_TIMEOUT_S = 10
#: How long a process starting with a file another one is moving over (_migrate) waits for it.
MIGRATION_WAIT_S = 900

#: The cost row of an LLM call: everything but its payload and response (llm_payloads).
LLM_REQUEST_COLUMN_NAMES = (
    "id", "timestamp_ms", "direction", "agent_name", "request_id", "session_id", "provider", "model", "url",
    "is_streaming", "error", "duration_ms", "usage_json", "finish_reason", "created_at", "served_by", "user_id",
)
_LLM_REQUEST_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms REAL NOT NULL,
    direction TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    url TEXT DEFAULT '',
    is_streaming INTEGER DEFAULT 0,
    error TEXT,
    duration_ms REAL,
    usage_json TEXT,
    finish_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    served_by TEXT,
    user_id TEXT
"""
# A user's reads go through these indexes only (_where), and each holds every column they filter and count by:
# reading user_id or error from a row of a large table would walk it -- the statistics a panel asks for every few
# seconds included. Rows from before the column do not hold it, so building the index is quick, once.
TURNS_USER_INDEX = ("CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user_id, timestamp_ms, agent_name, "
                    "session_id, request_id, snapshot_type) WHERE user_id IS NOT NULL")
LLM_REQUEST_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_agent ON llm_requests(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_session ON llm_requests(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_timestamp ON llm_requests(timestamp_ms)",
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_direction ON llm_requests(direction)",
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_provider ON llm_requests(provider)",
    # covering indexes for the statistics (no full table scans)
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_direction_duration ON llm_requests(direction, duration_ms)",
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_error ON llm_requests(error) WHERE error IS NOT NULL AND error != ''",
    # writer-costs walks the request tree of a root_request_id by equality and prefix; without it, minutes per story
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_request_id ON llm_requests(request_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_requests_user ON llm_requests(user_id, timestamp_ms, agent_name, "
    "session_id, request_id, direction, provider, error) WHERE user_id IS NOT NULL",
)


def _pack(value: Any) -> Optional[bytes]:
    """The large JSON of a row -- a payload, a response, a turn's messages -- as stored: zlib-compressed UTF-8, a
    third of the size (measured on the server's captures: 3.0-3.3x)."""
    return zlib.compress(json.dumps(value, default=str).encode("utf-8")) if value else None


def stored_text(value: Any) -> Optional[str]:
    """The JSON text of a stored payload, response or turn's messages: rows written since the compression are
    zlib BLOBs, rows from before plain text."""
    return zlib.decompress(value).decode("utf-8") if isinstance(value, bytes) else value


def register_reader_functions(conn: sqlite3.Connection) -> None:
    """For a reader of the file outside this plugin: ``debugger_text(<column>)`` is the text of a compressed column
    -- ``llm_payloads.payload_z``/``response_z`` (id = llm_requests.id), ``turns.messages_z`` -- to match with LIKE
    or load as JSON. A LIKE on the column itself finds nothing (it is zlib)::

        SELECT r.request_id, debugger_text(p.payload_z) FROM llm_requests r JOIN llm_payloads p ON p.id = r.id
    """
    conn.create_function("debugger_text", 1, stored_text, deterministic=True)


class MessageDebuggerDB:
    """SQLite-backed storage for message debugger data.

    Tables:
    - turns: Agent-level message snapshots (pre_llm / post_llm)
    - llm_requests: LLM-client-level raw API request/response logs, the cost
      row of each call -- kept when its payload goes
    - llm_payloads: a request's payload and response (id = llm_requests.id),
      zlib-compressed (stored_text reads them)

    Each row carries the user whose call it was (``user_id``; NULL: nobody's --
    a call no run named a user for, and every row from before the column).
    Every read takes ``owner`` as a required keyword: EVERYONE, a name (every
    row under it: an admin asking for one user), or an Account -- what a
    signed-in user reads with, their rows since their account was made. There
    is no default, so a read that forgot whose rows it wants fails instead of
    answering with everybody's.
    """
    
    def __init__(self, db_path: str | Path, wal_mode: bool = True, max_size_mb: float = 5120,
                 queue_max: int = 2000):
        """Initialize database.

        Args:
            db_path: Path to SQLite database file
            wal_mode: Enable WAL mode for concurrent access (default True)
            max_size_mb: Hard cap on the debugger DB's real data size. When the
                used data exceeds ~90% of this, the oldest turns/requests are
                pruned down to ~75% (hysteresis: rarely, in one batch). 0 = off.
            queue_max: Bound on the async write queue (see the async write
                pipeline below). 0 = unbounded.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._wal_mode = wal_mode
        self._local = threading.local()

        # Size-bounded retention (hysteresis). We measure REAL data size
        # ((page_count - freelist_count) * page_size), which drops on DELETE even
        # without VACUUM; the file plateaus at its high-water mark and reuses the
        # freed pages, so it stops growing. High/low watermarks avoid per-write
        # thrashing — pruning runs only after ~15% of the budget has accumulated.
        self._max_size_bytes = int(float(max_size_mb) * 1024 * 1024) if max_size_mb and max_size_mb > 0 else 0
        self._high_bytes = int(self._max_size_bytes * 0.90)
        self._low_bytes = int(self._max_size_bytes * 0.75)
        self._retention_check_interval = 200  # measure size only every N writes
        self._writes_since_check = 0
        self._retention_lock = threading.Lock()
        # Hysteresis state: once we cross HIGH we keep pruning (in small,
        # time-budgeted slices) until LOW, then rest. A single retention call is
        # bounded to ~1s so it never delays a capture write past the 5s hook
        # timeout (the old "prune all the way to LOW in one write" tripped it).
        self._pruning = False
        self._retention_budget_s = 1.0
        self._retention_batch = 500  # rows deleted per pass

        # ---- Async write pipeline -------------------------------------------
        # Capture must NEVER block the agent. A write is heavy: json.dumps of the
        # whole message list + commit + retention on a multi-GB WAL DB, which can
        # stall for MINUTES on a checkpoint. The hooks offloaded that to a worker
        # thread but still *awaited* it — the event loop stayed free, yet the
        # agent's own coroutine blocked on the await (observed: a single turn
        # snapshot wedged a whole pipeline run for ~20 min against a 4.7 GB DB).
        # So every write now goes through a bounded queue drained by ONE
        # dedicated background thread: the agent only does an O(1) put_nowait and
        # moves on; a slow DB just makes the queue lag, never the pipeline.
        # History is preserved — nothing is deleted here. Only on pathological
        # overflow (writer wedged for a very long time) the newest snapshot is
        # dropped, loudly, to protect memory instead of blocking or OOMing.
        self._write_q: "queue.Queue[Optional[Callable[[], Any]]]" = queue.Queue(
            maxsize=int(queue_max) if queue_max and queue_max > 0 else 0
        )
        self._dropped = 0
        self._closed = False

        # Initialize schema (on this thread's connection) before the writer runs.
        self._init_schema()
        if self._needs_migration:
            self._write_q.put_nowait(self._migrate)  # the writer's first job, ahead of every capture

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="msgdbg-writer", daemon=True
        )
        self._writer_thread.start()
        atexit.register(self.close)
        logger.info(f"MessageDebuggerDB initialized: {self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=BUSY_TIMEOUT_S
            )
            self._local.conn.row_factory = sqlite3.Row
            if self._wal_mode:
                self._local.conn.execute("PRAGMA journal_mode=WAL")
                # A WAL a checkpoint could not catch up with kept the size it grew to (2.1 GB on the server); this
                # cuts it back once a checkpoint gets through.
                self._local.conn.execute("PRAGMA journal_size_limit = 67108864")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn
    
    def _init_schema(self) -> None:
        """Create tables if they don't exist. A file from before llm_payloads is moved over by _migrate, on the
        writer: it takes a minute or two on a multi-GB file, and the plugin's start must not wait for it.

        Waits for the write lock as long as a migration may take: another process starting with the same file
        may be moving it over right now, and a start that gave up after the usual timeout would cost this
        process its debugger."""
        conn = self._get_conn()
        conn.execute(f"PRAGMA busy_timeout = {MIGRATION_WAIT_S * 1000}")
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS llm_requests ({_LLM_REQUEST_COLUMNS});
            -- A request's payload and response, apart from its cost row: retention deletes them whole, and the
            -- pages they held go free. NULLed in place in llm_requests, as they were, each row kept its page --
            -- the file held 1.3 GB of empty page space no retention could reach, and every pruned row added to it.
            -- "_z": zlib (stored_text, register_reader_functions)
            CREATE TABLE IF NOT EXISTS llm_payloads (
                id INTEGER PRIMARY KEY,  -- llm_requests.id
                payload_z BLOB,
                response_z BLOB
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms REAL NOT NULL,
                snapshot_type TEXT NOT NULL,
                agent_name TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                step INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                context_window INTEGER,
                messages_z BLOB,
                llm_response_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                user_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent_name);
            CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
            CREATE INDEX IF NOT EXISTS idx_turns_request_id ON turns(request_id);
            CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp_ms);
            CREATE INDEX IF NOT EXISTS idx_turns_type ON turns(snapshot_type);
        """)
        # Databases from before served_by existed. ADD COLUMN only touches the
        # schema, not the rows: instant even on a multi-GB file.
        self._add_column(conn, "llm_requests", "served_by")
        self._add_column(conn, "turns", "user_id")
        self._add_column(conn, "llm_requests", "user_id")
        # compressed now: a new name, so that a query of the old one fails instead of finding only the old rows
        # (a LIKE over zlib finds nothing). Only the schema changes: instant.
        self._rename_column(conn, "turns", "messages_json", "messages_z")
        self._needs_migration = "payload_json" in self._columns(conn, "llm_requests")
        for statement in (TURNS_USER_INDEX, *LLM_REQUEST_INDEXES):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as e:  # another process builds it right now
                logger.warning("message_debugger: index not built (%s) -- the next start builds it: %s",
                               e, statement.split(" ON ")[0])
        conn.commit()
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")

    def _migrate(self) -> None:
        """Move a file from before llm_payloads over, once: its payloads into llm_payloads, llm_requests rebuilt
        without them (the empty space the old rows held in their pages goes with the old table).

        On the writer, before any capture: the captures meanwhile wait in the queue. Another process starting
        with the same file waits for the write lock, then finds it done. The moved payloads stay uncompressed
        (stored_text reads both); the pages the old table held go on the freelist, where new rows reuse them --
        the prune in the panel gives them back to the disk.

        One-way: code from before cannot write to the file afterwards, nor start on it. All processes on the
        file restart together. A move that fails (a full disk) changes nothing; until a start succeeds, the
        retention strips the payloads still in llm_requests in place, as it did -- it must not take their cost
        rows for them.
        """
        conn = self._get_conn()
        conn.execute(f"PRAGMA busy_timeout = {MIGRATION_WAIT_S * 1000}")
        started = time.monotonic()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if "payload_json" not in self._columns(conn, "llm_requests"):
                conn.rollback()  # another process was first
                self._needs_migration = False
                return
            moved = conn.execute(
                "INSERT INTO llm_payloads (id, payload_z, response_z) SELECT id, payload_json, response_json "
                "FROM llm_requests WHERE payload_json IS NOT NULL OR response_json IS NOT NULL").rowcount
            sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'llm_requests'").fetchone()
            columns = ", ".join(LLM_REQUEST_COLUMN_NAMES)
            conn.execute(f"CREATE TABLE llm_requests_new ({_LLM_REQUEST_COLUMNS})")
            kept = conn.execute(f"INSERT INTO llm_requests_new ({columns}) SELECT {columns} FROM llm_requests").rowcount
            conn.execute("DROP TABLE llm_requests")
            conn.execute("ALTER TABLE llm_requests_new RENAME TO llm_requests")
            if sequence:  # ids never come back: one a cost row had before it was deleted is no newer row's
                conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'llm_requests'", sequence)
            for statement in LLM_REQUEST_INDEXES:
                conn.execute(statement)
            conn.commit()
        except Exception as e:  # the writer goes on; the next start tries again
            conn.rollback()
            logger.error("message_debugger: moving the file over to llm_payloads failed (%s) -- until a start "
                         "succeeds, the retention strips the old payloads in place", e)
            return
        finally:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
        self._needs_migration = False
        logger.warning(
            "message_debugger: moved %d payloads out of llm_requests (%d cost rows kept) in %.0f s; "
            "%.0f MB of the file are free now -- the panel's prune gives them back to the disk",
            moved, kept, time.monotonic() - started, self.free_pages() * self._page_size(conn) / 1024**2)

    def _still_old(self, conn: sqlite3.Connection) -> bool:
        """Whether the file is still not moved over. Another process may have done it since this one's own
        move failed or gave up waiting: its old payloads are gone from llm_requests then, and asking for them
        would fail every retention of this process until it restarts."""
        if self._needs_migration and "payload_json" not in self._columns(conn, "llm_requests"):
            self._needs_migration = False
        return self._needs_migration

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _page_size(conn: sqlite3.Connection) -> int:
        return conn.execute("PRAGMA page_size").fetchone()[0]

    @staticmethod
    def _rename_column(conn: sqlite3.Connection, table: str, old: str, new: str) -> None:
        """RENAME COLUMN on a file from before; another process may do it in between, as with _add_column."""
        if old not in MessageDebuggerDB._columns(conn, table):
            return
        try:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        except sqlite3.OperationalError:
            if new not in MessageDebuggerDB._columns(conn, table):
                raise

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str) -> None:
        """ADD COLUMN on a file from before it (only the schema changes: instant on a multi-GB file).
        Another process starting at the same moment may add it between the look and the ALTER --
        its "duplicate column" is no failure, and must not cost this process its debugger."""
        if column in MessageDebuggerDB._columns(conn, table):
            return
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise

    # ---- Turn operations ----
    
    def insert_turn(
        self,
        timestamp_ms: float,
        snapshot_type: str,
        agent_name: str = '',
        request_id: str = '',
        session_id: str = '',
        step: int = 0,
        message_count: int = 0,
        total_tokens: int = 0,
        context_window: Optional[int] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        llm_response: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Insert a conversation turn snapshot.
        
        Returns:
            Row ID of inserted turn
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO turns 
               (timestamp_ms, snapshot_type, agent_name, request_id, session_id,
                step, message_count, total_tokens, context_window,
                messages_z, llm_response_json, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp_ms,
                snapshot_type,
                agent_name,
                request_id,
                session_id,
                step,
                message_count,
                total_tokens,
                context_window,
                _pack(messages),
                # kept as text: the list reads its usage with json_extract
                json.dumps(llm_response, default=str) if llm_response else None,
                user_id,
            )
        )
        conn.commit()
        self.maybe_enforce_retention()
        return cursor.lastrowid  # type: ignore[return-value]
    
    # Columns to select in list queries (excludes large JSON blobs; of the LLM response only its usage)
    _TURNS_LIST_COLS = (
        "id, timestamp_ms, snapshot_type, agent_name, request_id, "
        "session_id, step, message_count, total_tokens, context_window, "
        "json_extract(llm_response_json, '$.usage') AS usage_json, created_at, user_id"
    )
    _LLM_REQUESTS_LIST_COLS = (
        "id, timestamp_ms, direction, agent_name, request_id, "
        "session_id, provider, model, url, is_streaming, "
        "error, duration_ms, usage_json, finish_reason, created_at, served_by, user_id"
    )

    @staticmethod
    def _owner_clause(owner: Any) -> tuple[list[str], list]:
        """The rows ``owner`` may read: EVERYONE all of them, a name every row under it (an admin
        asking for one user), an Account its rows since it was made.

        Anything else is refused, loudly: a read that did not say whose rows it
        wants would otherwise get everybody's, and nothing in the answer shows it.
        """
        if owner is EVERYONE:
            return [], []
        if isinstance(owner, Account) and owner.user_id:
            return ["user_id = ?", "timestamp_ms >= ?"], [owner.user_id, owner.since_ms]
        if isinstance(owner, str) and owner:
            return ["user_id = ?"], [owner]
        raise ValueError(f"not an owner: {owner!r} -- a user id, or EVERYONE")

    @classmethod
    def _where(cls, owner: Any, request_id: Optional[str] = None, max_id: Optional[int] = None,
               **filters: Optional[str]) -> tuple[str, list]:
        """The WHERE clause of a list or count query: ``owner``'s rows, each filter given an equality on its column.

        A request takes the calls under it along: tool calls and sub-agents run under ``<request_id>_...`` ids.
        ``max_id`` keeps to the rows that existed when it was the newest id (ids only ever grow).
        For one user's rows the other columns are written ``+column``: no index but the user's may serve
        them, since any other one would read user_id from each row it finds (see _init_schema).
        """
        clauses, params = cls._owner_clause(owner)
        mark = "+" if clauses else ""
        given = {column: value for column, value in filters.items() if value}
        clauses += [f"{mark}{column} = ?" for column in given]
        params += list(given.values())
        if max_id is not None:
            clauses.append("+id <= ?")  # "+": not a rowid range -- a count would read the table instead of an index
            params.append(max_id)
        if request_id:
            # the ids that start with "<request_id>_" as a range the index serves: '`' is the character after '_'
            clauses.append(f"({mark}request_id = ? OR ({mark}request_id >= ? AND {mark}request_id < ?))")
            params += [request_id, f"{request_id}_", f"{request_id}`"]
        return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def get_turns(
        self,
        *,
        owner: Any,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        snapshot_type: Optional[str] = None,
        max_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query turns with optional filters.

        Returns lightweight rows (no messages_json) for list views.
        Use get_turn(id) to fetch full details including messages.
        """
        where, params = self._where(owner, agent_name=agent_name, session_id=session_id, request_id=request_id,
                                    snapshot_type=snapshot_type, max_id=max_id)
        rows = self._get_conn().execute(
            f"SELECT {self._TURNS_LIST_COLS} FROM turns{where} ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_turn(self, turn_id: int, *, owner: Any) -> Optional[Dict[str, Any]]:
        """Get a specific turn by ID; None when it is not ``owner``'s either."""
        return self._get_row("turns", turn_id, owner)

    def _get_row(self, table: str, row_id: int, owner: Any) -> Optional[Dict[str, Any]]:
        clauses, params = self._owner_clause(owner)
        mine = "".join(f" AND {clause}" for clause in clauses)
        row = self._get_conn().execute(f"SELECT * FROM {table} WHERE id = ?{mine}", (row_id, *params)).fetchone()
        return self._row_to_dict(row) if row else None

    def count_turns(
        self,
        *,
        owner: Any,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        snapshot_type: Optional[str] = None,
        max_id: Optional[int] = None,
    ) -> int:
        """Count the turns get_turns finds with the same filters."""
        where, params = self._where(owner, agent_name=agent_name, session_id=session_id, request_id=request_id,
                                    snapshot_type=snapshot_type, max_id=max_id)
        return self._get_conn().execute(f"SELECT COUNT(*) FROM turns{where}", params).fetchone()[0]
    
    # ---- LLM Request operations ----
    
    def insert_llm_request(
        self,
        timestamp_ms: float,
        direction: str,  # 'request' or 'response'
        agent_name: str = '',
        request_id: str = '',
        session_id: str = '',
        provider: str = '',
        model: str = '',
        url: str = '',
        is_streaming: bool = False,
        payload: Optional[Dict[str, Any]] = None,
        response_data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
        usage: Optional[Dict[str, Any]] = None,
        finish_reason: Optional[str] = None,
        served_by: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Insert an LLM API request or response log entry.

        ``served_by``: the backend a gateway routed the call to (OpenRouter: "Google AI Studio", "Google" for
        Vertex), as the LLM client read it from the response.

        Returns:
            Row ID of inserted entry
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO llm_requests
               (timestamp_ms, direction, agent_name, request_id, session_id,
                provider, model, url, is_streaming,
                error, duration_ms,
                usage_json, finish_reason, served_by, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp_ms,
                direction,
                agent_name,
                request_id,
                session_id,
                provider,
                model,
                url,
                1 if is_streaming else 0,
                error,
                duration_ms,
                json.dumps(usage, default=str) if usage else None,
                finish_reason,
                served_by,
                user_id,
            )
        )
        if payload or response_data:
            conn.execute("INSERT INTO llm_payloads (id, payload_z, response_z) VALUES (?, ?, ?)",
                         (cursor.lastrowid, _pack(payload), _pack(response_data)))
        conn.commit()
        self.maybe_enforce_retention()
        return cursor.lastrowid  # type: ignore[return-value]

    # ---- Async write pipeline ------------------------------------------------

    def submit(self, fn: "Callable[[], Any]") -> None:
        """Enqueue an arbitrary write callable (build + insert) for the background
        writer. Never blocks the caller; used by the capture hooks so the agent
        loop is never delayed by a slow DB."""
        self._submit(fn)

    def submit_turn(self, **kwargs: Any) -> None:
        """Enqueue a turn insert for the background writer (never blocks)."""
        self._submit(lambda: self.insert_turn(**kwargs))

    def submit_llm_request(self, **kwargs: Any) -> None:
        """Enqueue an llm_request insert for the background writer (never blocks)."""
        self._submit(lambda: self.insert_llm_request(**kwargs))

    def _submit(self, fn: "Callable[[], Any]") -> None:
        if self._closed:
            return
        try:
            self._write_q.put_nowait(fn)
        except queue.Full:
            # Writer wedged for a long time. Drop the newest capture (loudly)
            # rather than block the agent or grow memory without bound.
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 200 == 0:
                logger.warning(
                    "message_debugger: write queue full — DB writes lagging, "
                    "dropped %d capture(s) so far", self._dropped,
                )

    def call(self, fn: "Callable[[], Any]") -> Any:
        """Run ``fn`` on the background writer and wait for its result (blocks the caller).

        For the maintenance that holds the write lock long -- a clear or a VACUUM
        of a multi-GB file takes minutes: run on another connection, every capture
        written meanwhile waited out the 10 s busy timeout and was lost. Behind
        the writer, this process's captures wait in the queue instead (up to its
        limit); another process writing the same file still meets the lock.
        """
        if self._closed:
            raise RuntimeError("the message debugger database is closed")
        done: "concurrent.futures.Future[Any]" = concurrent.futures.Future()

        def job() -> None:
            try:
                done.set_result(fn())
            except BaseException as e:  # noqa: BLE001 -- handed to the caller
                done.set_exception(e)

        self._write_q.put(job)  # waits for a slot: maintenance is not dropped like a capture
        while True:
            try:
                return done.result(timeout=1.0)
            except concurrent.futures.TimeoutError:
                if not self._writer_thread.is_alive():  # closed meanwhile: the job sits behind the sentinel
                    raise RuntimeError("the message debugger database was closed") from None

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the write queue is drained, or ``timeout`` elapses.

        Not needed in production (writes are fire-and-forget); useful for tests
        and for a graceful drain before shutdown. Returns True if fully drained.
        """
        deadline = time.monotonic() + timeout
        while self._write_q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._write_q.unfinished_tasks == 0

    def _writer_loop(self) -> None:
        """Single background thread: drains the write queue, one job at a time.

        Owns its own thread-local connection (created lazily in _get_conn on this
        thread). A slow insert/commit/retention here only delays the queue, never
        an agent request. Exceptions never propagate — a debug write must not kill
        the writer thread.
        """
        while True:
            fn = self._write_q.get()
            try:
                if fn is None:  # shutdown sentinel
                    return
                fn()
            except Exception as e:  # noqa: BLE001 — capture must never crash
                logger.warning("message_debugger write failed: %s", e)
            finally:
                self._write_q.task_done()

    def close(self, timeout: float = 5.0) -> None:
        """Flush pending writes, stop the background writer, close the connection.

        Enqueues a sentinel behind the pending jobs, so the writer drains the
        backlog (up to ``timeout``) before exiting — history captured just before
        shutdown is not lost. The flush runs once (idempotent); closing this
        thread's connection is safe to repeat.
        """
        atexit.unregister(self.close)  # explicit close: don't also fire at exit
        if not self._closed:
            self._closed = True
            # Block up to `timeout` for a slot rather than discarding a pending
            # job — on shutdown we want the backlog flushed, not dropped. The
            # sentinel sits behind the pending jobs (FIFO), so the writer drains
            # them first, then exits.
            try:
                self._write_q.put(None, timeout=timeout)
            except queue.Full:
                pass  # writer is a daemon; it exits with the process regardless
            if self._writer_thread.is_alive():
                self._writer_thread.join(timeout=timeout)
        # Close this thread's connection (thread-local; the writer thread's own
        # connection is released when that daemon thread exits).
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def get_llm_requests(
        self,
        *,
        owner: Any,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        direction: Optional[str] = None,
        provider: Optional[str] = None,
        max_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query LLM request logs with optional filters.

        Returns lightweight rows (no payload_json/response_json) for list views.
        Use get_llm_request(id) to fetch full details.
        """
        where, params = self._where(owner, agent_name=agent_name, session_id=session_id, request_id=request_id,
                                    direction=direction, provider=provider, max_id=max_id)
        rows = self._get_conn().execute(
            f"SELECT {self._LLM_REQUESTS_LIST_COLS} FROM llm_requests{where} ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_llm_request(self, req_id: int, *, owner: Any) -> Optional[Dict[str, Any]]:
        """Get a specific LLM request by ID, its payload and response with it (None: the retention deleted them,
        or there were none); None when it is not ``owner``'s either."""
        clauses, params = self._owner_clause(owner)
        mine = "".join(f" AND r.{clause}" for clause in clauses)
        columns = ", ".join(f"r.{column}" for column in LLM_REQUEST_COLUMN_NAMES)

        def read(inline: bool):
            payload, response = (("COALESCE(p.payload_z, r.payload_json)", "COALESCE(p.response_z, r.response_json)")
                                 if inline else ("p.payload_z", "p.response_z"))
            return self._get_conn().execute(
                f"SELECT {columns}, {payload} AS payload_json, {response} AS response_json FROM llm_requests r "
                f"LEFT JOIN llm_payloads p ON p.id = r.id WHERE r.id = ?{mine}", (req_id, *params)).fetchone()

        inline = self._needs_migration  # not moved over (yet): the old payloads are in their rows
        try:
            row = read(inline)
        except sqlite3.OperationalError:
            if not inline:
                raise
            row = read(False)  # moved over by another process meanwhile
            self._still_old(self._get_conn())
        return self._row_to_dict(row) if row else None

    def count_llm_requests(
        self,
        *,
        owner: Any,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        direction: Optional[str] = None,
        provider: Optional[str] = None,
        max_id: Optional[int] = None,
    ) -> int:
        """Count the LLM request logs get_llm_requests finds with the same filters."""
        where, params = self._where(owner, agent_name=agent_name, session_id=session_id, request_id=request_id,
                                    direction=direction, provider=provider, max_id=max_id)
        return self._get_conn().execute(f"SELECT COUNT(*) FROM llm_requests{where}", params).fetchone()[0]
    
    def has_session(self, session_id: str) -> bool:
        """Whether anything was captured for ``session_id`` -- a turn or an LLM request, whoever owns it. Two indexed
        lookups (idx_turns_session, idx_llm_requests_session) that stop at the first row."""
        conn = self._get_conn()
        return any(conn.execute(f"SELECT 1 FROM {table} WHERE session_id = ? LIMIT 1", (session_id,)).fetchone()
                   for table in ("llm_requests", "turns"))

    def newest_id(self, table: str, *, owner: Any) -> int:
        """The id the newest of ``owner``'s rows in ``turns`` or ``llm_requests`` has, 0 for none: a list's point
        in time."""
        if table not in ("turns", "llm_requests"):
            raise ValueError(f"not a list table: {table}")
        where, params = self._where(owner)
        # MAX(id) of one user's rows would walk the rowids down from the newest and read
        # user_id from each row it passes; MAX(+id) takes the user's index (see _where).
        mark = "+" if params else ""
        return self._get_conn().execute(f"SELECT COALESCE(MAX({mark}id), 0) FROM {table}{where}",
                                        params).fetchone()[0]

    # ---- Stats ----
    
    def get_stats(self, *, owner: Any) -> Dict[str, Any]:
        """Statistics over ``owner``'s rows.

        Optimised to avoid expensive full-table aggregations on large
        databases.  Uses indexed COUNT queries and limits the session
        list to avoid scanning hundreds of thousands of rows. The size of
        the file is everyone's rows: only EVERYONE's statistics name it.
        """
        conn = self._get_conn()
        where, params = self._where(owner)
        mine = where.replace(" WHERE ", " AND ", 1)
        # One user's rows: the other columns as +column, see _where -- in the
        # select list too: a DISTINCT would otherwise walk the agent or provider
        # index over everyone's rows and read user_id from each.
        mark = "+" if params else ""

        turn_count = conn.execute(f"SELECT COUNT(*) FROM turns{where}", params).fetchone()[0]
        request_count = conn.execute(f"SELECT COUNT(*) FROM llm_requests{where}", params).fetchone()[0]

        # Use separate indexed queries instead of UNION (faster on large DBs)
        agents_set: set[str] = set()
        for row in conn.execute(
            f"SELECT DISTINCT {mark}agent_name FROM turns WHERE {mark}agent_name != ''{mine}", params
        ).fetchall():
            agents_set.add(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT {mark}agent_name FROM llm_requests WHERE {mark}agent_name != ''{mine}", params
        ).fetchall():
            agents_set.add(row[0])

        providers = [r[0] for r in conn.execute(
            f"SELECT DISTINCT {mark}provider FROM llm_requests WHERE {mark}provider != ''{mine}", params
        ).fetchall()]

        # Error count uses partial index (fast); one user's reads the error of each of their rows
        error_count = conn.execute(
            f"SELECT COUNT(*) FROM llm_requests WHERE {mark}error IS NOT NULL AND {mark}error != ''{mine}", params
        ).fetchone()[0]

        # Session count instead of full list (much cheaper)
        session_count_turns = conn.execute(
            f"SELECT COUNT(DISTINCT {mark}session_id) FROM turns WHERE {mark}session_id != ''{mine}", params
        ).fetchone()[0]
        session_count_reqs = conn.execute(
            f"SELECT COUNT(DISTINCT {mark}session_id) FROM llm_requests WHERE {mark}session_id != ''{mine}", params
        ).fetchone()[0]
        session_count = max(session_count_turns, session_count_reqs)

        stats = {
            "total_turns": turn_count,
            "total_llm_requests": request_count,
            "unique_agents": sorted(agents_set),
            "unique_session_count": session_count,
            "unique_providers": sorted(providers),
            "error_count": error_count,
        }
        if owner is EVERYONE:
            stats["db_size_mb"] = self.size_mb()
        return stats

    def size_mb(self) -> float:
        """The file's size in MB: everyone's rows, an admin's to know."""
        return round((self.db_path.stat().st_size if self.db_path.exists() else 0) / (1024 * 1024), 1)
    
    # ---- Maintenance ----
    
    def clear_all(self) -> Dict[str, int]:
        """Clear all data. Returns counts of deleted rows."""
        conn = self._get_conn()
        turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        requests = conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0]
        conn.execute("DELETE FROM turns")
        conn.execute("DELETE FROM llm_payloads")
        conn.execute("DELETE FROM llm_requests")
        conn.commit()
        return {"turns_deleted": turns, "requests_deleted": requests}
    
    def free_pages(self) -> int:
        """Pages on the freelist: the space a VACUUM gives back to the disk."""
        return self._get_conn().execute("PRAGMA freelist_count").fetchone()[0]

    def vacuum(self) -> None:
        """Reclaim disk space after deletions (shrinks the file).

        In WAL mode VACUUM writes the compacted database into the WAL; without a
        checkpoint the main file stays at its old size on disk until the next
        checkpoint/close. Checkpoint(TRUNCATE) here so the file actually shrinks
        immediately.
        """
        conn = self._get_conn()
        conn.execute("VACUUM")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass

    # ---- Size-bounded retention (automatic) ----------------------------------

    def _used_bytes(self, conn: sqlite3.Connection) -> int:
        """Real data size: (allocated pages - free pages) * page size.

        Unlike os.path.getsize this DROPS when rows are deleted (freed pages go
        on the freelist), so it is the right metric to bound without VACUUM.
        """
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return max(0, (page_count - freelist) * page_size)

    #: What retention deletes before any cost row: (result key, the oldest row's time, a batch's delete). A
    #: payload without its cost row counts as the oldest of all.
    _CONTENT = (
        ("payloads_deleted",
         "SELECT COALESCE(r.timestamp_ms, 0) FROM llm_payloads p LEFT JOIN llm_requests r ON r.id = p.id "
         "ORDER BY p.id LIMIT 1",
         "DELETE FROM llm_payloads WHERE id IN (SELECT id FROM llm_payloads ORDER BY id LIMIT ?)"),
        ("turns_deleted",
         "SELECT timestamp_ms FROM turns ORDER BY timestamp_ms LIMIT 1",
         "DELETE FROM turns WHERE id IN (SELECT id FROM turns ORDER BY timestamp_ms LIMIT ?)"),
    )
    #: A file not moved over: its payloads still in their cost rows, stripped there as before (_migrate).
    _INLINE = (
        "payloads_deleted",
        "SELECT timestamp_ms FROM llm_requests WHERE payload_json IS NOT NULL OR response_json IS NOT NULL "
        "ORDER BY timestamp_ms LIMIT 1",
        "UPDATE llm_requests SET payload_json = NULL, response_json = NULL WHERE id IN (SELECT id FROM llm_requests "
        "WHERE payload_json IS NOT NULL OR response_json IS NOT NULL ORDER BY timestamp_ms LIMIT ?)",
    )

    def enforce_retention(self, force: bool = False, budget: float = None) -> Dict[str, int]:
        """Keep the DB within its size cap WITHOUT discarding cost history.

        The payloads, responses and turn snapshots are ~all the bytes; a cost row
        (provider/model/usage/request_id/timestamps) is a few hundred. So over the
        HIGH watermark the oldest content goes, whole rows -- payloads
        (llm_payloads) and turns, whichever reaches further back, so both keep the
        same days -- and the cost rows stay. Only when no content is left and the
        file is still over budget do the oldest cost rows go.

        Time-budgeted: a single call does at most ~1s of work so it never delays
        a capture write past the 5s hook timeout. Hysteresis: once armed (over
        HIGH) it keeps pruning across calls until LOW, then rests. No VACUUM --
        freed pages are reused, the file plateaus.
        """
        keys = {"payloads_deleted": 0, "turns_deleted": 0, "requests_deleted": 0}
        if self._max_size_bytes <= 0:
            return keys
        conn = self._get_conn()
        used = self._used_bytes(conn)
        if not self._pruning:
            # Auto path arms only above HIGH; the manual button passes force=True
            # to prune down to LOW even when between the watermarks.
            if used <= self._high_bytes and not force:
                return keys
            self._pruning = True  # arm: prune down to LOW

        BATCH = self._retention_batch
        target = max(1, int(self._low_bytes * 0.98))
        deadline = time.monotonic() + (budget if budget else self._retention_budget_s)
        done = dict(keys)

        sources = list(self._CONTENT)
        if self._still_old(conn):  # not moved over: its old payloads are content too, and the oldest
            sources.append(self._INLINE)
        while used > target and time.monotonic() < deadline:
            # the source whose oldest row is the oldest: payloads and turns keep the same days
            heads = [(row[0], key, sql) for key, head, sql in sources
                     if (row := conn.execute(head).fetchone()) is not None]
            if heads:
                _, key, sql = min(heads, key=lambda head: head[0])
            else:  # nothing but cost rows left -- the last resort
                key, sql = "requests_deleted", ("DELETE FROM llm_requests WHERE id IN "
                                                "(SELECT id FROM llm_requests ORDER BY timestamp_ms LIMIT ?)")
            deleted = conn.execute(sql, (BATCH,)).rowcount
            conn.commit()
            done[key] += deleted
            if not deleted:
                break  # truly empty
            # Nudge a checkpoint so the freelist (and thus _used_bytes) reflects
            # the freed pages in WAL mode. PASSIVE never blocks on readers; the
            # old TRUNCATE variant waited for every reader (e.g. the web UI) to
            # release and could wedge this thread for minutes. Freed pages land on
            # the freelist on commit regardless, so PASSIVE is sufficient here.
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.OperationalError:
                pass
            used = self._used_bytes(conn)

        if used <= target:
            self._pruning = False  # reached LOW -> rest until HIGH again

        if any(done.values()):
            logger.info(
                "message_debugger retention: deleted %d payloads, %d turns, %d old cost rows "
                "(used now %.0f MB, cap %.0f MB)",
                done["payloads_deleted"], done["turns_deleted"], done["requests_deleted"],
                used / 1024**2, self._high_bytes / 1024**2,
            )
        return done

    def maybe_enforce_retention(self) -> None:
        """Cheap per-write hook: every N writes, size-check and prune if needed.

        Most calls are a no-op (counter only). The size measurement runs every
        _retention_check_interval writes; the actual prune happens far more
        rarely (only above the high watermark). A non-blocking lock guarantees
        at most one prune runs at a time across worker threads.
        """
        if self._max_size_bytes <= 0:
            return
        self._writes_since_check += 1
        if self._writes_since_check < self._retention_check_interval:
            return
        if not self._retention_lock.acquire(blocking=False):
            return  # another thread is already pruning; it covers us
        try:
            self._writes_since_check = 0
            self.enforce_retention()
        except Exception as e:  # retention must never break capture
            logger.warning("message_debugger retention failed: %s", e)
        finally:
            self._retention_lock.release()

    # ---- Helpers ----
    
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a sqlite3.Row to a dict, parsing JSON columns."""
        d = dict(row)
        if 'messages_z' in d:
            d['messages_json'] = d.pop('messages_z')
        for key in ('messages_json', 'llm_response_json', 'payload_json', 'response_json', 'usage_json'):
            if key in d and d[key]:
                try:
                    d[key] = stored_text(d[key])
                except (zlib.error, UnicodeDecodeError) as e:
                    d[key] = f"[unreadable: {e}]"
                    continue
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep as string
        return d
