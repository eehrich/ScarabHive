"""Message Debugger Plugin - SQLite database storage.

Persistent storage for LLM conversation turns and raw API request/response logs.
Uses SQLite with WAL mode for concurrent read/write access.
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import sqlite3
import threading
import time
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


class MessageDebuggerDB:
    """SQLite-backed storage for message debugger data.

    Tables:
    - turns: Agent-level message snapshots (pre_llm / post_llm)
    - llm_requests: LLM-client-level raw API request/response logs

    Each row carries the user whose call it was (``user_id``; NULL: nobody's --
    a call no run named a user for, and every row from before the column).
    Every read takes ``owner`` as a required keyword: a user's name, or
    EVERYONE. There is no default, so a read that forgot whose rows it wants
    fails instead of answering with everybody's.
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
        self._retention_batch = 500  # rows stripped/deleted per pass

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
                timeout=10.0
            )
            self._local.conn.row_factory = sqlite3.Row
            if self._wal_mode:
                self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn
    
    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
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
                messages_json TEXT,
                llm_response_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                user_id TEXT
            );
            
            CREATE TABLE IF NOT EXISTS llm_requests (
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
                payload_json TEXT,
                response_json TEXT,
                error TEXT,
                duration_ms REAL,
                usage_json TEXT,
                finish_reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                served_by TEXT,
                user_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent_name);
            CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
            CREATE INDEX IF NOT EXISTS idx_turns_request_id ON turns(request_id);
            CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp_ms);
            CREATE INDEX IF NOT EXISTS idx_turns_type ON turns(snapshot_type);
            
            CREATE INDEX IF NOT EXISTS idx_llm_requests_agent ON llm_requests(agent_name);
            CREATE INDEX IF NOT EXISTS idx_llm_requests_session ON llm_requests(session_id);
            CREATE INDEX IF NOT EXISTS idx_llm_requests_timestamp ON llm_requests(timestamp_ms);
            CREATE INDEX IF NOT EXISTS idx_llm_requests_direction ON llm_requests(direction);
            CREATE INDEX IF NOT EXISTS idx_llm_requests_provider ON llm_requests(provider);
            
            -- Covering indexes for stats queries (avoid full table scans)
            CREATE INDEX IF NOT EXISTS idx_llm_requests_direction_duration
                ON llm_requests(direction, duration_ms);
            CREATE INDEX IF NOT EXISTS idx_llm_requests_error
                ON llm_requests(error) WHERE error IS NOT NULL AND error != '';

            -- Partial index over rows that still carry a payload, so retention
            -- can find the OLDEST un-stripped row in O(log n) instead of scanning
            -- past every already-stripped old row (stripped rows drop out of this
            -- index automatically when their BLOBs are NULLed).
            CREATE INDEX IF NOT EXISTS idx_llm_requests_stripable
                ON llm_requests(timestamp_ms)
                WHERE payload_json IS NOT NULL OR response_json IS NOT NULL;

            -- request_id index — drives writer-costs queries that walk the
            -- request tree of a root_request_id via equality + prefix LIKE
            -- (`request_id LIKE 'root_%'`). Without this index a single
            -- writer-costs call full-scans every 'response' row in the DB,
            -- which on a multi-GB store takes minutes per story.
            CREATE INDEX IF NOT EXISTS idx_llm_requests_request_id
                ON llm_requests(request_id);
        """)
        # Databases from before served_by existed. ADD COLUMN only touches the
        # schema, not the rows: instant even on a multi-GB file.
        self._add_column(conn, "llm_requests", "served_by")
        # A user's reads go through this index only (_where), and it holds every
        # column they filter and count by: user_id and error sit behind the
        # payloads, and reading either from a row walks them all -- the
        # statistics a panel asks for every few seconds included. Rows from
        # before the column do not hold it, so building the index reads no
        # payload: once, some seconds on a multi-GB file.
        covering = {
            "turns": "user_id, timestamp_ms, agent_name, session_id, request_id, snapshot_type",
            "llm_requests": "user_id, timestamp_ms, agent_name, session_id, request_id, direction, provider, error",
        }
        for table, columns in covering.items():
            self._add_column(conn, table, "user_id")
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user ON {table}({columns}) "
                             "WHERE user_id IS NOT NULL")
            except sqlite3.OperationalError as e:  # another process builds it right now
                logger.warning("message_debugger: index on %s.user_id not built (%s) -- the next start builds it",
                               table, e)
        conn.commit()
    
    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str) -> None:
        """ADD COLUMN on a file from before it (only the schema changes: instant on a multi-GB file).
        Another process starting at the same moment may add it between the look and the ALTER --
        its "duplicate column" is no failure, and must not cost this process its debugger."""
        if column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
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
                messages_json, llm_response_json, user_id)
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
                json.dumps(messages, default=str) if messages else None,
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
                payload_json, response_json, error, duration_ms,
                usage_json, finish_reason, served_by, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                json.dumps(payload, default=str) if payload else None,
                json.dumps(response_data, default=str) if response_data else None,
                error,
                duration_ms,
                json.dumps(usage, default=str) if usage else None,
                finish_reason,
                served_by,
                user_id,
            )
        )
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
        """Get a specific LLM request by ID; None when it is not ``owner``'s either."""
        return self._get_row("llm_requests", req_id, owner)

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
            db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
            stats["db_size_mb"] = round(db_size_bytes / (1024 * 1024), 1)
        return stats
    
    # ---- Maintenance ----
    
    def clear_all(self) -> Dict[str, int]:
        """Clear all data. Returns counts of deleted rows."""
        conn = self._get_conn()
        turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        requests = conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0]
        conn.execute("DELETE FROM turns")
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

    def enforce_retention(self, force: bool = False, budget: float = None) -> Dict[str, int]:
        """Keep the DB within its size cap WITHOUT discarding cost history.

        The payload/response BLOBs are ~all the bytes; the cost columns
        (provider/model/usage/request_id/timestamps) are tiny. So when over the
        HIGH watermark we first STRIP the oldest llm_request payloads (NULL the
        BLOBs, keep the cost row) and drop the oldest turns (debug snapshots, not
        cost-relevant). Only as a last resort — when every old payload is already
        stripped and turns are gone but we are still over budget — do we delete
        the oldest cost rows.

        Time-budgeted: a single call does at most ~1s of work so it never delays
        a capture write past the 5s hook timeout. Hysteresis: once armed (over
        HIGH) it keeps pruning across calls until LOW, then rests. No VACUUM —
        freed pages are reused, the file plateaus.
        """
        keys = {"stripped": 0, "turns_deleted": 0, "requests_deleted": 0}
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
        stripped = turns_deleted = requests_deleted = 0

        while used > target and time.monotonic() < deadline:
            # 1) Strip the oldest payloads — frees the bulk of the bytes while
            #    KEEPING the cost row (usage/model/provider/request_id/...).
            n = conn.execute(
                "UPDATE llm_requests SET payload_json = NULL, response_json = NULL "
                "WHERE id IN (SELECT id FROM llm_requests "
                "             WHERE payload_json IS NOT NULL OR response_json IS NOT NULL "
                "             ORDER BY timestamp_ms ASC LIMIT ?)",
                (BATCH,),
            ).rowcount
            # 2) Drop the oldest turns (agent message snapshots; not used for costing).
            n2 = conn.execute(
                "DELETE FROM turns WHERE id IN "
                "(SELECT id FROM turns ORDER BY timestamp_ms ASC LIMIT ?)",
                (BATCH,),
            ).rowcount
            progress = n + n2
            if progress == 0:
                # Nothing cheap left (all old payloads stripped, no turns): the
                # DB is cost-only rows. Last resort — delete oldest cost rows.
                n3 = conn.execute(
                    "DELETE FROM llm_requests WHERE id IN "
                    "(SELECT id FROM llm_requests ORDER BY timestamp_ms ASC LIMIT ?)",
                    (BATCH,),
                ).rowcount
                requests_deleted += n3
                if n3 == 0:
                    break  # truly empty
            stripped += n
            turns_deleted += n2
            conn.commit()
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

        if stripped or turns_deleted or requests_deleted:
            logger.info(
                "message_debugger retention: stripped %d payloads, dropped %d turns, "
                "deleted %d old cost rows (used now %.0f MB, cap %.0f MB)",
                stripped, turns_deleted, requests_deleted,
                used / 1024**2, self._high_bytes / 1024**2,
            )
        return {"stripped": stripped, "turns_deleted": turns_deleted, "requests_deleted": requests_deleted}

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
        for key in ('messages_json', 'llm_response_json', 'payload_json', 'response_json', 'usage_json'):
            if key in d and d[key]:
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep as string
        return d
