#!/usr/bin/env python3
"""Weather MCP Server main entry point."""

import asyncio
import argparse
from .server import WeatherServer
from ..http_server import serve_mcp_server

async def main():
    parser = argparse.ArgumentParser(description="Weather MCP Server")
    parser.add_argument("--location", default="Berlin, Germany", help="Location for weather forecast")
    parser.add_argument("--source", default="wttr.in", choices=["wttr.in", "weather.gov", "met.no"], help="Weather data source")
    parser.add_argument("--days", type=int, default=3, help="Number of forecast days")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9000, help="Server port")
    args = parser.parse_args()
    
    server = WeatherServer("weather")
    
    if args.server:
        print(f"Starting Weather MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
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
