#!/usr/bin/env python3
"""CLI entrypoint for the llm_router plugin.

Provides a help/CLI surface so `python -m plugins.llm_router --help` works
for tooling and tests.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import sys
from typing import Any

from .server import LLMRouterServer
from agent_system.servers.http_server import serve_mcp_server
from agent_system.utils.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.llm_router", description="LLM Router MCP Server")

    # Core LLM parameters
    parser.add_argument("--message", "--prompt", help="Single message to send to LLM")
    parser.add_argument("--provider", choices=["openai", "ollama"], default="openai", help="LLM provider to use")
    parser.add_argument("--model", help="Specific model to use (optional)")
    parser.add_argument("--default-provider", choices=["openai", "ollama"], default="openai", help="Default provider")
    parser.add_argument("--default-model", default="gpt-4o-mini", help="Default model")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (MCP server)")
    parser.add_argument("--port", type=int, default=8081, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="llm_router plugin 1.0.0")

    return parser


async def async_main():
    # Setup logging for proper color output
    setup_logging(True, "INFO", "logs/llm_router.log")
    
    parser = build_parser()
    args = parser.parse_args()

    server = LLMRouterServer("llm_router")

    if args.server:
        print(f"Starting LLM Router MCP Server on port {args.port}")
        await serve_mcp_server(server, port=args.port)
    else:
        if not args.message:
            print("Error: --message is required when not in server mode")
            return

        try:
            result = await server.call("chat", {
                "message": args.message,
                "provider": args.provider,
                "model": args.model
            })
            print(f"LLM Response ({args.provider}/{args.model}):")
            if isinstance(result, dict):
                if "content" in result:
                    print(result["content"])
                elif "response" in result:
                    print(result["response"])
                else:
                    print(json.dumps(result, indent=2))
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")


def main(argv: list[str] | None = None) -> None:
    # Setup logging for proper color output
    setup_logging(True, "INFO", "logs/llm_router.log")
    
    parser = build_parser()
    args = parser.parse_args(argv)

    # For tests, print a concise summary showing that the parser accepted the args.
    summary: dict[str, Any] = {
        "description": "LLM Router MCP Server",
        "message": args.message,
        "provider": args.provider,
        "model": args.model,
        "default_provider": args.default_provider,
        "default_model": args.default_model,
        "server_mode": args.server,
        "port": args.port,
    }

    print("LLM Router MCP Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
