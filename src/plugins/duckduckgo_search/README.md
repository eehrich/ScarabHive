# DuckDuckGo Search Plugin

The DuckDuckGo Search plugin provides privacy-focused web search capabilities using the DuckDuckGo search engine. It returns search results with titles, URLs, and snippets without tracking or storing user queries.

## Overview

This plugin leverages DuckDuckGo's search API to perform web searches while maintaining user privacy. DuckDuckGo is known for not tracking users, not storing personal information, and providing unbiased search results.

## Features

### Core Operations
- **Web Search**: Search the web using DuckDuckGo
- **Privacy-Focused**: No user tracking or data collection
- **Clean Results**: Titles, URLs, and content snippets
- **Configurable Results**: 1-10 results per search
- **Fast Response**: Optimized for quick search results

### Privacy Benefits
- **No Tracking**: DuckDuckGo doesn't track users
- **No Personal Profiles**: No user data storage
- **No Filter Bubble**: Unbiased search results
- **Secure**: HTTPS-only connections

## Configuration

Configure the DuckDuckGo Search plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - duckduckgo_search

servers:
  duckduckgo_search:
    type: duckduckgo_search
    # Optional configuration
    # timeout: 15
    # default_max_results: 5
    # cache_enabled: true
    # cache_ttl: 900  # 15 minutes
```

### Configuration Options
- **timeout**: Request timeout in seconds (default: 15)
- **default_max_results**: Default maximum search results (default: 5)
- **cache_enabled**: Enable/disable caching (default: true)
- **cache_ttl**: Cache lifetime in seconds (default: 900 = 15 minutes)

### Environment Variables
- `DUCKDUCKGO_TIMEOUT`: Request timeout (default: 15 seconds)
- `DUCKDUCKGO_MAX_RESULTS`: Default maximum results (default: 5)

## Usage Examples

### Basic Web Search
```python
# Simple search query
{
  "action": "search",
  "query": "artificial intelligence news 2025"
}

# Search with specific result count
{
  "action": "search", 
  "query": "python programming tutorial",
  "max_results": 8
}
```

### Specific Searches
```python
# Technical search
{
  "action": "search",
  "query": "machine learning algorithms comparison",
  "max_results": 10
}

# News search
{
  "action": "search",
  "query": "latest technology news today",
  "max_results": 5
}

# Research query
{
  "action": "search",
  "query": "climate change research papers 2024",
  "max_results": 7
}
```

## API Reference

### Parameters

- **action** (required): Search operation
  - `search` - Perform web search

- **query** (required): Search query terms
  - Natural language queries supported
  - Boolean operators: AND, OR, NOT
  - Quotes for exact phrases: "exact phrase"

- **max_results**: Maximum number of search results
  - Range: 1-10 (default: 5)
  - Higher values may increase response time

### Response Format

```json
{
  "query": "artificial intelligence",
  "results": [
    {
      "title": "Introduction to Artificial Intelligence",
      "url": "https://example.com/ai-intro",
      "snippet": "A comprehensive guide to understanding artificial intelligence, its applications, and future implications...",
      "position": 1
    },
    {
      "title": "AI News and Updates",
      "url": "https://example.com/ai-news", 
      "snippet": "Latest developments in AI technology, research breakthroughs, and industry applications...",
      "position": 2
    }
  ],
  "total_results": 5,
  "search_time": 0.45
}
```

## Performance Features

### Intelligent Caching
- **Automatic Caching**: Search results cached in `data/cache/duckduckgo_search/` directory  
- **TTL-Based Expiration**: Default 15-minute cache lifetime (configurable)
- **Cache Keys**: Based on search query and max_results parameter
- **Performance Boost**: Avoid repeated API calls for identical searches
- **Fresh Results**: Shorter TTL ensures reasonably current search results

### Rate Limiting Protection
- **Built-in Delays**: Automatic spacing between requests
- **Retry Logic**: Handles temporary rate limits gracefully
- **Error Recovery**: Fallback mechanisms for API issues

## Search Tips

### Query Optimization
1. **Specific Terms**: Use specific keywords for better results
2. **Exact Phrases**: Use quotes for exact phrase matching
3. **Boolean Logic**: Combine terms with AND, OR, NOT
4. **Recent Results**: Add year/date terms for current information

### Example Queries
- `"machine learning" AND python tutorial`
- `climate change NOT opinion`
- `AI OR "artificial intelligence" applications`
- `javascript frameworks 2025`

### Search Modifiers
- **Site-specific**: `site:reddit.com artificial intelligence`
- **File types**: `filetype:pdf machine learning`
- **Exact match**: `"exact phrase here"`
- **Exclude terms**: `python -java programming`

## Result Quality

### What DuckDuckGo Provides
- **Unbiased Results**: No personalized filtering
- **Global Perspective**: Results from worldwide sources
- **Privacy-Preserved**: No tracking influences results
- **Spam-Filtered**: Automatic low-quality content filtering

### Result Ranking
DuckDuckGo uses:
- **Relevance**: Content matching to query terms
- **Authority**: Source credibility and trustworthiness  
- **Freshness**: Recency for time-sensitive queries
- **User Signals**: Aggregate user behavior (anonymized)

## Performance Considerations

### Response Times
- **Fast Queries**: Simple searches typically < 1 second
- **Complex Queries**: Detailed searches may take 2-3 seconds
- **Network Dependent**: Speed varies with connection quality
- **Result Count Impact**: More results = longer response time

### Rate Limiting
- **Fair Use**: DuckDuckGo expects reasonable request rates
- **Built-in Delays**: Plugin includes appropriate delays between requests
- **Error Handling**: Graceful degradation if limits exceeded

## Privacy Features

### No Data Collection
- **No Query Storage**: Search terms not stored or tracked
- **No User Profiles**: No personal information collected
- **No Behavioral Tracking**: Browsing patterns not monitored
- **No Targeted Ads**: Search results not influenced by personal data

### Security
- **HTTPS Only**: All connections encrypted
- **No Third-Party Trackers**: Clean search environment
- **Tor Compatible**: Works with privacy networks
- **No Cookies**: Minimal cookie usage

## Troubleshooting

### Common Issues

1. **No Results Found**
   - Try broader search terms
   - Check spelling of query terms
   - Remove overly specific modifiers
   - Try alternative phrasings

2. **Slow Response Times**
   - Check network connectivity
   - Reduce max_results parameter
   - Simplify search query
   - Try again if DuckDuckGo is experiencing high load

3. **Unexpected Results**
   - Add more specific keywords
   - Use quotes for exact phrases
   - Add date/year terms for current information
   - Try different query formulations

### Error Messages
- **Rate Limited**: Wait before making additional requests
- **Network Error**: Check internet connectivity
- **Invalid Query**: Ensure query is not empty or malformed

## Comparison with Other Search Engines

### DuckDuckGo Advantages
- **Privacy**: No tracking or data collection
- **Unbiased**: No personalized filter bubbles
- **Clean**: No ads mixed in organic results
- **Global**: Not region-specific biased

### Considerations
- **Result Volume**: May have fewer results than Google
- **Personalization**: No personalized results (pro and con)
- **Local Results**: Limited local business information
- **Real-time Data**: May be less current than some alternatives