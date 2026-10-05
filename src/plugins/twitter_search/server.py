from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import Any, TYPE_CHECKING

try:
    import requests  # tweepy's own transport
    import tweepy
    TWEEPY_AVAILABLE = True
except ImportError:
    TWEEPY_AVAILABLE = False

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

MAX_LIMIT = 50  # the schema's maximum for `limit`
# GET /2/tweets/search/recent refuses max_results below 10 (400 Bad Request);
# a smaller limit asks for 10 and cuts the answer.
API_MIN_RESULTS = 10
# tweepy's requests session has no timeout of its own: the client's session is
# given this one (connect and each read), so a stalled connection frees its
# worker thread. The call runs in a thread so the event loop stays free, and
# wait_for bounds the whole call with the same value as a backstop.
REQUEST_TIMEOUT = 30


def _failure_message(e: Exception) -> str:
    """The hint for the model, chosen by the exception's class."""
    if isinstance(e, tweepy.TooManyRequests):
        reset = getattr(getattr(e, "response", None), "headers", {}).get("x-rate-limit-reset")
        try:
            return f"X API rate limit reached; it resets in {max(int(reset) - int(time.time()), 0)} seconds."
        except (TypeError, ValueError):
            return "X API rate limit reached; try again later."
    if isinstance(e, tweepy.Unauthorized):
        return "X rejected the bearer token. Check TWITTER_BEARER_TOKEN."
    if isinstance(e, tweepy.Forbidden):
        return "X refused access: the app's plan, permissions or credits do not cover recent search."
    if isinstance(e, tweepy.BadRequest):
        return "X rejected the request, usually the query syntax."
    if isinstance(e, tweepy.TwitterServerError):
        return "X server error; try again later."
    return "Twitter API request failed."


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
                # No wait_on_rate_limit: tweepy would sleep up to 15 minutes
                # inside the call; a rate limit is answered as an error instead.
                self.client = tweepy.Client(bearer_token=bearer_token)
                self.client.session.request = functools.partial(
                    self.client.session.request, timeout=REQUEST_TIMEOUT)
                logger.info("Twitter API v2 client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Twitter client: {e}")
                self.client = None
        else:
            logger.warning(
                "TWITTER_BEARER_TOKEN not found in environment. "
                "Twitter search will return a setup guide. "
                "Get API access at https://developer.x.com/en/portal/dashboard"
            )

    async def tweets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Search tweets by query (tool method).

        Tool name: {{ name }}_tweets → Method: tweets (after stripping {{ name }}_ prefix)
        """
        raw_query = params.get("query")
        query = raw_query.strip() if isinstance(raw_query, str) else ""

        # Get status object (mandatory from framework)
        status = params["_status"]

        # Check for cancellation before starting
        cancellation_token = params.get("_cancellation_token")
        if cancellation_token and cancellation_token.is_cancelled:
            return {"error": f"Twitter search for '{query}' cancelled by user", "cancelled": True}

        if not query:
            return {"status": "error", "query": query, "error": "Empty query"}
        # The framework does not enforce the schema's 1..50.
        raw_limit = params.get("limit")
        try:
            limit = 10 if raw_limit is None else min(max(int(raw_limit), 1), MAX_LIMIT)
        except (TypeError, ValueError, OverflowError):
            return {"status": "error", "query": query,
                    "error": f"limit must be a whole number from 1 to {MAX_LIMIT}, got {raw_limit!r}"}

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
                    "step_1": "Go to https://developer.x.com/en/portal/dashboard",
                    "step_2": "Create a developer account (X API access is paid)",
                    "step_3": "Create a new App or use existing App",
                    "step_4": "Navigate to your App -> Keys and tokens",
                    "step_5": "Generate Bearer Token",
                    "step_6": "Set environment variable: TWITTER_BEARER_TOKEN=your_token_here"
                },
            }

        await status.progress(f"Searching Twitter for: {query}")

        # Search tweets using Twitter API v2
        try:
            # Recent search covers the last 7 days
            tweets_response = await asyncio.wait_for(asyncio.to_thread(
                self.client.search_recent_tweets,
                query=query,
                max_results=max(limit, API_MIN_RESULTS),
                tweet_fields=['created_at', 'public_metrics', 'author_id', 'lang', 'source'],
                user_fields=['username', 'name', 'verified', 'public_metrics'],
                expansions=['author_id']
            ), REQUEST_TIMEOUT)

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
            for tweet in tweets_response.data[:limit]:
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
                    "search_window": "Last 7 days",
                }
            }

        except (asyncio.TimeoutError, requests.Timeout):
            error_msg = f"X API did not answer within {REQUEST_TIMEOUT} seconds"
            logger.error(error_msg)
            await status.error(error_msg, meta={"query": query})
            return {"status": "error", "query": query, "error": error_msg,
                    "error_type": "TimeoutError", "message": "Try again later."}
        except tweepy.TweepyException as e:
            error_msg = f"Twitter API error: {str(e)}"
            logger.error(error_msg)
            await status.error(error_msg, meta={"error_type": type(e).__name__, "query": query})
            return {
                "status": "error",
                "query": query,
                "error": str(e),
                "error_type": type(e).__name__,
                "message": _failure_message(e)
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
