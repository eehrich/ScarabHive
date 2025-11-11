"""SQLite Query MCP Server implementation.

This module provides simple SQL execution for debugging database content.
No safety checks, no restrictions - direct SQL execution.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class SqliteQueryServer(SchemaBasedMCPServer):
    """SQLite Query MCP server for debugging.

    This server provides simple SQL execution without restrictions.
    Tool is automatically loaded from schema.yaml by SchemaBasedMCPServer.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract configuration from mcp_config
        self.database = str(getattr(mcp_config, 'database', 'default.db'))
        self.query_timeout = int(getattr(mcp_config, 'query_timeout', 30))
        
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
                    
                    # Success message for user
                    await status.end(f"Query returned {len(result_rows)} row(s)")
                    
                    result = {
                        "status": "success",
                        "rows": result_rows,
                        "row_count": len(result_rows),
                        "columns": columns
                    }
                else:
                    # Write operation (INSERT/UPDATE/DELETE)
                    conn.commit()
                    
                    # Success message for user
                    await status.end(f"Query affected {cursor.rowcount} row(s)")
                    
                    result = {
                        "status": "success",
                        "rows_affected": cursor.rowcount,
                        "last_row_id": cursor.lastrowid if cursor.lastrowid > 0 else None
                    }
                
                logger.info(f"Executed SQL on {self.database}: {sql[:100]}")
                return result
                
            finally:
                if conn:
                    conn.close()
        
        except sqlite3.Error as e:
            error_msg = f"SQL error: {str(e)}"
            logger.error(error_msg)
            await status.error(error_msg)
            return {"status": "error", "error": str(e)}
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(error_msg)
            await status.error(error_msg)
            return {"status": "error", "error": str(e)}
