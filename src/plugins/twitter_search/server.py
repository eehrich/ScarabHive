from __future__ import annotations

from typing import Any

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
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Get information about Twitter/X search limitations and suggested alternatives for social media and stock trend analysis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["search"], "description": "Use 'search' to find tweets"},
                        "query": {"type": "string", "description": "Search terms for finding tweets"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10, "description": "Number of tweets to return"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10, "description": "Alternative name for limit"},
                    },
                    "required": ["query"],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for Twitter search."""
        return "search"

