# Twitter Search Plugin

Search recent tweets using the official Twitter API v2 via tweepy. This plugin provides access to public Twitter content for research, monitoring, and analysis.

## Overview

This plugin uses the **official Twitter API v2** through the `tweepy` Python library to search for recent tweets. It requires free API credentials from Twitter but provides reliable, legal access to Twitter data.

## Features

- **Official API Access**: Uses Twitter API v2 (legal and stable)
- **Recent Tweets**: Search tweets from the last 7 days (free tier)
- **Rich Metadata**: Author info, engagement metrics, timestamps
- **Rate Limit Friendly**: Automatically waits when rate limited
- **Free Tier Support**: Works with free Twitter API access
- **No Scraping**: Legal alternative to broken scrapers (snscrape, twint)

- **No Scraping**: Legal alternative to broken scrapers (snscrape, twint)

## Setup Instructions

### 1. Install Dependencies

```bash
pip install tweepy
# or from plugin directory:
pip install -r requirements.txt
```

### 2. Get Free Twitter API Credentials

1. **Create Twitter Developer Account**
   - Go to https://developer.twitter.com/en/portal/dashboard
   - Sign up for free developer access
   - Fill out the application (usually approved instantly for basic use)

2. **Create an App**
   - In the Developer Portal, create a new "Project" and "App"
   - Give it a name (e.g., "AgentSystem Research")

3. **Generate Bearer Token**
   - Navigate to your App → "Keys and tokens"
   - Click "Generate" under "Bearer Token"
   - **Copy the token immediately** (you won't see it again!)

4. **Set Environment Variable**
   ```bash
   # Linux/Mac:
   export TWITTER_BEARER_TOKEN="your_bearer_token_here"
   
   # Windows (PowerShell):
   $env:TWITTER_BEARER_TOKEN="your_bearer_token_here"
   
   # Or add to .env file:
   TWITTER_BEARER_TOKEN=your_bearer_token_here
   ```

### 3. Verify Setup

The plugin will log on startup:
- ✅ "Twitter API v2 client initialized successfully" - Ready to use
- ⚠️ "TWITTER_BEARER_TOKEN not found" - Setup needed

## Free Tier Limits (2025)

**Twitter API v2 Free Tier:**
- 📊 **10,000 tweet reads** per month
- 📝 **1,500 tweet posts** per month  
- 📅 **Last 7 days** of tweets only (no historical data)
- 🔢 **100 tweets** maximum per request
- ⏱️ **Rate limits** enforced (plugin auto-waits)

**Good for:**
- Research and analysis
- Trend monitoring
- Brand mentions tracking
- Academic projects

**Not sufficient for:**
- Large-scale data mining (need paid tier)
- Historical analysis beyond 7 days
- High-frequency monitoring

## Configuration

## Configuration

Add to `config/plugins.yaml`:

```yaml
twitter_search:
  plugin: twitter_search
  enabled: true
  # No additional config needed - uses environment variable TWITTER_BEARER_TOKEN
```

## Usage Examples

### Basic Tweet Search

```python
{
  "query": "artificial intelligence",
  "limit": 10
}
```

**Response:**
```json
{
  "status": "success",
  "query": "artificial intelligence",
  "tweets": [
    {
      "id": "1234567890",
      "text": "Exciting AI developments...",
      "created_at": "2025-11-05T10:30:00",
      "lang": "en",
      "source": "Twitter Web App",
      "author": {
        "username": "tech_expert",
        "name": "Tech Expert",
        "verified": true,
        "followers_count": 15420
      },
      "metrics": {
        "likes": 142,
        "retweets": 38,
        "replies": 12,
        "quotes": 5
      }
    }
  ],
  "total_results": 10
}
```

### Hashtag Search

```python
{
  "query": "#Python OR #MachineLearning",
  "limit": 20
}
```

### User Mentions

```python
{
  "query": "@openai GPT",
  "limit": 15
}
```

### Advanced Query Syntax

```python
# Multiple keywords (AND)
{"query": "python programming tutorial"}

# Boolean OR
{"query": "#AI OR artificial intelligence"}

# Exclude terms
{"query": "bitcoin -crypto"}

# From specific user
{"query": "from:elonmusk"}

# With links
{"query": "machine learning filter:links"}
```

## Response Formats

### Success Response

```json
{
  "status": "success",
  "query": "search term",
  "tweets": [...],
  "total_results": 10,
  "api_info": {
    "version": "Twitter API v2",
    "search_window": "Last 7 days (free tier)",
    "rate_limit_friendly": "Auto-waits on rate limits"
  }
}
```

### Setup Required Response

When `TWITTER_BEARER_TOKEN` is not set:

```json
{
  "status": "setup_required",
  "query": "search term",
  "message": "Twitter API credentials not configured",
  "setup_instructions": {
    "step_1": "Go to https://developer.twitter.com/en/portal/dashboard",
    "step_2": "Create a free developer account",
    "step_3": "Create a new App",
    "step_4": "Generate Bearer Token",
    "step_5": "Set environment variable: TWITTER_BEARER_TOKEN=your_token"
  },
  "free_tier_limits": {
    "tweet_reads": "10,000 per month",
    "search_window": "Last 7 days only"
  }
}
```

### Error Response

```json
{
  "status": "error",
  "query": "search term",
  "error": "Error message",
  "error_type": "TweepyException",
  "message": "Twitter API request failed..."
}
```

## Tweet Data Fields

Each tweet includes:

- **id**: Unique tweet ID
- **text**: Full tweet content
- **created_at**: ISO timestamp
- **lang**: Language code (e.g., "en")
- **source**: Platform used (e.g., "Twitter Web App")
- **author**: User information
  - `username`: Twitter handle
  - `name`: Display name
  - `verified`: Verification status
  - `followers_count`: Follower count
- **metrics**: Engagement stats
  - `likes`: Like count
  - `retweets`: Retweet count
  - `replies`: Reply count
  - `quotes`: Quote tweet count

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