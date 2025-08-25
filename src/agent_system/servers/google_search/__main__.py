#!/usr/bin/env python3
"""Google Search MCP Server main entry point."""

from __future__ import annotations

import asyncio
import argparse
from ..http_server import serve_mcp_server
from .server import GoogleSearchServer


async def async_main():
    parser = argparse.ArgumentParser(description="Google Search MCP Server")
    parser.add_argument("--query", default="Python programming", help="Search query")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum number of results")
    parser.add_argument("--api-key", help="Google API key (or set GOOGLE_API_KEY env var)")
    parser.add_argument("--cx", help="Google Custom Search Engine ID (or set GOOGLE_CX env var)")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9006, help="Server port")
    args = parser.parse_args()
    
    # Create server config
    config = {}
    if args.api_key:
        config["api_key"] = args.api_key
    if args.cx:
        config["cx"] = args.cx
    
    server = GoogleSearchServer("google_search", config)
    
    if args.server:
        print(f"Starting Google Search MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            result = await server.call("search", {
                "query": args.query,
                "max_results": args.max_results
            })
            print(f"Google search results for '{args.query}':")
            if isinstance(result, dict) and "results" in result:
                for i, item in enumerate(result["results"], 1):
                    print(f"{i}. {item.get('title', 'No title')}")
                    print(f"   {item.get('url', 'No URL')}")
                    print(f"   {item.get('snippet', 'No description')}\n")
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")
            print("Note: Google Search requires API_KEY and CX environment variables or command line arguments")

def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(async_main())

# Keep old function for compatibility
def main() -> None:
    serve_mcp_server(GoogleSearchServer("google_search"))

if __name__ == "__main__":
    cli_main()
