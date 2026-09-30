"""SQLite Query Tool Server implementation.

This module provides simple SQL execution for debugging database content.
Any statement runs against the one configured file; what could reach other
files (ATTACH, VACUUM INTO, the directory pragmas) is refused.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.utils.suggest import suggest_path

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

#: SQLite's own wording for the two misses an agent can act on.
_NO_TABLE_RE = re.compile(r"no such table:\s*([\w.]+)")
_NO_COLUMN_RE = re.compile(r"no such column:\s*([\w.]+)")

#: Bounds for the recovery block: enough to re-orient, never a schema dump.
_MAX_TABLES = 25
_MAX_COLUMNS_PER_TABLE = 30

#: SQLite opcodes between two looks at the deadline and the cancel token.
_PROGRESS_STEPS = 10_000

#: Pragmas that point SQLite at another directory, process-wide.
_DIRECTORY_PRAGMAS = {"temp_store_directory", "data_store_directory"}

#: Pragmas whose setting is process-wide and can only be lowered; reading them is harmless.
_PROCESS_PRAGMAS = {"hard_heap_limit", "soft_heap_limit"}


def _positive_int(value: Any, default: int) -> int:
    """A setting that must be a positive whole number; anything else means the default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _answer_value(value: Any, max_chars: int) -> Any:
    """One cell as the answer carries it: long text cut, bytes as a hex literal.

    str(bytes) would triple the size and cannot be pasted back into SQL;
    x'..' can. A cut value ends in "…[<full length>]".
    """
    if isinstance(value, bytes):
        text = f"x'{value[:max_chars // 2].hex()}'"
        return text if len(value) * 2 <= max_chars else f"{text}…[{len(value)} bytes]"
    if isinstance(value, str) and len(value) > max_chars:
        return f"{value[:max_chars]}…[{len(value)} chars]"
    return value


def _only_this_file(action: int, arg1: Any, arg2: Any, _db: Any, _src: Any) -> int:
    """Authorizer: keep every statement inside the configured database file.

    ATTACH and VACUUM INTO (both reported as SQLITE_ATTACH) would open or
    create any path the process can reach -- another database, a new file,
    a network share.
    """
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() in _DIRECTORY_PRAGMAS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and arg2 is not None and str(arg1).lower() in _PROCESS_PRAGMAS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _schema_recovery(db_path: Path, error: str, sql: str) -> dict[str, Any] | None:
    """What the agent needs to fix a bad table/column name, from the error alone.

    Without this, ``no such table: X`` costs a second turn for the
    ``sqlite_master`` query — and that follow-up is exactly what every agent
    types next, so the tool can just answer it pre-emptively. Returns None for
    errors that are not name misses (syntax errors etc.): those have no
    mechanical recovery, and guessing would mislead.
    """
    kind, missing = None, None
    m = _NO_TABLE_RE.search(error or "")
    if m:
        kind, missing = "table", m.group(1)
    else:
        m = _NO_COLUMN_RE.search(error or "")
        if m:
            # Qualified names (t.col) miss on the column part.
            kind, missing = "column", m.group(1).rsplit(".", 1)[-1]
    if kind is None:
        return None

    try:
        # as_uri() escapes '#', '?' and '%' in the path; a raw f-string opened another file.
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]

            if kind == "table":
                return {
                    "did_you_mean": suggest_path(missing, tables),
                    "tables": tables[:_MAX_TABLES],
                    "hint": f"'{missing}' does not exist — the listed tables do",
                }

            # Column miss: only the tables the query actually names are
            # relevant. Their columns are the search space AND the answer.
            sql_words = {w.lower() for w in re.findall(r"[\w]+", sql or "")}
            referenced = [t for t in tables if t.lower() in sql_words] or tables
            columns_by_table: dict[str, list[str]] = {}
            all_columns: list[str] = []
            for t in referenced[:_MAX_TABLES]:
                cols = [r[0] for r in conn.execute(
                    "SELECT name FROM pragma_table_info(?)", (t,)).fetchall()][:_MAX_COLUMNS_PER_TABLE]
                columns_by_table[t] = cols
                all_columns.extend(cols)
            return {
                "did_you_mean": suggest_path(missing, all_columns),
                "columns": columns_by_table,
                "hint": (f"column '{missing}' does not exist in the referenced "
                         f"table(s) — their actual columns are listed"),
            }
        finally:
            conn.close()
    except sqlite3.Error as e:  # pragma: no cover - depends on host state
        logger.debug("schema recovery skipped: %s", e)
        return None


