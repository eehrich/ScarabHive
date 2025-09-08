"""CLI entrypoint for the weather plugin: python -m plugins.weather

This provides a minimal, standalone CLI surface used by tests. It intentionally
does not start servers — it only validates and displays CLI arguments and help
text so test harnesses can import/execute the module.
"""
from __future__ import annotations

import argparse
import json
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugins.weather", description="Weather MCP Server")

    # Core weather parameters
    parser.add_argument("--location", help="Location name (city, address, coordinates)")
    parser.add_argument("--source", help="Weather data source (wttr.in, weather.gov, met.no, marine.weather.gov)")
    parser.add_argument("--days", type=int, default=3, help="Number of forecast days (1-7)")
    parser.add_argument("--units", choices=["metric", "imperial"], default="metric", help="Temperature units")
    parser.add_argument("--include-marine", action="store_true", help="Include marine data (SST, wave heights)")
    parser.add_argument("--summary-format", choices=["detailed", "daily_summary", "hourly"], default="detailed")
    parser.add_argument("--include-radiation", action="store_true", help="Include estimated solar radiation data")

    # Server mode options
    parser.add_argument("--server", action="store_true", help="Run in server mode (MCP server)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on when in server mode")

    # Misc
    parser.add_argument("--version", action="version", version="weather plugin 0.1.0")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    # For tests, print a concise summary showing that the parser accepted the args.
    summary: dict[str, Any] = {
        "description": "Weather MCP Server",
        "location": args.location,
        "source": args.source,
        "days": args.days,
        "units": args.units,
        "include_marine": args.include_marine,
        "server_mode": args.server,
        "port": args.port,
    }

    print("Weather MCP Server")
    print(json.dumps(summary))


def cli_main():
    main()


if __name__ == "__main__":
    cli_main()
