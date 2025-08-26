#!/usr/bin/env python3
"""WebResearchAgent main entry point for direct testing."""

import asyncio
import argparse
from .server import WebResearchAgent


async def main():
    parser = argparse.ArgumentParser(description="Web Research Agent")
    parser.add_argument("--query", default="artificial intelligence trends 2025", help="Research query")
    parser.add_argument("--action", default="research", choices=["research", "fact_check", "compare_sources"], help="Action to perform")
    parser.add_argument("--max-results", type=int, default=3, help="Maximum number of results")
    args = parser.parse_args()
    
    agent = WebResearchAgent("web_researcher_cli")
    
    try:
        if args.action == "research":
            print(f"Researching: {args.query}")
            result = await agent.call("research", {
                "topic": args.query,
                "max_results": args.max_results
            })
        elif args.action == "fact_check":
            print(f"Fact-checking: {args.query}")
            result = await agent.call("fact_check", {
                "claim": args.query
            })
        else:
            print(f"Invalid action: {args.action}")
            return
            
        print("\nResult:")
        if isinstance(result, dict):
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(result)
        else:
            print(result)
            
    except Exception as e:
        print(f"Error during research: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
