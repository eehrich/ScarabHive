#!/usr/bin/env python3
"""Weather MCP Server main entry point."""

import asyncio
import argparse
from .server import WeatherServer

async def main():
    parser = argparse.ArgumentParser(description="Weather MCP Server")
    parser.add_argument("--location", default="Berlin, Germany", help="Location for weather forecast")
    parser.add_argument("--source", default="wttr.in", choices=["wttr.in", "weather.gov", "met.no"], help="Weather data source")
    parser.add_argument("--days", type=int, default=3, help="Number of forecast days")
    args = parser.parse_args()
    
    server = WeatherServer("weather")
    try:
        result = await server.call("forecast", {
            "location": args.location,
            "source": args.source,
            "days": args.days
        })
        print(f"Weather forecast for {args.location}:")
        print(result)
    except Exception as e:
        print(f"Error: {e}")

def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(main())

if __name__ == "__main__":
    cli_main()
