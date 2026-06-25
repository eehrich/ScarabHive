"""Message Debugger Plugin - SQLite database storage.

Persistent storage for LLM conversation turns and raw API request/response logs.
Uses SQLite with WAL mode for concurrent read/write access.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MessageDebuggerDB:
    """SQLite-backed storage for message debugger data.
    
    Tables:
    - turns: Agent-level message snapshots (pre_llm / post_llm)
    - llm_requests: LLM-client-level raw API request/response logs
    """
    
    def __init__(self, db_path: str | Path, wal_mode: bool = True, max_size_mb: float = 5120):
        """Initialize database.

        Args:
            db_path: Path to SQLite database file
            wal_mode: Enable WAL mode for concurrent access (default True)
            max_size_mb: Hard cap on the debugger DB's real data size. When the
                used data exceeds ~90% of this, the oldest turns/requests are
                pruned down to ~75% (hysteresis: rarely, in one batch). 0 = off.
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

        # Initialize schema
        self._init_schema()
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
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            
            CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent_name);
            CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
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

            -- request_id index — drives writer-costs queries that walk the
            -- request tree of a root_request_id via equality + prefix LIKE
            -- (`request_id LIKE 'root_%'`). Without this index a single
            -- writer-costs call full-scans every 'response' row in the DB,
            -- which on a multi-GB store takes minutes per story.
            CREATE INDEX IF NOT EXISTS idx_llm_requests_request_id
                ON llm_requests(request_id);
        """)
        conn.commit()
    
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
                messages_json, llm_response_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            )
        )
        conn.commit()
        self.maybe_enforce_retention()
        return cursor.lastrowid  # type: ignore[return-value]
    
    # Columns to select in list queries (excludes large JSON blobs)
    _TURNS_LIST_COLS = (
        "id, timestamp_ms, snapshot_type, agent_name, request_id, "
        "session_id, step, message_count, total_tokens, context_window, "
        "llm_response_json, created_at"
    )
    _LLM_REQUESTS_LIST_COLS = (
        "id, timestamp_ms, direction, agent_name, request_id, "
        "session_id, provider, model, url, is_streaming, "
        "error, duration_ms, usage_json, finish_reason, created_at"
    )

    def get_turns(
        self,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        snapshot_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query turns with optional filters.
        
        Returns lightweight rows (no messages_json) for list views.
        Use get_turn(id) to fetch full details including messages.
        """
        conn = self._get_conn()
        query = f"SELECT {self._TURNS_LIST_COLS} FROM turns WHERE 1=1"
        params: list = []
        
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if snapshot_type:
            query += " AND snapshot_type = ?"
            params.append(snapshot_type)
        
        query += " ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]
    
    def get_turn(self, turn_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific turn by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return self._row_to_dict(row) if row else None
    
    def count_turns(
        self,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Count turns with optional filters."""
        conn = self._get_conn()
        query = "SELECT COUNT(*) FROM turns WHERE 1=1"
        params: list = []
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        return conn.execute(query, params).fetchone()[0]
    
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
    ) -> int:
        """Insert an LLM API request or response log entry.
        
        Returns:
            Row ID of inserted entry
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO llm_requests
               (timestamp_ms, direction, agent_name, request_id, session_id,
                provider, model, url, is_streaming,
                payload_json, response_json, error, duration_ms,
                usage_json, finish_reason)
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
                json.dumps(payload, default=str) if payload else None,
                json.dumps(response_data, default=str) if response_data else None,
                error,
                duration_ms,
                json.dumps(usage, default=str) if usage else None,
                finish_reason,
            )
        )
        conn.commit()
        self.maybe_enforce_retention()
        return cursor.lastrowid  # type: ignore[return-value]
    
    def get_llm_requests(
        self,
        agent_name: Optional[str] = None,
        session_id: Optional[str] = None,
        direction: Optional[str] = None,
        provider: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query LLM request logs with optional filters.
        
        Returns lightweight rows (no payload_json/response_json) for list views.
        Use get_llm_request(id) to fetch full details.
        """
        conn = self._get_conn()
        query = f"SELECT {self._LLM_REQUESTS_LIST_COLS} FROM llm_requests WHERE 1=1"
        params: list = []
        
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if direction:
            query += " AND direction = ?"
            params.append(direction)
        if provider:
            query += " AND provider = ?"
            params.append(provider)
        
        query += " ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]
    
    def get_llm_request(self, req_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific LLM request by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM llm_requests WHERE id = ?", (req_id,)).fetchone()
        return self._row_to_dict(row) if row else None
    
    def count_llm_requests(
        self,
        agent_name: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> int:
        """Count LLM request logs."""
        conn = self._get_conn()
        query = "SELECT COUNT(*) FROM llm_requests WHERE 1=1"
        params: list = []
        if agent_name:
            query += " AND agent_name = ?"
            params.append(agent_name)
        if provider:
            query += " AND provider = ?"
            params.append(provider)
        return conn.execute(query, params).fetchone()[0]
    
    # ---- Stats ----
    
    def get_stats(self) -> Dict[str, Any]:
        """Get overall statistics.
        
        Optimised to avoid expensive full-table aggregations on large
        databases.  Uses indexed COUNT queries and limits the session
        list to avoid scanning hundreds of thousands of rows.
        """
        conn = self._get_conn()
        
        turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        request_count = conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0]
        
        # Use separate indexed queries instead of UNION (faster on large DBs)
        agents_set: set[str] = set()
        for row in conn.execute(
            "SELECT DISTINCT agent_name FROM turns WHERE agent_name != ''"
        ).fetchall():
            agents_set.add(row[0])
        for row in conn.execute(
            "SELECT DISTINCT agent_name FROM llm_requests WHERE agent_name != ''"
        ).fetchall():
            agents_set.add(row[0])
        
        providers = [r[0] for r in conn.execute(
            "SELECT DISTINCT provider FROM llm_requests WHERE provider != ''"
        ).fetchall()]
        
        # Error count uses partial index (fast)
        error_count = conn.execute(
            "SELECT COUNT(*) FROM llm_requests WHERE error IS NOT NULL AND error != ''"
        ).fetchone()[0]
        
        # Session count instead of full list (much cheaper)
        session_count_turns = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM turns WHERE session_id != ''"
        ).fetchone()[0]
        session_count_reqs = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM llm_requests WHERE session_id != ''"
        ).fetchone()[0]
        session_count = max(session_count_turns, session_count_reqs)
        
        # DB file size
        db_size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        
        return {
            "total_turns": turn_count,
            "total_llm_requests": request_count,
            "unique_agents": sorted(agents_set),
            "unique_session_count": session_count,
            "unique_providers": sorted(providers),
            "error_count": error_count,
            "db_size_mb": round(db_size_bytes / (1024 * 1024), 1),
        }
    
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
    
    def clear_older_than(self, hours: float = 24) -> Dict[str, int]:
        """Clear entries older than specified hours."""
        conn = self._get_conn()
        cutoff_ms = (time.time() - hours * 3600) * 1000
        turns = conn.execute(
            "DELETE FROM turns WHERE timestamp_ms < ?", (cutoff_ms,)
        ).rowcount
        requests = conn.execute(
            "DELETE FROM llm_requests WHERE timestamp_ms < ?", (cutoff_ms,)
        ).rowcount
        conn.commit()
        return {"turns_deleted": turns, "requests_deleted": requests}
    
    def prune_to_max(self, max_turns: int = 5000, max_requests: int = 5000) -> Dict[str, int]:
        """Prune tables to keep only the most recent N entries.
        
        Args:
            max_turns: Maximum turns to keep
            max_requests: Maximum LLM requests to keep
            
        Returns:
            Counts of deleted rows
        """
        conn = self._get_conn()
        turns_deleted = 0
        requests_deleted = 0
        
        turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        if turn_count > max_turns:
            # Delete oldest rows, keeping max_turns most recent
            turns_deleted = conn.execute(
                "DELETE FROM turns WHERE id NOT IN "
                "(SELECT id FROM turns ORDER BY timestamp_ms DESC LIMIT ?)",
                (max_turns,)
            ).rowcount
        
        req_count = conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0]
        if req_count > max_requests:
            requests_deleted = conn.execute(
                "DELETE FROM llm_requests WHERE id NOT IN "
                "(SELECT id FROM llm_requests ORDER BY timestamp_ms DESC LIMIT ?)",
                (max_requests,)
            ).rowcount
        
        conn.commit()
        return {"turns_deleted": turns_deleted, "requests_deleted": requests_deleted}

    def vacuum(self) -> None:
        """Reclaim disk space after deletions."""
        conn = self._get_conn()
        conn.execute("VACUUM")

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

    def enforce_retention(self) -> Dict[str, int]:
        """Prune oldest rows when the data size exceeds the high watermark.

        Hysteresis: only fires above HIGH (~90% of the cap) and then deletes the
        oldest turns/requests in batches down to LOW (~75%). The file does not
        shrink (no VACUUM) but stops growing — freed pages are reused. Safe to
        call from any worker thread; concurrent calls are skipped by the caller.
        """
        if self._max_size_bytes <= 0:
            return {"turns_deleted": 0, "requests_deleted": 0}
        conn = self._get_conn()
        if self._used_bytes(conn) <= self._high_bytes:
            return {"turns_deleted": 0, "requests_deleted": 0}

        turns_deleted = 0
        requests_deleted = 0
        # Aim a hair below LOW so a single proportional pass lands safely under it.
        target = max(1, int(self._low_bytes * 0.98))
        # Delete the oldest rows in proportion to how far we are over target,
        # re-measuring each pass (row sizes vary, so converge in a few passes
        # rather than guessing a fixed batch — too-large a batch would wipe the
        # whole table when rows are big; too-small would checkpoint-thrash).
        for _ in range(16):
            used = self._used_bytes(conn)
            if used <= target:
                break
            turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            req_count = conn.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0]
            if turn_count == 0 and req_count == 0:
                break
            frac = min(1.0, (used - target) / used)
            drop_t = max(1, int(turn_count * frac)) if turn_count else 0
            drop_r = max(1, int(req_count * frac)) if req_count else 0
            if drop_t:
                turns_deleted += conn.execute(
                    "DELETE FROM turns WHERE id IN "
                    "(SELECT id FROM turns ORDER BY timestamp_ms ASC LIMIT ?)",
                    (drop_t,),
                ).rowcount
            if drop_r:
                requests_deleted += conn.execute(
                    "DELETE FROM llm_requests WHERE id IN "
                    "(SELECT id FROM llm_requests ORDER BY timestamp_ms ASC LIMIT ?)",
                    (drop_r,),
                ).rowcount
            conn.commit()
            # Checkpoint so the freelist (and thus _used_bytes) reflects the
            # deletes in WAL mode; ignore if checkpointing is unavailable.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass

        if turns_deleted or requests_deleted:
            logger.info(
                "message_debugger retention: pruned %d turns + %d requests "
                "(data over %.0f MB high-watermark, down toward %.0f MB)",
                turns_deleted, requests_deleted,
                self._high_bytes / 1024**2, self._low_bytes / 1024**2,
            )
        return {"turns_deleted": turns_deleted, "requests_deleted": requests_deleted}

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

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
    
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
