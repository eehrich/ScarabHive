from __future__ import annotations

from typing import Any
from pathlib import Path

from agent_system.mcp.base import MCPServer


class TwitterSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool != "search_tweets":
            return {"error": f"Unknown tool: {tool}. Only 'search_tweets' supported."}
            
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

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tools from schema.yaml - Multi-Tool format."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema_data = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema_data:
            raise RuntimeError("Missing required schema.yaml for twitter_search plugin")
        
        if 'tools' in schema_data:
            return schema_data['tools']
        else:
            raise RuntimeError("Twitter Search plugin must use Multi-Tool format with 'tools' array")

