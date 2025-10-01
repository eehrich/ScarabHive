from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig


class TwitterSearchServer(SchemaBasedMCPServer):
    """Twitter search plugin using modern MCPServer pattern."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract SSL verification setting from system config if available
        self.ssl_verify = getattr(system_config, 'ssl_verify', True)
    
    async def search_tweets(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Search recent tweets.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        
        Note: Twitter/X search is now heavily restricted and requires official API access.
        This returns a helpful message with alternatives.
        """
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



