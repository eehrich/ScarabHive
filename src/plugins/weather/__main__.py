"""CLI entrypoint for the weather plugin: python -m plugins.weather"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.weather", description="Weather Tool Server")

    # Core weather parameters
    parser.add_argument("--location", help="Location name (city, address, coordinates)")
    parser.add_argument("--source", help="Weather data source (wttr.in, weather.gov, met.no, marine.weather.gov)")
    parser.add_argument("--days", type=int, default=3, help="Number of forecast days (1-7)")
    parser.add_argument("--units", choices=["metric", "imperial"], default="metric", help="Temperature units")
    parser.add_argument("--include-marine", action="store_true", help="Include marine data (SST, wave heights)")
    parser.add_argument("--summary-format", choices=["daily", "hourly"], default="daily")
    parser.add_argument("--include-radiation", action="store_true", help="Accepted and ignored: the tool has no radiation data")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (tool server)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="weather plugin 0.1.0")

    return parser


async def async_main():
    """Async main function for actual execution."""
    parser = build_parser()
    args = parser.parse_args()

    # Lazy imports
    from .plugin import PLUGIN_FACTORY
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from agent_system.servers.http_server import serve_tool_server

    # Create minimal config for CLI usage
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig(type="weather", enabled=True)
    
    server = PLUGIN_FACTORY("weather", system_config, server_config)

    if args.server:
        print(f"Starting Weather Tool Server on port {args.port}")
        await serve_tool_server(server, port=args.port)
    else:
        # Direct test (async)
        if not args.location:
            print("Error: --location is required when not in server mode")
            return
            
        from unittest.mock import AsyncMock
        mock_status = AsyncMock()
        
        params = {
            "location": args.location,
            "days": args.days,
            "units": args.units,
            "include_marine": args.include_marine,
            "summary_format": args.summary_format,
            "include_radiation": args.include_radiation,
            "_status": mock_status,
        }
        if args.source:
            params["source"] = args.source
            
        result = await server.call("forecast", params)
        print(f"Weather for {args.location}:")
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    """Synchronous main for backward compatibility with tests."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # For tests, print a concise summary showing that the parser accepted the args.
    summary: dict[str, Any] = {
        "description": "Weather Tool Server",
        "location": args.location,
        "source": args.source,
        "days": args.days,
        "units": args.units,
        "include_marine": args.include_marine,
        "server_mode": args.server,
        "port": args.port,
    }

    print("Weather Tool Server")
    print(json.dumps(summary))


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    cli_main()
