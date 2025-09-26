# Twitter Search Plugin

The Twitter Search plugin enables searching recent tweets by query terms and returns structured tweet metadata. It provides access to public Twitter content for research, monitoring, and analysis purposes.

## Overview

This plugin searches Twitter/X for recent tweets matching specified query terms. It returns structured data including tweet content, author information, engagement metrics, and metadata, making it useful for social media research, trend analysis, and content monitoring.

## Features

### Core Operations
- **Tweet Search**: Search recent tweets by keywords and phrases
- **Structured Data**: Returns organized tweet metadata
- **Engagement Metrics**: Likes, retweets, replies, and quote tweets
- **Author Information**: User profiles and verification status
- **Flexible Limits**: Configurable result counts (1-50 tweets)
- **Recent Content**: Focus on recent and trending tweets

### Data Provided
- **Tweet Content**: Full text and media information
- **Author Details**: Username, display name, verification status
- **Engagement**: Like, retweet, reply, and quote counts
- **Timestamps**: Tweet creation and last updated times
- **Metadata**: Tweet ID, source, language, and more

## Configuration

Configure the Twitter Search plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - twitter_search

servers:
  twitter_search:
    type: twitter_search
    # Optional configuration
    # default_limit: 10
    # timeout: 20
```

### Environment Variables
- `TWITTER_API_KEY`: Twitter API key (if using official API)
- `TWITTER_BEARER_TOKEN`: Bearer token for Twitter API v2
- `TWITTER_TIMEOUT`: Request timeout (default: 20 seconds)

## Usage Examples

### Basic Tweet Search
```python
# Simple keyword search
{
  "action": "search",
  "query": "artificial intelligence",
  "limit": 10
}

# Hashtag search
{
  "action": "search",
  "query": "#MachineLearning",
  "max_results": 15
}
```

### Advanced Search Queries
```python
# Multiple keywords
{
  "action": "search",
  "query": "python programming tutorial",
  "limit": 20
}

# Specific user mentions
{
  "action": "search",
  "query": "@openai GPT-5",
  "max_results": 25
}

# Trending topics
{
  "action": "search",
  "query": "#AI2025 OR #ArtificialIntelligence",
  "limit": 30
}
```

### Research and Monitoring
```python
# Brand monitoring
{
  "action": "search",
  "query": "your-brand-name",
  "limit": 50
}

# News tracking
{
  "action": "search", 
  "query": "breaking news technology",
  "max_results": 20
}

# Event monitoring
{
  "action": "search",
  "query": "#ConferenceName",
  "limit": 40
}
```

## API Reference

### Parameters

- **action** (required): Search operation
  - `search` - Search recent tweets

- **query** (required): Search query for tweets
  - Keywords, hashtags, mentions, phrases
  - Boolean operators: AND, OR, NOT
  - Exact phrases with quotes

- **limit**: Maximum tweets to return
  - Range: 1-50 (default: 10)
  - Also accepts `max_results` parameter

- **max_results**: Alternative limit parameter
  - Same function as `limit`
  - Range: 1-50 (default: 10)

### Response Format

```json
{
  "query": "artificial intelligence",
  "tweets": [
    {
      "id": "1234567890123456789",
      "text": "Exciting developments in artificial intelligence are changing how we work and live. The future is here! #AI",
      "author": {
        "username": "tech_expert",
        "display_name": "Tech Expert",
        "verified": true,
        "followers_count": 15420
      },
      "metrics": {
        "likes": 142,
        "retweets": 38,
        "replies": 12,
        "quotes": 5
      },
      "created_at": "2025-01-15T14:30:00Z",
      "lang": "en",
      "source": "Twitter Web App",
      "urls": [
        "https://example.com/ai-article"
      ],
      "hashtags": ["AI"],
      "mentions": []
    }
  ],
  "total_results": 10,
  "search_time": 1.23
}
```

## Search Query Syntax

### Basic Operators
- **Keywords**: `artificial intelligence`
- **Hashtags**: `#MachineLearning #AI`
- **Mentions**: `@username`
- **Exact Phrases**: `"exact phrase here"`

### Advanced Operators
- **Boolean AND**: `python AND machine learning`
- **Boolean OR**: `AI OR "artificial intelligence"`
- **Boolean NOT**: `AI NOT bitcoin`
- **Grouping**: `(python OR java) AND programming`

### Special Queries
- **From User**: `from:username`
- **To User**: `to:username`
- **Replies**: `@username`
- **Links**: `filter:links`
- **Media**: `filter:media`

## Content Types

### Included Content
- **Original Tweets**: User-generated content
- **Replies**: Responses to other tweets (configurable)
- **Retweets**: Shared content (configurable)
- **Quote Tweets**: Retweets with added commentary
- **Media Tweets**: Tweets with images, videos, GIFs

### Metadata Available
- **Engagement Data**: Likes, retweets, replies, quotes
- **Author Information**: Profile data and verification
- **Timing Data**: Creation and update timestamps
- **Content Analysis**: Language detection, source app
- **Link Extraction**: URLs, media, hashtags, mentions

## Rate Limits and Ethics

### Usage Guidelines
- **Respectful Limits**: Don't overwhelm the service
- **Public Content Only**: Only accesses publicly available tweets
- **No Personal Data**: Avoid collecting private information
- **Terms Compliance**: Follow Twitter's Terms of Service

### Rate Limiting
- **Built-in Delays**: Plugin includes appropriate request spacing
- **Error Handling**: Graceful handling of rate limit responses
- **Retry Logic**: Automatic retry with exponential backoff
- **Fair Use**: Designed for reasonable research and monitoring

## Privacy and Legal Considerations

### Public Data Only
- **Public Tweets**: Only searches publicly visible content
- **No Private Messages**: Cannot access DMs or private accounts
- **Respect Privacy**: Consider user intent and context
- **Attribution**: Maintain proper attribution when using content

### Compliance
- **Terms of Service**: Follow Twitter/X Terms of Service
- **Academic Use**: Suitable for research and analysis
- **Commercial Use**: Review licensing for commercial applications
- **Data Retention**: Consider data retention policies

## Troubleshooting

### Common Issues

1. **No Results Found**
   - Try broader search terms
   - Check if query uses correct syntax
   - Verify hashtags and mentions are spelled correctly
   - Consider that recent content may be limited

2. **Rate Limited**
   - Wait before making additional requests
   - Reduce search frequency
   - Consider using smaller result limits
   - Plugin will automatically retry after delays

3. **Incomplete Data**
   - Some tweets may have limited metadata
   - Private accounts won't appear in results
   - Deleted tweets won't be returned
   - Very recent tweets may take time to index

### Error Handling
- **Network Errors**: Automatic retry with backoff
- **Invalid Queries**: Clear error messages with suggestions
- **API Limits**: Graceful degradation and retry logic
- **Content Filtering**: Handles restricted or removed content

## Use Cases

### Research Applications
- **Academic Research**: Social media analysis and studies
- **Market Research**: Brand sentiment and consumer insights
- **Trend Analysis**: Identifying emerging topics and discussions
- **News Monitoring**: Tracking breaking news and developments

### Business Applications
- **Brand Monitoring**: Track mentions and sentiment
- **Customer Service**: Find customer complaints and feedback
- **Competitive Analysis**: Monitor competitor discussions
- **Influencer Research**: Identify key voices in your industry

### Content Strategy
- **Hashtag Research**: Find effective hashtags for your content
- **Audience Insights**: Understand your audience's interests
- **Timing Analysis**: Identify optimal posting times
- **Content Ideas**: Discover trending topics and discussions