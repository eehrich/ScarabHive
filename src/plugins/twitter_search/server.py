from __future__ import annotations

from typing import Any
from pathlib import Path

from agent_system.mcp.base import MCPServer


class TwitterSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            
            # Twitter/X search is now heavily restricted and requires official API access
            # snscrape has compatibility issues with modern Python versions
            # Return a helpful message instead of failing
            return {
                "engine": "twitter-info",
                "query": query,
                "message": "Twitter/X search requires official API access. For stock trends, consider using:",
                "alternatives": [
                    "yahoo_finance tool for stock data and news",
                    "duckduckgo_search for recent stock mentions", 
                    "web_scraper for financial news websites",
                    "Use the official Twitter API with proper credentials"
                ],
                "suggestion": f"Try searching for '{query}' using duckduckgo_search or yahoo_finance instead"
            }
        raise ValueError(f"Unknown tool: {tool}")

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for Twitter search."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for twitter_search plugin")
        return schema

    def get_default_action(self) -> str:
        """Return the default action for Twitter search."""
        return "search"

