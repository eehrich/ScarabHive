#!/usr/bin/env python3
"""DuckDuckGo Search MCP Server main entry point."""

import asyncio
import argparse
from .server import DuckDuckGoSearchServer
from ..http_server import serve_mcp_server


async def main():
    parser = argparse.ArgumentParser(description="DuckDuckGo Search MCP Server")
    parser.add_argument("--query", default="Python programming", help="Search query")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum number of results")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9001, help="Server port")
    args = parser.parse_args()
    
    server = DuckDuckGoSearchServer("duckduckgo_search")
    
    if args.server:
        print(f"Starting DuckDuckGo Search MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            result = await server.call("search", {
                "query": args.query,
                "max_results": args.max_results
            })
            print(f"Search results for '{args.query}':")
            if isinstance(result, dict) and "results" in result:
                results = result["results"]
                if not results:
                    # Handle empty results gracefully
                    if "error" in result:
                        print(f"No results found: {result['error']}")
                        if "suggestion" in result:
                            print(f"Suggestion: {result['suggestion']}")
                    else:
                        print("No results found for this query.")
                else:
                    for i, item in enumerate(results, 1):
                        print(f"{i}. {item.get('title', 'No title')}")
                        print(f"   {item.get('href', item.get('url', 'No URL'))}")
                        print(f"   {item.get('body', item.get('snippet', 'No description'))}\n")
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")

def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(main())

if __name__ == "__main__":
    cli_main()
