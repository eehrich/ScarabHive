#!/usr/bin/env python3
"""Web Scraper MCP Server main entry point."""

import asyncio
import argparse
from .server import WebScraperServer
from ..http_server import serve_mcp_server


def main():
    """Synchronous main function that handles both server and test modes."""
    parser = argparse.ArgumentParser(description="Web Scraper MCP Server")
    parser.add_argument("--url", default="https://example.com", help="URL to scrape for testing")
    parser.add_argument("--max-chars", type=int, default=1000, help="Maximum characters to return")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9003, help="Server port")
    args = parser.parse_args()
    
    server = WebScraperServer("web_scraper")
    
    if args.server:
        print(f"Starting Web Scraper MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        # Direct test (async)
        asyncio.run(test_scraper(server, args))


async def test_scraper(server, args):
    """Test the scraper directly."""
    result = await server.call("fetch", {
        "url": args.url,
        "max_chars": args.max_chars,
        "timeout": 10
    })
    
    print(f"URL: {result['url']}")
    print(f"Status: {result['status_code']}")
    print(f"Title: {result.get('title', 'N/A')}")
    print(f"Text length: {len(result.get('text', ''))}")
    print("=" * 50)
    print("Cleaned text preview:")
    print("=" * 50)
    text = result.get('text', '')
    if text:
        # Show first 2000 chars to see the cleaning in action
        preview = text[:2000]
        print(repr(preview))  # Use repr to show actual newlines
        print("\n" + "=" * 50)
        print("Formatted output:")
        print("=" * 50)
        print(preview)
    else:
        print("No text extracted")


if __name__ == "__main__":
    main()
