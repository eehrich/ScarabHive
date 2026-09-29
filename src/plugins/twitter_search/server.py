from __future__ import annotations

import logging
import os
from typing import Any, TYPE_CHECKING

try:
    import tweepy
    TWEEPY_AVAILABLE = True
except ImportError:
    TWEEPY_AVAILABLE = False

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class TwitterSearchServer(SchemaBasedToolServer):
    """Twitter search plugin using Twitter API v2 via tweepy."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)
        
        # Extract SSL verification setting from system config if available
        self.ssl_verify = getattr(system_config, 'ssl_verify', True)
        
        # Initialize Twitter API client
        self.client = None
        self._setup_twitter_client()
    
    def _setup_twitter_client(self) -> None:
        """Setup Twitter API v2 client using environment variables."""
        if not TWEEPY_AVAILABLE:
            logger.warning("tweepy not installed - Twitter search will not work. Install with: pip install tweepy")
            return
        
        # Try to get API credentials from environment
        bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
        
        if bearer_token:
            try:
                self.client = tweepy.Client(
                    bearer_token=bearer_token,
                    wait_on_rate_limit=True  # Auto-wait when rate limited
                )
                logger.info("Twitter API v2 client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Twitter client: {e}")
                self.client = None
        else:
            logger.warning(
                "TWITTER_BEARER_TOKEN not found in environment. "
                "Twitter search will return a setup guide. "
                "Get free API access at https://developer.twitter.com/en/portal/dashboard"
            )
    
    async def tweets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search tweets by query (tool method).
        
        Tool name: {{ name }}_tweets → Method: tweets (after stripping {{ name }}_ prefix)
        """
        query = params.get("query", "")
        limit = params.get("limit", 10)
        
        # Get status object (mandatory from framework)
        status = params["_status"]
        
        # Check for cancellation before starting
        cancellation_token = params.get("_cancellation_token")
        if cancellation_token and cancellation_token.is_cancelled:
            return {"error": f"Twitter search for '{query}' cancelled by user", "cancelled": True}
        
        # Check if tweepy is available
        if not TWEEPY_AVAILABLE:
            error_msg = "tweepy library not installed"
            await status.error(error_msg)
            return {
                "status": "error",
                "error": error_msg,
                "message": "Install tweepy to use Twitter search: pip install tweepy",
                "query": query
            }
        
        # Check if client is initialized
        if not self.client:
            await status.error("Twitter API credentials not configured", meta={"query": query})
            return {
                "status": "setup_required",
                "query": query,
                "message": "Twitter API credentials not configured",
                "setup_instructions": {
                    "step_1": "Go to https://developer.twitter.com/en/portal/dashboard",
                    "step_2": "Create a free developer account (if you don't have one)",
                    "step_3": "Create a new App or use existing App",
                    "step_4": "Navigate to your App -> Keys and tokens",
                    "step_5": "Generate Bearer Token",
                    "step_6": "Set environment variable: TWITTER_BEARER_TOKEN=your_token_here"
                },
                "free_tier_limits": {
                    "tweet_reads": "10,000 per month",
                    "search_window": "Last 7 days only",
                    "max_results_per_request": 100
                },
                "alternatives": [
                    "yahoo_finance - for stock data and financial news",
                    "web_scraper - for scraping news websites"
                ]
            }
        
        await status.progress(f"Searching Twitter for: {query}")
        
        # Search tweets using Twitter API v2
        try:
            # Search recent tweets (last 7 days on free tier)
            tweets_response = self.client.search_recent_tweets(
                query=query,
                max_results=min(limit, 100),  # API limit is 100
                tweet_fields=['created_at', 'public_metrics', 'author_id', 'lang', 'source'],
                user_fields=['username', 'name', 'verified', 'public_metrics'],
                expansions=['author_id']
            )
            
            if not tweets_response.data:
                await status.end(f"No tweets found for query: {query}", meta={"total_results": 0})
                return {
                    "status": "success",
                    "query": query,
                    "tweets": [],
                    "total_results": 0,
                    "message": f"No tweets found for query: {query}"
                }
            
            # Build user lookup for author info
            users = {}
            if tweets_response.includes and 'users' in tweets_response.includes:
                for user in tweets_response.includes['users']:
                    users[user.id] = user
            
            # Format tweets
            formatted_tweets = []
            for tweet in tweets_response.data:
                author = users.get(tweet.author_id)
                
                tweet_data = {
                    "id": tweet.id,
                    "text": tweet.text,
                    "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                    "lang": tweet.lang,
                    "source": tweet.source
                }
                
                # Add author info if available
                if author:
                    tweet_data["author"] = {
                        "id": author.id,
                        "username": author.username,
                        "name": author.name,
                        "verified": getattr(author, 'verified', False),
                        "followers_count": author.public_metrics.get('followers_count', 0) if author.public_metrics else 0
                    }
                
                # Add engagement metrics
                if tweet.public_metrics:
                    tweet_data["metrics"] = {
                        "likes": tweet.public_metrics.get('like_count', 0),
                        "retweets": tweet.public_metrics.get('retweet_count', 0),
                        "replies": tweet.public_metrics.get('reply_count', 0),
                        "quotes": tweet.public_metrics.get('quote_count', 0)
                    }
                
                formatted_tweets.append(tweet_data)
            
            await status.end(
                f"Found {len(formatted_tweets)} tweets for: {query}",
                meta={
                    "query": query,
                    "total_results": len(formatted_tweets),
                    "api_version": "Twitter API v2"
                }
            )
            
            return {
                "status": "success",
                "query": query,
                "tweets": formatted_tweets,
                "total_results": len(formatted_tweets),
                "api_info": {
                    "version": "Twitter API v2",
                    "search_window": "Last 7 days (free tier)",
                    "rate_limit_friendly": "Auto-waits on rate limits"
                }
            }
            
        except tweepy.TweepyException as e:
            error_msg = f"Twitter API error: {str(e)}"
            logger.error(error_msg)
            await status.error(error_msg, meta={"error_type": type(e).__name__, "query": query})
            return {
                "status": "error",
                "query": query,
                "error": str(e),
                "error_type": type(e).__name__,
                "message": "Twitter API request failed. Check your credentials and rate limits."
            }
        except Exception as e:
            error_msg = f"Unexpected error in Twitter search: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await status.error(error_msg, meta={"query": query})
            return {
                "status": "error",
                "query": query,
                "error": str(e),
                "message": "An unexpected error occurred"
            }



