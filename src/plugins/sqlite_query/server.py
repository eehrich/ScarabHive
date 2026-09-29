"""SQLite Query Tool Server implementation.

This module provides simple SQL execution for debugging database content.
No safety checks, no restrictions - direct SQL execution.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
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
                cols = [r[1] for r in conn.execute(
                    f"PRAGMA table_info({t})").fetchall()][:_MAX_COLUMNS_PER_TABLE]
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

    def _execute_sql_sync(self, sql: str, db_path: Path) -> dict[str, Any]:
        """Execute SQL synchronously - runs in thread pool."""
        conn = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=self.query_timeout)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Execute SQL (no parameters - LLM generates complete SQL)
            cursor.execute(sql)
            
            # Check if it's a SELECT query (has results)
            if cursor.description:
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                result_rows = [dict(row) for row in rows]
                
                return {
                    "status": "success",
                    "rows": result_rows,
                    "row_count": len(result_rows),
                    "columns": columns
                }
            else:
                # Write operation (INSERT/UPDATE/DELETE)
                conn.commit()
                
                return {
                    "status": "success",
                    "rows_affected": cursor.rowcount,
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
            result = await asyncio.to_thread(self._execute_sql_sync, sql, db_path)
            
            # Send success status based on result type
            if "row_count" in result:
                await status.end(f"Query returned {result['row_count']} row(s)")
            else:
                await status.end(f"Query affected {result.get('rows_affected', 0)} row(s)")
            
            logger.info(f"Executed SQL on {self.database}: {sql[:100]}")
            return result
        
        except sqlite3.Error as e:
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
