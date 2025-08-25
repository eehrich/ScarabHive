#!/usr/bin/env python3
"""DateTime MCP Server main entry point."""

from __future__ import annotations

import asyncio
import argparse
from ..http_server import serve_mcp_server
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
        serve_mcp_server(server, port=args.port)
    else:
        try:
            # Show current time
            result = await server.call("current_time", {
                "timezone": args.timezone,
                "format": args.format
            })
            print(f"Current time ({args.timezone}): {result}")
            
            # Show available functions
            tools_result = await server.call("list_tools", {})
            print("\nAvailable datetime functions:")
            if isinstance(tools_result, dict) and "tools" in tools_result:
                for tool in tools_result["tools"]:
                    print(f"- {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}")
            
        except Exception as e:
            print(f"Error: {e}")

def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(main())

if __name__ == "__main__":
    cli_main()
