"""CLI: run one SQL statement through the plugin.

    mcp-sqlite-query --database data/writer/books.db --sql "PRAGMA table_info(books)"
    python -m plugins.sqlite_query --database <db> --sql "<sql>" --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from agent_system.config.models import AgentSystemConfig, ToolServerConfig

from .server import SqliteQueryServer

EPILOG = """
Examples:
  # Query database
  mcp-sqlite-query --database data/writer/books.db \\
      --sql "SELECT * FROM books WHERE status = 'draft'"

  # Update data
  mcp-sqlite-query --database data/writer/books.db \\
      --sql "UPDATE books SET status = 'completed' WHERE id = 1"

  # Schema inspection
  mcp-sqlite-query --database data/writer/books.db \\
      --sql "PRAGMA table_info(books)"
"""


async def execute_query(database: str, sql: str) -> dict[str, Any]:
    server = SqliteQueryServer("sqlite_query", AgentSystemConfig(),
                               ToolServerConfig(type="sqlite_query", enabled=True,
                                         database=database))
    # call_with_status opens the status scope the tool expects in ``_status``.
    return await server.call_with_status("sqlite_query_execute_sql", {"sql": sql})


def print_result(result: dict[str, Any]) -> int:
    """Human-readable output; the exit code says whether the statement ran."""
    if result.get("status") == "error":
        print(f"Error: {result.get('error')}", file=sys.stderr)
        # The plugin's recovery for a name miss: the whole point of it.
        if result.get("did_you_mean"):
            print(f"Did you mean: {result['did_you_mean']}", file=sys.stderr)
        if result.get("hint"):
            print(f"Hint: {result['hint']}", file=sys.stderr)
        for key in ("tables", "columns"):
            if result.get(key):
                print(f"{key.capitalize()}: {json.dumps(result[key], ensure_ascii=False)}",
                      file=sys.stderr)
        return 1
    if "rows" in result:
        print(f"Found {result['row_count']} rows")
        columns, rows = result["columns"], result["rows"]
        if rows:
            cells = [{c: "NULL" if r.get(c) is None else str(r.get(c)) for c in columns}
                     for r in rows]
            widths = {c: max([len(c)] + [len(cell[c]) for cell in cells]) for c in columns}
            header = " | ".join(c.ljust(widths[c]) for c in columns)
            print(header)
            print("-" * len(header))
            for cell in cells:
                print(" | ".join(cell[c].ljust(widths[c]) for c in columns))
    elif result.get("rows_affected", -1) < 0:
        # sqlite3 reports -1 for statements that change no rows (DDL).
        print("Success")
    else:
        print(f"Success: {result['rows_affected']} rows affected")
        if result.get("last_row_id"):
            print(f"Last inserted ID: {result['last_row_id']}")
    return 0


def cli_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-sqlite-query",
        description="Execute SQL queries on SQLite databases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG)
    parser.add_argument("--database", "-d", required=True,
                        help="Database path (absolute or relative)")
    parser.add_argument("--sql", "-s", required=True, help="SQL statement to execute")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args(argv)

    result = asyncio.run(execute_query(args.database, args.sql))
    if args.json:
        # default=str: a BLOB column comes back as bytes.
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        sys.exit(1 if result.get("status") == "error" else 0)
    sys.exit(print_result(result))


if __name__ == "__main__":
    cli_main()
