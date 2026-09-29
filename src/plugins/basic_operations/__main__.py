#!/usr/bin/env python3
"""CLI entrypoint for the basic_operations plugin.

Provides a help/CLI surface so `python -m plugins.basic_operations --help` works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plugins.basic_operations",
        description="Basic Operations Tool Server - Utility operations for testing and timing"
    )

    # Operation selection
    parser.add_argument(
        "--operation",
        choices=["wait", "ping"],
        default="ping",
        help="Operation to perform: wait (with countdown) or ping (connectivity test)"
    )
    
    # Wait operation parameters
    parser.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help="Number of seconds to wait (for wait operation)"
    )
    parser.add_argument(
        "--message",
        type=str,
        help="Optional message to display during wait"
    )
    
    # Ping operation parameters
    parser.add_argument(
        "--include-details",
        action="store_true",
        help="Include detailed system information in ping response"
    )

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (tool server)")
    parser.add_argument("--port", type=int, default=9010, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="basic_operations plugin 1.0.0")

    return parser


async def async_main():
    """Async main function for actual execution."""
    parser = build_parser()
    args = parser.parse_args()

    from .plugin import PLUGIN_FACTORY
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    
    # Create minimal config for CLI usage
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig(type="basic_operations", enabled=True)
    
    server = PLUGIN_FACTORY("basic_operations", system_config, server_config)

    if args.server:
        print(f"Starting Basic Operations Tool Server on port {args.port}")
        try:
            from agent_system.servers.http_server import serve_tool_server
        except Exception:
            print("serve_tool_server not available; cannot start HTTP server")
            return

        await serve_tool_server(server, port=args.port)
    else:
        try:
            from unittest.mock import AsyncMock
            mock_status = AsyncMock()
            
            if args.operation == "wait":
                print(f"Waiting {args.seconds} seconds...")
                result = await server.call("wait", {
                    "seconds": args.seconds,
                    "message": args.message if args.message else None,
                    "_status": mock_status
                })
            else:  # ping
                result = await server.call("ping", {
                    "include_details": args.include_details,
                    "_status": mock_status
                })
            
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Error: {e}")


def main(argv: list[str] | None = None) -> None:
    """Main function for test/validation purposes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    summary: dict[str, Any] = {
        "description": "Basic Operations Tool Server",
        "operation": args.operation,
        "seconds": args.seconds if args.operation == "wait" else None,
        "server_mode": args.server,
        "port": args.port,
    }

    print("Basic Operations Tool Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
