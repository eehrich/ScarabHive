"""SQLite storage for the context usage tracker.

Replaces a JSON file that was read ONCE at construction and then rewritten in
full on every change. That worked for a single process and silently destroyed
data across two: `agent-cli` and `agent-api` each held their own in-memory copy,
and whichever wrote last overwrote the other's runs entirely — measured, not
inferred. The panel in the API could not show a CLI run without a restart, and
the CLI run was gone from the file the moment the API recorded anything.

Two tables, written in ONE transaction per call:

``usage_snapshots``  append-only, one row per LLM call. Replaces the
                     1000-entry deque; retention is by age, not by a ring
                     buffer that silently drops the oldest.
``agent_stats``      all-time accumulators per agent, upserted. Kept separate
                     ON PURPOSE so retention can prune snapshots without
                     rewriting history — the same split the JSON had between
                     its "agents" and "history" sections.
``session_invalidations``  the "data is stale since compaction" flag, which used
                     to be a per-process set and therefore invisible to the very
                     process that renders it.

WAL mode and thread-local connections follow message_debugger/database.py.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Snapshots older than this are pruned. Age, not size: the volume is tiny
#: (~250 rows/day measured over the JSON file's four days) and an age window is
#: what someone reading the panel actually reasons about.
DEFAULT_RETENTION_DAYS = 90

#: Backstop against a runaway loop filling the table faster than the age window
#: prunes it. Well above 90 days of normal traffic.
MAX_SNAPSHOT_ROWS = 500_000

#: How often retention runs, in write calls. Every write would cost a DELETE
#: scan per LLM call for nothing.
_RETENTION_EVERY_N_WRITES = 500


class UsageDatabase:
    """Cross-process storage for usage snapshots and per-agent totals."""

    def __init__(self, db_path: str | Path,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 max_rows: int = MAX_SNAPSHOT_ROWS) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.max_rows = max_rows
        self._local = threading.local()
        self._writes_since_retention = 0
        self._init_schema()
        logger.info(f"UsageDatabase initialized: {self.db_path}")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _get_conn(self) -> sqlite3.Connection:
        """Thread-local connection. WAL so a reader never blocks the writer —
        the API renders the panel while the worker is recording."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                   timeout=10.0)
            conn.row_factory = sqlite3.Row
            # busy_timeout FIRST. Two processes starting together both run the
            # schema init, and `PRAGMA journal_mode=WAL` itself takes an
            # exclusive lock — measured: setting the timeout afterwards leaves
            # that very pragma without one, and the loser died with
            # "database is locked" before it had a connection at all.
            conn.execute("PRAGMA busy_timeout=10000")
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            actual = (mode[0] if mode else "").lower()
            if actual != "wal":
                # WAL needs shared memory and is refused on network shares —
                # silently, by returning the mode still in effect. Without WAL a
                # reader blocks the writer, which is the whole reason this store
                # exists. Say so instead of degrading quietly.
                logger.warning(
                    f"Usage database at {self.db_path} runs in '{actual}' mode, "
                    f"not WAL — concurrent access will block. Is it on a "
                    f"network share?"
                )
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS usage_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                agent_id TEXT NOT NULL,
                agent_name TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                total_tokens INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                context_window INTEGER NOT NULL DEFAULT 0,
                usage_percentage REAL NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                tool_definition_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cost REAL,
                cost_is_estimate INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT '',
                latency_ms REAL
            );
            -- On `timestamp`, not on `id`: `id` IS the rowid, so an index on it
            -- is a duplicate SQLite never uses, while the age-based retention
            -- DELETE (WHERE timestamp < ?) had nothing to walk and full-scanned
            -- the table — inside a write transaction that blocks the other
            -- process.
            CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_snapshots(session_id);
            CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage_snapshots(agent_id);
            DROP INDEX IF EXISTS idx_usage_time;

            CREATE TABLE IF NOT EXISTS agent_stats (
                agent_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL DEFAULT '',
                total_calls INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                peak_tokens INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                last_activity REAL NOT NULL DEFAULT 0,
                total_cached_tokens INTEGER NOT NULL DEFAULT 0,
                total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
                total_completion_tokens INTEGER NOT NULL DEFAULT 0,
                cache_rate_cached_tokens INTEGER NOT NULL DEFAULT 0,
                cache_rate_prompt_tokens INTEGER NOT NULL DEFAULT 0,
                total_cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost REAL NOT NULL DEFAULT 0,
                cost_known_calls INTEGER NOT NULL DEFAULT 0,
                cost_estimated_calls INTEGER NOT NULL DEFAULT 0,
                total_latency_ms REAL NOT NULL DEFAULT 0,
                latency_calls INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS session_invalidations (
                session_id TEXT PRIMARY KEY,
                invalidated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return row["value"] if row else None

    def claim_once(self, key: str) -> bool:
        """True for EXACTLY ONE caller, across processes. False for everyone else.

        A check-then-act over two transactions is not enough here: `agent-api`
        and `agent-writer-worker` are restarted together, both open the store in
        the same instant, both see "not imported yet" and both import.
        Reproduced — the legacy history landed twice. `INSERT OR IGNORE` plus
        rowcount decides it in one atomic step.
        """
        try:
            conn = self._get_conn()
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (key, "claimed"))
                return (cur.rowcount or 0) == 1
        except sqlite3.Error as e:
            logger.error(f"Failed to claim {key}: {e}")
            return False

    def release_claim(self, key: str) -> None:
        """Give the claim back so a later start can retry a failed import."""
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        except sqlite3.Error as e:
            logger.error(f"Failed to release {key}: {e}")

    def set_meta(self, key: str, value: str) -> None:
        try:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value))
        except sqlite3.Error as e:
            logger.error(f"Failed to write meta {key}: {e}")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def record(self, snapshot: Dict[str, Any]) -> None:
        """Append one snapshot and fold it into the agent's totals.

        Both in one transaction: a snapshot the totals do not know about (or the
        reverse) is exactly the kind of disagreement that made the old cache-rate
        read 799 %.
        """
        conn = self._get_conn()
        cost = snapshot.get("cost")
        latency = snapshot.get("latency_ms")
        try:
            with conn:  # BEGIN ... COMMIT / ROLLBACK
                conn.execute("""
                    INSERT INTO usage_snapshots
                    (timestamp, agent_id, agent_name, session_id, request_id,
                     total_tokens, prompt_tokens, completion_tokens, message_count,
                     context_window, usage_percentage, cached_tokens,
                     tool_definition_tokens, cache_write_tokens, cost,
                     cost_is_estimate, model, latency_ms)
                    VALUES (:timestamp, :agent_id, :agent_name, :session_id, :request_id,
                            :total_tokens, :prompt_tokens, :completion_tokens, :message_count,
                            :context_window, :usage_percentage, :cached_tokens,
                            :tool_definition_tokens, :cache_write_tokens, :cost,
                            :cost_is_estimate, :model, :latency_ms)
                """, {
                    "timestamp": snapshot.get("timestamp", time.time()),
                    "agent_id": snapshot.get("agent_id", ""),
                    "agent_name": snapshot.get("agent_name", ""),
                    "session_id": snapshot.get("session_id", ""),
                    "request_id": snapshot.get("request_id", ""),
                    "total_tokens": snapshot.get("total_tokens", 0),
                    "prompt_tokens": snapshot.get("prompt_tokens", 0),
                    "completion_tokens": snapshot.get("completion_tokens", 0),
                    "message_count": snapshot.get("message_count", 0),
                    "context_window": snapshot.get("context_window", 0),
                    "usage_percentage": snapshot.get("usage_percentage", 0.0),
                    "cached_tokens": snapshot.get("cached_tokens", 0),
                    "tool_definition_tokens": snapshot.get("tool_definition_tokens", 0),
                    "cache_write_tokens": snapshot.get("cache_write_tokens", 0),
                    "cost": cost,
                    "cost_is_estimate": 1 if snapshot.get("cost_is_estimate") else 0,
                    "model": snapshot.get("model", ""),
                    "latency_ms": latency,
                })

                # The accumulators are folded in SQL so two processes recording
                # at the same moment add up instead of overwriting each other —
                # the whole point of the move off the JSON file.
                conn.execute("""
                    INSERT INTO agent_stats (
                        agent_id, agent_name, total_calls, total_tokens, peak_tokens,
                        message_count, last_activity, total_cached_tokens,
                        total_prompt_tokens, total_completion_tokens,
                        cache_rate_cached_tokens, cache_rate_prompt_tokens,
                        total_cache_write_tokens, total_cost, cost_known_calls,
                        cost_estimated_calls, total_latency_ms, latency_calls
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        agent_name = excluded.agent_name,
                        total_calls = agent_stats.total_calls + 1,
                        total_tokens = agent_stats.total_tokens + excluded.total_tokens,
                        peak_tokens = MAX(agent_stats.peak_tokens, excluded.peak_tokens),
                        message_count = excluded.message_count,
                        last_activity = excluded.last_activity,
                        total_cached_tokens = agent_stats.total_cached_tokens + excluded.total_cached_tokens,
                        total_prompt_tokens = agent_stats.total_prompt_tokens + excluded.total_prompt_tokens,
                        total_completion_tokens = agent_stats.total_completion_tokens + excluded.total_completion_tokens,
                        cache_rate_cached_tokens = agent_stats.cache_rate_cached_tokens + excluded.cache_rate_cached_tokens,
                        cache_rate_prompt_tokens = agent_stats.cache_rate_prompt_tokens + excluded.cache_rate_prompt_tokens,
                        total_cache_write_tokens = agent_stats.total_cache_write_tokens + excluded.total_cache_write_tokens,
                        total_cost = agent_stats.total_cost + excluded.total_cost,
                        cost_known_calls = agent_stats.cost_known_calls + excluded.cost_known_calls,
                        cost_estimated_calls = agent_stats.cost_estimated_calls + excluded.cost_estimated_calls,
                        total_latency_ms = agent_stats.total_latency_ms + excluded.total_latency_ms,
                        latency_calls = agent_stats.latency_calls + excluded.latency_calls
                """, (
                    snapshot.get("agent_id", ""), snapshot.get("agent_name", ""),
                    snapshot.get("total_tokens", 0), snapshot.get("total_tokens", 0),
                    snapshot.get("message_count", 0), time.time(),
                    snapshot.get("cached_tokens", 0),
                    snapshot.get("prompt_tokens", 0), snapshot.get("completion_tokens", 0),
                    # Lockstep pair: same statement, same call. See AgentStats.
                    snapshot.get("cached_tokens", 0), snapshot.get("prompt_tokens", 0),
                    snapshot.get("cache_write_tokens", 0),
                    cost or 0.0,
                    1 if cost is not None else 0,
                    1 if (cost is not None and snapshot.get("cost_is_estimate")) else 0,
                    latency or 0.0,
                    1 if latency is not None else 0,
                ))

                # A new snapshot means fresh data for this session.
                conn.execute("DELETE FROM session_invalidations WHERE session_id = ?",
                             (snapshot.get("session_id", ""),))
        except sqlite3.Error as e:
            # Telemetry must never sink the call it measures.
            logger.error(f"Failed to record usage snapshot: {e}")
            return

        self._writes_since_retention += 1
        if self._writes_since_retention >= _RETENTION_EVERY_N_WRITES:
            self._writes_since_retention = 0
            self._apply_retention()

    def invalidate_session(self, session_id: str) -> None:
        try:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT INTO session_invalidations (session_id, invalidated_at) "
                    "VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "invalidated_at = excluded.invalidated_at",
                    (session_id, time.time()))
        except sqlite3.Error as e:
            logger.error(f"Failed to invalidate session {session_id}: {e}")

    def is_invalidated(self, session_id: str) -> bool:
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT 1 FROM session_invalidations WHERE session_id = ?",
                (session_id,)).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def clear(self) -> None:
        try:
            conn = self._get_conn()
            with conn:
                conn.execute("DELETE FROM usage_snapshots")
                conn.execute("DELETE FROM agent_stats")
                conn.execute("DELETE FROM session_invalidations")
        except sqlite3.Error as e:
            logger.error(f"Failed to clear usage data: {e}")

    def import_legacy(self, snapshots: List[Dict[str, Any]],
                      agents: Dict[str, Dict[str, Any]]) -> None:
        """Load a pre-SQLite JSON dump in ONE transaction.

        The agent totals are INSERTed as they stand, not folded call by call:
        they are all-time sums whose calls are long gone from the capped
        history, so replaying the snapshots through ``record`` would count the
        retained 1000 twice on top of totals that already include them.
        """
        if not snapshots and not agents:
            return
        conn = self._get_conn()
        columns = ("timestamp", "agent_id", "agent_name", "session_id", "request_id",
                   "total_tokens", "prompt_tokens", "completion_tokens",
                   "message_count", "context_window", "usage_percentage",
                   "cached_tokens", "tool_definition_tokens", "cache_write_tokens",
                   "cost", "cost_is_estimate", "model", "latency_ms")
        stat_columns = ("agent_id", "agent_name", "total_calls", "total_tokens",
                        "peak_tokens", "message_count", "last_activity",
                        "total_cached_tokens", "total_prompt_tokens",
                        "total_completion_tokens", "cache_rate_cached_tokens",
                        "cache_rate_prompt_tokens", "total_cache_write_tokens",
                        "total_cost", "cost_known_calls", "cost_estimated_calls",
                        "total_latency_ms", "latency_calls")
        # Per-column defaults for a key that is absent entirely. Text columns
        # get "", the nullable REALs get None — NOT 0: a call that never had a
        # price would otherwise count as a known $0.00 call and inflate
        # cost_known_calls, and a timestamp of 0 is deleted by the first
        # retention pass. Everything else is a NOT NULL integer, so 0.
        text_cols = {"agent_id", "agent_name", "session_id", "request_id", "model"}
        nullable_cols = {"cost", "latency_ms"}

        def cell(row: Dict[str, Any], col: str) -> Any:
            if col == "cost_is_estimate":
                return 1 if row.get(col) else 0
            if col == "timestamp":
                return row.get(col) if row.get(col) is not None else time.time()
            if col in text_cols:
                return row.get(col, "")
            if col in nullable_cols:
                return row.get(col)
            return row.get(col, 0)

        with conn:
            conn.executemany(
                f"INSERT INTO usage_snapshots ({','.join(columns)}) "
                f"VALUES ({','.join('?' * len(columns))})",
                [tuple(cell(s, c) for c in columns) for s in snapshots])
            conn.executemany(
                f"INSERT OR REPLACE INTO agent_stats ({','.join(stat_columns)}) "
                f"VALUES ({','.join('?' * len(stat_columns))})",
                # The dict KEY is the fallback agent_id: keying every row off a
                # missing field would collapse every agent onto "".
                [tuple(aid if c == "agent_id" and not st.get(c)
                       else st.get(c, "" if c in ("agent_id", "agent_name") else 0)
                       for c in stat_columns)
                 for aid, st in agents.items()])

    def _apply_retention(self) -> None:
        conn = self._get_conn()
        cutoff = time.time() - self.retention_days * 86400
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM usage_snapshots WHERE timestamp < ?", (cutoff,))
                by_age = cur.rowcount or 0
                # Backstop: a runaway loop can outrun the age window.
                cur = conn.execute("""
                    DELETE FROM usage_snapshots WHERE id <= (
                        SELECT id FROM usage_snapshots
                        ORDER BY id DESC LIMIT 1 OFFSET ?
                    )
                """, (self.max_rows,))
                by_count = cur.rowcount or 0
        except sqlite3.Error as e:
            logger.error(f"Usage retention failed: {e}")
            return
        if by_age or by_count:
            logger.info(f"Usage retention: pruned {by_age} snapshots older than "
                        f"{self.retention_days}d, {by_count} over the {self.max_rows}-row cap")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def recent_snapshots(self, limit: int,
                         session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The newest ``limit`` snapshots, oldest first.

        Oldest-first because every caller treats the list as a timeline —
        `[-1]` is "now" and `[0]` is the start of the window.

        With ``session_id`` the filter runs BEFORE the limit — including the
        session's sub-agent calls. That ordering matters now that the store is
        shared: taking the newest N rows globally and filtering afterwards
        drops a quiet session out of view as soon as another process, or a
        coordinator fanning out to sub-agents, fills the window. The rows are
        still there; they just fall outside it.

        ``substr(request_id, 1, n) = prefix`` rather than LIKE: a request id is
        arbitrary text, and ``_`` is LIKE's single-character wildcard — the
        prefixes here are FULL of underscores.
        """
        try:
            conn = self._get_conn()
            if session_id is None:
                rows = conn.execute(
                    "SELECT * FROM usage_snapshots ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            else:
                # Sub-agent calls run in their own sessions; their request ids
                # carry the parent's as a prefix (<parent>_sub_<id>,
                # transitively). The session's OWN requests define the tree.
                own = [r[0] for r in conn.execute(
                    "SELECT DISTINCT request_id FROM usage_snapshots "
                    "WHERE session_id = ? AND request_id != ''",
                    (session_id,)).fetchall()]
                params: list[Any] = [session_id]
                clause = "session_id = ?"
                if own:
                    prefixes = [r + "_" for r in own]
                    clause += " OR " + " OR ".join(
                        "substr(request_id, 1, ?) = ?" for _ in prefixes)
                    for p in prefixes:
                        params.extend((len(p), p))
                params.append(limit)
                rows = conn.execute(
                    f"SELECT * FROM usage_snapshots WHERE {clause} "
                    f"ORDER BY id DESC LIMIT ?", tuple(params)).fetchall()
        except sqlite3.Error as e:
            logger.error(f"Failed to read usage snapshots: {e}")
            return []
        return [_row_to_snapshot(r) for r in reversed(rows)]

    def agent_stats(self) -> Dict[str, Dict[str, Any]]:
        try:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM agent_stats").fetchall()
        except sqlite3.Error as e:
            logger.error(f"Failed to read agent stats: {e}")
            return {}
        return {r["agent_id"]: dict(r) for r in rows}

    # "The latest snapshot" means the latest CONVERSATION, which is what every
    # caller does with it: the panel's "Context now" card, the chat footer's
    # measured fill, and the two context plugins asking how full it got. A
    # call that carries no conversation -- a decision, a synthesis that
    # reports tokens -- is stored with context_window 0, and it arrives on the
    # same session id as the chat it was made from. Without this predicate the
    # last such call would answer "how full is the context" with its own size
    # and a window of 0.
    _LATEST = "SELECT * FROM usage_snapshots WHERE context_window > 0"

    def latest_snapshot(self) -> Optional[Dict[str, Any]]:
        try:
            conn = self._get_conn()
            row = conn.execute(
                f"{self._LATEST} ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error as e:
            logger.error(f"Failed to read latest snapshot: {e}")
            return None
        return _row_to_snapshot(row) if row else None

    def latest_for_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            conn = self._get_conn()
            row = conn.execute(
                f"{self._LATEST} AND session_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        except sqlite3.Error as e:
            logger.error(f"Failed to read latest snapshot for {session_id}: {e}")
            return None
        return _row_to_snapshot(row) if row else None

    def count(self) -> int:
        try:
            conn = self._get_conn()
            return conn.execute("SELECT COUNT(*) FROM usage_snapshots").fetchone()[0]
        except sqlite3.Error:
            return 0


def _row_to_snapshot(row: sqlite3.Row) -> Dict[str, Any]:
    """A row in the shape the snapshot dataclass used to serialize to.

    The web endpoints and the panel consume this shape directly, so it must not
    drift — `id` is dropped and the integer flag becomes a bool again.
    """
    data = dict(row)
    data.pop("id", None)
    data["cost_is_estimate"] = bool(data.get("cost_is_estimate"))
    return data
