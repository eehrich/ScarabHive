#!/usr/bin/env python3
"""CLI: send one message to an LLM profile through the plugin, or serve it.

    python -m plugins.llm_router --profile fast --message "Hello"
    python -m plugins.llm_router --server --port 8081
"""

from __future__ import annotations

import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.llm_router", description="LLM Router Tool Server")
    parser.add_argument("--message", "--prompt", help="Message to send to the profile's model")
    parser.add_argument("--profile", help="LLM profile of config/llm.yaml (as the tool's profile)")
    parser.add_argument("--config", help="Config file (default: config/config.yaml or AGENT_CONFIG_PATH)")
    parser.add_argument("--server", action="store_true", help="Run in server mode (tool server)")
    parser.add_argument("--port", type=int, default=8081, help="Port to listen on when in server mode")
    parser.add_argument("--version", action="version", version="llm_router plugin 1.0.0")
    return parser


async def _run(args: argparse.Namespace) -> int:
    from agent_system.config.models import ToolServerConfig
    from agent_system.config.settings import load_settings

    from .server import LLMRouterServer

    # The loaded config, not a bare AgentSystemConfig(): the profiles live
    # in llm.yaml, and a default config has none.
    server = LLMRouterServer("llm_router", load_settings(args.config),
                             ToolServerConfig(type="llm_router", enabled=True))
    if args.server:
        from agent_system.servers.http_server import serve_tool_server

        print(f"Starting LLM Router Tool Server on port {args.port}")
        await serve_tool_server(server, port=args.port)
        return 0

    # call_with_status opens the status scope the tool expects in ``_status``.
    result = await server.call_with_status(
        "llm_router_chat", {"profile": args.profile, "message": args.message})
    if result.get("error"):
        print(f"Error: {result['error']}")
        return 1
    print(f"LLM Response ({result['profile']}: {result['provider']}/{result['model']}):")
    print(result["content"])
    return 0


def cli_main(argv: list[str] | None = None) -> None:
    """Synchronous entry point for the console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.server and not (args.message and args.profile):
        parser.error("--message and --profile are required when not in server mode")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    cli_main()
