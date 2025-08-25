#!/usr/bin/env python3
"""LLM Router MCP Server main entry point."""

import asyncio
import argparse
from .server import LLMRouterServer
from ..http_server import serve_mcp_server


async def main():
    parser = argparse.ArgumentParser(description="LLM Router MCP Server")
    parser.add_argument("--prompt", default="Hello, how are you?", help="Prompt to send to LLM")
    parser.add_argument("--provider", default="ollama", choices=["ollama", "openai"], help="LLM provider")
    parser.add_argument("--model", default="llama2", help="Model name")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=9005, help="Server port")
    args = parser.parse_args()
    
    server = LLMRouterServer("llm_router")
    
    if args.server:
        print(f"Starting LLM Router MCP Server on port {args.port}")
        serve_mcp_server(server, port=args.port)
    else:
        try:
            result = await server.call("chat", {
                "prompt": args.prompt,
                "provider": args.provider,
                "model": args.model
            })
            print(f"LLM Response ({args.provider}/{args.model}):")
            if isinstance(result, dict) and "response" in result:
                print(result["response"])
            else:
                print(result)
        except Exception as e:
            print(f"Error: {e}")

def cli_main():
    """Synchronous entry point for console script."""
    asyncio.run(main())

if __name__ == "__main__":
    cli_main()
