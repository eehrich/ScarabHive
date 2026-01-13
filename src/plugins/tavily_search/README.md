# Tavily Search Plugin

AI-powered web search and content extraction using the [Tavily API](https://tavily.com/).

## Features

- **Web Search**: AI-optimized search with better results for complex queries
- **Content Extraction**: Extract clean content from URLs with high success rate
- **Caching**: Built-in result caching (30 minutes default)
- **Filtering**: Domain include/exclude, time range, topic categories

## Configuration

Add to `config/plugins.yaml`:

```yaml
plugins:
  servers:
    tavily_search:
      type: tavily_search
      enabled: true
      api_key: ""  # Or use TAVILY_API_KEY env var (recommended)
      cache_ttl: 1800  # 30 minutes
      cache_enabled: true
      default_max_results: 5
      default_search_depth: "basic"  # "basic" or "advanced" (2x credits)
```

Or set the environment variable:
```bash
export TAVILY_API_KEY="tvly-your-api-key"
```

Get your API key at [app.tavily.com](https://app.tavily.com/home).

## Tools

### `tavily_search_web_search`

AI-powered web search with content snippets.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | ✅ | Search query (question or keywords) |
| `max_results` | int | | Number of results (1-20, default: 5) |
| `search_depth` | string | | `"basic"` (fast) or `"advanced"` (thorough, 2x credits) |
| `topic` | string | | `"general"`, `"news"`, or `"finance"` |
| `time_range` | string | | `"day"`, `"week"`, `"month"`, or `"year"` |
| `include_domains` | array | | Only include these domains |
| `exclude_domains` | array | | Exclude these domains |
| `include_raw_content` | bool | | Include full page content (markdown) |
| `include_answer` | bool | | Include AI-generated answer summary |
| `ignore_cache` | bool | | Bypass cache |

**Example:**
```json
{
  "query": "Python async programming best practices",
  "max_results": 5,
  "search_depth": "advanced",
  "include_answer": true
}
```

### `tavily_search_extract`

Extract clean content from URLs.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `urls` | array | ✅ | URLs to extract (max 20) |
| `extract_depth` | string | | `"basic"` or `"advanced"` (for complex pages) |
| `format` | string | | `"markdown"` (default) or `"text"` |
| `include_images` | bool | | Include image URLs |
| `ignore_cache` | bool | | Bypass cache |

**Example:**
```json
{
  "urls": ["https://en.wikipedia.org/wiki/Python_(programming_language)"],
  "extract_depth": "basic",
  "format": "markdown"
}
```

## Comparison with DuckDuckGo Search

| Feature | Tavily | DuckDuckGo |
|---------|--------|------------|
| Search quality for complex queries | Better (AI-powered) | Basic |
| Anti-scraping bypass | High success rate | Variable |
| Content extraction | Built-in extract tool | Requires web_scraper |
| Rate limits | Based on API plan | May get blocked |
| Cost | Paid API | Free |
| AI-generated answers | Yes | No |

**When to use Tavily:**
- Complex research queries requiring context understanding
- Extracting content from sites with anti-scraping measures
- Need for AI-generated answer summaries
- Higher reliability requirements

**When to use DuckDuckGo:**
- Simple keyword searches
- Free tier sufficient
- Don't need content extraction

## API Credits

- Basic search: 1 credit per request
- Advanced search: 2 credits per request
- Basic extract: 1 credit per 5 URLs
- Advanced extract: 2 credits per 5 URLs

Free tier includes 1,000 credits/month.

## Error Handling

The plugin handles common errors:
- Invalid API key → Clear error message
- Rate limit exceeded → Suggests waiting
- Extraction failures → Reports per-URL errors

## Testing

```bash
# Run plugin tests
.venv/Scripts/python.exe -m pytest tests/plugins/test_plugin_tavily_search.py -v
```
