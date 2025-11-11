"""CLI entry point for sqlite_query MCP server."""

import sys
import asyncio
from mcp.server.stdio import stdio_server
from src.plugins.sqlite_query.server import SqliteQueryServer


async def main():
    """Run the MCP server."""
    server = SqliteQueryServer()
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def cli_main():
    """CLI entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli_main()
