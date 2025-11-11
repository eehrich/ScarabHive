"""CLI interface for sqlite_query plugin."""

import sys
import json
import argparse
import asyncio
from unittest.mock import AsyncMock
from src.plugins.sqlite_query.server import SqliteQueryServer
from agent_system.config import AgentSystemConfig, MCPConfig
from unittest.mock import MagicMock


async def execute_query(database: str, sql: str) -> dict:
    """Execute SQL query with mock status."""
    # Mock configs
    system_config = MagicMock(spec=AgentSystemConfig)
    mcp_config = MagicMock(spec=MCPConfig)
    mcp_config.database = database
    mcp_config.query_timeout = 30
    
    # Mock status
    status = AsyncMock()
    status.progress = AsyncMock()
    status.end = AsyncMock()
    status.error = AsyncMock()
    
    # Create server and execute
    server = SqliteQueryServer(name="cli_sqlite", system_config=system_config, mcp_config=mcp_config)
    result = await server.execute_sql({
        "sql": sql,
        "_status": status
    })
    
    return result


def main():
    """CLI entry point for sqlite-query command."""
    parser = argparse.ArgumentParser(
        description="Execute SQL queries on SQLite databases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
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
    )
    
    parser.add_argument(
        "--database", "-d",
        required=True,
        help="Database path (absolute or relative)"
    )
    
    parser.add_argument(
        "--sql", "-s",
        required=True,
        help="SQL statement to execute"
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    
    args = parser.parse_args()
    
    # Execute SQL asynchronously
    result = asyncio.run(execute_query(args.database, args.sql))
    
    # Output result
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if result["status"] == "error":
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        
        if "rows" in result:
            # SELECT query
            print(f"Found {result['row_count']} rows")
            if result["row_count"] > 0:
                # Print as table
                columns = result["columns"]
                rows = result["rows"]
                
                # Calculate column widths
                widths = {col: len(col) for col in columns}
                for row in rows:
                    for col in columns:
                        val_str = str(row.get(col, ""))
                        widths[col] = max(widths[col], len(val_str))
                
                # Print header
                header = " | ".join(col.ljust(widths[col]) for col in columns)
                print(header)
                print("-" * len(header))
                
                # Print rows
                for row in rows:
                    row_str = " | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns)
                    print(row_str)
        else:
            # Write operation
            print(f"Success: {result['rows_affected']} rows affected")
            if result.get("last_row_id"):
                print(f"Last inserted ID: {result['last_row_id']}")


if __name__ == "__main__":
    main()
