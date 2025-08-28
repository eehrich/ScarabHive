#!/usr/bin/env python3
"""Web Scraper plugin CLI shim (lazy imports)."""
import argparse
import asyncio


def main():
    parser = argparse.ArgumentParser(description="Web Scraper MCP Server")
    parser.add_argument("--url", default="https://example.com", help="URL to scrape for testing")
    parser.add_argument("--max-chars", type=int, default=1000, help="Maximum characters to return")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9003, help="Server port")
    args = parser.parse_args()

    # Lazy imports
    from .plugin import PLUGIN_FACTORY
    from agent_system.http_server import serve_mcp_server

    server = PLUGIN_FACTORY("web_scraper", {})

    if args.server:
        print(f"Starting Web Scraper MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        # Direct test (async)
        asyncio.run(test_scraper(server, args))


async def test_scraper(server, args):
    result = await server.call("fetch", {
        "url": args.url,
        "max_chars": args.max_chars,
        "timeout": 10
    })

    print(f"URL: {result.get('url')}")
    print(f"Status: {result.get('status_code')}")
    print(f"Title: {result.get('title', 'N/A')}")
    print(f"Text length: {len(result.get('text', ''))}")
    print("=" * 50)
    print("Cleaned text preview:")
    print("=" * 50)
    text = result.get('text', '')
    if text:
        preview = text[:2000]
        print(repr(preview))
        print("\n" + "=" * 50)
        print("Formatted output:")
        print("=" * 50)
        print(preview)
    else:
        print("No text extracted")


if __name__ == "__main__":
    main()
