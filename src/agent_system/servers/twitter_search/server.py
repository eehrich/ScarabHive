from __future__ import annotations

from typing import Any

from ...mcp.base import MCPServer


class TwitterSearchServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "search":
            query = params.get("query", "")
            limit = int(params.get("limit", 10))
            results = []
            try:
                import snscrape.modules.twitter as sntwitter  # type: ignore
            except Exception as e:
                raise RuntimeError("snscrape is required for twitter_search. Install it via pip.") from e
            # snscrape uses HTTP clients internally; if enterprise SSL MITM causes issues, users may need to set
            # SSL_CERT_FILE/CURL_CA_BUNDLE or use corporate proxies. Proceed with default behavior here.
            for i, tweet in enumerate(sntwitter.TwitterSearchScraper(query).get_items()):
                results.append({
                    "date": str(tweet.date),
                    "user": str(tweet.user.username),
                    "content": tweet.rawContent,
                    "url": tweet.url,
                })
                if i + 1 >= limit:
                    break
            return {"engine": "twitter-scrape", "query": query, "results": results}
        raise ValueError(f"Unknown tool: {tool}")
