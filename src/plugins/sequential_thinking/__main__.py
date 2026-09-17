#!/usr/bin/env python3
"""CLI entrypoint for the sequential_thinking plugin.

Provides a help/CLI surface so `python -m plugins.sequential_thinking --help` works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plugins.sequential_thinking",
        description="Sequential Thinking Tool Server - Step-by-step reasoning with branching and revision"
    )

    # Operation selection
    parser.add_argument(
        "--operation",
        choices=["think", "clear", "summary"],
        default="think",
        help="Operation to perform: think (add thought), clear (clear history), summary (get thought summary)"
    )
    
    # Think operation parameters
    parser.add_argument(
        "--thought",
        type=str,
        help="Thought content (for think operation)"
    )
    parser.add_argument(
        "--thought-number",
        type=int,
        default=1,
        help="Current thought number (1-indexed)"
    )
    parser.add_argument(
        "--total-thoughts",
        type=int,
        default=5,
        help="Estimated total thoughts needed"
    )
    parser.add_argument(
        "--next-thought-needed",
        action="store_true",
        default=True,
        help="Whether another thought step is needed"
    )
    parser.add_argument(
        "--session-id",
        type=str,
        help="Session ID (auto-generated if omitted)"
    )
    parser.add_argument(
        "--branch-id",
        type=str,
        help="Branch identifier for exploring alternatives"
    )
    parser.add_argument(
        "--branch-from-thought",
        type=int,
        help="Thought number to branch from"
    )
    parser.add_argument(
        "--is-revision",
        action="store_true",
        help="Mark this as a revision of previous thought"
    )
    parser.add_argument(
        "--revises-thought",
        type=int,
        help="Which thought number to revise"
    )
    
    # Summary operation parameters
    parser.add_argument(
        "--max-thoughts",
        type=int,
        default=10,
        help="Maximum thoughts to include in summary"
    )
    parser.add_argument(
        "--include-branches",
        action="store_true",
        default=True,
        help="Include branch info in summary"
    )

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (tool server)")
    parser.add_argument("--port", type=int, default=9011, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="sequential_thinking plugin 1.0.0")

    return parser


async def async_main():
    """Async main function for actual execution."""
    parser = build_parser()
    args = parser.parse_args()

    from .plugin import PLUGIN_FACTORY
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    
    # Create minimal config for CLI usage
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig(type="sequential_thinking", enabled=True)
    
    server = PLUGIN_FACTORY("sequential_thinking", system_config, server_config)

    if args.server:
        print(f"Starting Sequential Thinking Tool Server on port {args.port}")
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
            
            if args.operation == "think":
                if not args.thought:
                    print("Error: --thought is required for think operation")
                    return
                
                print(f"Adding thought {args.thought_number}/{args.total_thoughts}...")
                result = await server.call("sequentialthinking", {
                    "thought": args.thought,
                    "thought_number": args.thought_number,
                    "total_thoughts": args.total_thoughts,
                    "next_thought_needed": args.next_thought_needed,
                    "session_id": args.session_id,
                    "branch_id": args.branch_id,
                    "branch_from_thought": args.branch_from_thought,
                    "is_revision": args.is_revision,
                    "revises_thought": args.revises_thought,
                    "_status": mock_status
                })
            elif args.operation == "clear":
                print(f"Clearing {'session ' + args.session_id if args.session_id else 'all sessions'}...")
                result = await server.call("clear_history", {
                    "session_id": args.session_id,
                    "_status": mock_status
                })
            else:  # summary
                if not args.session_id:
                    print("Error: --session-id is required for summary operation")
                    return
                
                print(f"Generating summary for session {args.session_id}...")
                result = await server.call("get_thought_summary", {
                    "session_id": args.session_id,
                    "max_thoughts": args.max_thoughts,
                    "include_branches": args.include_branches,
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
        "description": "Sequential Thinking Tool Server",
        "operation": args.operation,
        "server_mode": args.server,
        "port": args.port,
    }

    print("Sequential Thinking Tool Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
