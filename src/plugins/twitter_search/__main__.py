#!/usr/bin/env python3
"""Twitter Search MCP Server plugin CLI shim."""

import asyncio
import argparse


async def main():
    parser = argparse.ArgumentParser(description="Twitter Search MCP Server")
    parser.add_argument("--query", default="Python", help="Search query")
    parser.add_argument("--max-results", type=int, default=10, help="Maximum number of tweets")
    parser.add_argument("--lang", default="en", help="Language filter (e.g., en, de, fr)")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9004, help="Server port")
    args = parser.parse_args()

    # Lazy import to avoid importing server code on plain `import plugins.twitter_search`
    from .server import TwitterSearchServer
    from agent_system.servers.http_server import serve_mcp_server

    server = TwitterSearchServer("twitter_search")

    if args.server:
        print(f"Starting Twitter Search MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            result = await server.call("search", {
                "query": args.query,
                "max_results": args.max_results,
                "lang": args.lang
            })
            print(f"Twitter search results for '{args.query}':")
            if isinstance(result, dict) and "tweets" in result:
                for i, tweet in enumerate(result["tweets"], 1):
                    print(f"{i}. @{tweet.get('username', 'unknown')}: {tweet.get('text', 'No text')[:100]}...")
                    print(f"   Date: {tweet.get('date', 'Unknown')}")
                    print(f"   URL: {tweet.get('url', 'No URL')}\n")
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")


def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
