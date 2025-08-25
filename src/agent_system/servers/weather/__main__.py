#!/usr/bin/env python3
"""Weather MCP Server main entry point."""

import asyncio
from .server import WeatherServer

async def main():
    server = WeatherServer("weather")
    # Example usage
    result = await server.call("forecast", {
        "location": "Berlin, Germany",
        "source": "wttr.in"
    })
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
