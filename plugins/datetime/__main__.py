#!/usr/bin/env python3
"""DateTime MCP Server main entry point (plugin-local).

This is the CLI entry for the datetime plugin. It mirrors the original
`agent_system.servers.datetime.__main__` behavior but lives under
`plugins.datetime` so the implementation is contained in the plugin.
"""

from __future__ import annotations

import asyncio
import argparse
from .server import DateTimeServer


async def main():
    parser = argparse.ArgumentParser(description="DateTime MCP Server")
    parser.add_argument("--timezone", default="UTC", help="Timezone for datetime operations")
    parser.add_argument("--format", default="%Y-%m-%d %H:%M:%S", help="Datetime format string")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9003, help="Server port")
    args = parser.parse_args()
    
    server = DateTimeServer()
    
    if args.server:
        print(f"Starting DateTime MCP Server on port {args.port}")
        try:
            # Import lazily because the test subprocess may not have the full package on sys.path
            from agent_system.http_server import serve_mcp_server
        except Exception:
            print("serve_mcp_server not available; cannot start HTTP server in this environment")
            return

        serve_mcp_server(server, port=args.port)
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


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
