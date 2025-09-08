#!/usr/bin/env python3
"""CLI entrypoint for the datetime plugin.

Provides a help/CLI surface so `python -m plugins.datetime --help` works
for tooling and tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.datetime", description="DateTime MCP Server")

    # Core datetime parameters
    parser.add_argument("--timezone", default="UTC", help="Timezone for datetime operations")
    parser.add_argument("--format", default="%Y-%m-%d %H:%M:%S", help="Datetime format string")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (MCP server)")
    parser.add_argument("--port", type=int, default=9003, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="datetime plugin 1.0.0")

    return parser


async def async_main():
    """Async main function for actual execution."""
    parser = build_parser()
    args = parser.parse_args()

    from .server import DateTimeServer
    server = DateTimeServer()

    if args.server:
        print(f"Starting DateTime MCP Server on port {args.port}")
        try:
            # Import lazily because the test subprocess may not have the full package on sys.path
            from agent_system.servers.http_server import serve_mcp_server
        except Exception:
            print("serve_mcp_server not available; cannot start HTTP server in this environment")
            return

        await serve_mcp_server(server, port=args.port)
    else:
        try:
            # Show current time
            result = await server.call("current", {
                "timezone": args.timezone,
                "format": args.format
            })
            print(f"Current time ({args.timezone}): {result}")

            # Show available functions via schema
            schema = server.get_schema()
            print("\nAvailable datetime functions:")
            if isinstance(schema, dict):
                props = schema.get("function", {}).get("parameters", {}).get("properties", {})
                for name, meta in props.items():
                    print(f"- {name}: {meta.get('description', '')}")
        except Exception as e:
            print(f"Error: {e}")


def main(argv: list[str] | None = None) -> None:
    """Main function for test/validation purposes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # For tests, print a concise summary showing that the parser accepted the args.
    summary: dict[str, Any] = {
        "description": "DateTime MCP Server",
        "timezone": args.timezone,
        "format": args.format,
        "server_mode": args.server,
        "port": args.port,
    }

    print("DateTime MCP Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