class SqliteQueryServer(SchemaBasedToolServer):
    """SQLite Query tool server for debugging.

    This server provides simple SQL execution without restrictions.
    Tool is automatically loaded from schema.yaml by SchemaBasedToolServer.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)
        
        # Extract configuration from server_config
        self.database = str(getattr(server_config, 'database', 'default.db'))
        self.query_timeout = int(getattr(server_config, 'query_timeout', 30))
        self.max_rows = _positive_int(getattr(server_config, 'max_rows', None), 500)
        self.max_value_bytes = _positive_int(
            getattr(server_config, 'max_value_bytes', None), 64 * 1024 * 1024)
        self.max_value_chars = _positive_int(getattr(server_config, 'max_value_chars', None), 10_000)
        
        logger.info(
            f"SqliteQuery server '{name}' initialized - database={self.database}, "
            f"query_timeout={self.query_timeout}"
        )

    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering."""
        return {
            "name": self.name,
            "database": self.database
        }

    def _execute_sql_sync(self, sql: str, db_path: Path, token: Any = None) -> dict[str, Any]:
        """Execute SQL synchronously - runs in thread pool."""
        conn = None
        deadline = time.monotonic() + self.query_timeout
        try:
            conn = sqlite3.connect(str(db_path), timeout=self.query_timeout)
            conn.row_factory = sqlite3.Row
            conn.set_authorizer(_only_this_file)
            # One value is built in one opcode, out of the progress handler's reach:
            # zeroblob(900000000) peaked at 13.5 GB. Bound it where SQLite makes it.
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.max_value_bytes)
            # The thread outlives a cancelled task: stop the statement itself.
            conn.set_progress_handler(
                lambda: int(time.monotonic() > deadline or bool(token and token.is_cancelled)),
                _PROGRESS_STEPS)
            cursor = conn.cursor()
            
            # Execute SQL (no parameters - LLM generates complete SQL)
            cursor.execute(sql)
            
            # Check if it's a SELECT query (has results)
            if cursor.description:
                rows = cursor.fetchmany(self.max_rows + 1)
                truncated = len(rows) > self.max_rows
                columns: list[str] = []
                for desc in cursor.description:
                    # a join's id and id: a dict keeps only one of them
                    col, n = desc[0], 2
                    while col in columns:
                        col, n = f"{desc[0]}_{n}", n + 1
                    columns.append(col)
                result_rows = [
                    dict(zip(columns, (_answer_value(v, self.max_value_chars) for v in row)))
                    for row in rows[:self.max_rows]]
                cursor.close()
                # INSERT/UPDATE/DELETE ... RETURNING also land here; uncommitted, they rolled back.
                conn.commit()
                
                result = {
                    "status": "success",
                    "rows": result_rows,
                    "row_count": len(result_rows),
                    "columns": columns
                }
                if truncated:
                    result["truncated"] = True
                    result["hint"] = f"only the first {self.max_rows} rows; use LIMIT/OFFSET for more"
                return result
            else:
                # Write operation (INSERT/UPDATE/DELETE)
                conn.commit()
                
                return {
                    "status": "success",
                    # rowcount is -1 for DML that starts with WITH; changes() is not.
                    "rows_affected": conn.execute("SELECT changes()").fetchone()[0],
                    "last_row_id": cursor.lastrowid if cursor.lastrowid > 0 else None
                }
        finally:
            if conn:
                conn.close()

    async def execute_sql(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Execute SQL statement and return results.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        token = params.get("_cancellation_token")
        try:
            sql = params.get("sql")
            
            # Get status context (mandatory from framework)
            status = params["_status"]
            
            if not sql:
                await status.error("SQL parameter is required")
                return {"status": "error", "error": "sql parameter is required"}
            
            db_path = Path(self.database)
            if not db_path.exists():
                error_msg = f"Database not found: {self.database}"
                await status.error(error_msg)
                return {"status": "error", "error": error_msg}
            
            # Progress: Starting query
            sql_preview = sql[:50] + "..." if len(sql) > 50 else sql
            await status.progress(f"Executing: {sql_preview}")
            
            # Execute SQL in thread pool to avoid blocking event loop
            result = await asyncio.to_thread(self._execute_sql_sync, sql, db_path, token)
            
            # Send success status based on result type
            if "row_count" in result:
                more = ", more cut off" if result.get("truncated") else ""
                await status.end(f"Query returned {result['row_count']} row(s){more}")
            else:
                await status.end(f"Query affected {result.get('rows_affected', 0)} row(s)")
            
            logger.info(f"Executed SQL on {self.database}: {sql[:100]}")
            return result
        
        except sqlite3.Error as e:
            if str(e) == "interrupted":
                if token and token.is_cancelled:
                    await status.error(f"Stopped on cancel: {sql[:80]}")
                    return {"error": "Tool 'execute_sql' was cancelled.", "cancelled": True,
                            "forced": token.is_forced}
                e = sqlite3.OperationalError(
                    f"interrupted after query_timeout ({self.query_timeout} s)")
            error_msg = f"SQL error: {str(e)}"
            logger.info(error_msg)
            out: dict[str, Any] = {"status": "error", "error": str(e)}
            # A name miss answers the follow-up the agent would ask anyway.
            recovery = await asyncio.to_thread(
                _schema_recovery, db_path, str(e), sql or "")
            if recovery:
                out.update(recovery)
                if recovery.get("did_you_mean"):
                    error_msg += f" — did you mean '{recovery['did_you_mean']}'?"
            await status.error(error_msg)
            return out
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(error_msg)
            await status.error(error_msg)
            return {"status": "error", "error": str(e)}
