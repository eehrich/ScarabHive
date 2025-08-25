#!/usr/bin/env python3
"""DateTime MCP Server main module."""

from .server import DateTimeServer

if __name__ == "__main__":
    import asyncio
    from ...servers.http_server import serve_mcp_server
    
    async def main():
        server = DateTimeServer()
        serve_mcp_server(server, port=9003)
    
    asyncio.run(main())
