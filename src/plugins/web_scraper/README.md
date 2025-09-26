# Web Scraper Plugin

The Web Scraper plugin provides comprehensive web page scraping capabilities with structured data extraction. It fetches web pages, extracts text content, and provides enhanced parsing of tables, forms, lists, and links.

## Overview

This plugin enables reliable web content extraction for research, data collection, and content analysis. It includes advanced features for structured data parsing and handles various web page formats with built-in error handling and timeout management.

## Features

### Core Operations
- **Page Fetching**: Download and parse web pages
- **Link Extraction**: Extract anchor links with filtering options
- **Structured Data**: Parse tables, forms, and lists
- **Content Optimization**: Token-efficient text extraction
- **Error Handling**: Robust handling of network and parsing errors

### Advanced Parsing
- **Table Extraction**: Convert HTML tables to structured data
- **Form Analysis**: Extract form fields and structure
- **List Processing**: Parse ordered and unordered lists
- **Link Filtering**: Domain-specific and pattern-based filtering
- **Content Truncation**: Configurable text length limits

## Configuration

Configure the Web Scraper plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - web_scraper

servers:
  web_scraper:
    type: web_scraper
    proxies: []
    # Optional configuration
    # timeout: 20
    # max_chars: 8000
    # cache_enabled: true
    # cache_ttl: 1800  # 30 minutes
```

### Configuration Options
- **timeout**: Request timeout in seconds (default: 20)
- **max_chars**: Default text truncation limit (default: 8000)
- **cache_enabled**: Enable/disable caching (default: true)
- **cache_ttl**: Cache lifetime in seconds (default: 1800 = 30 minutes)
- **proxies**: List of proxy URLs for requests

### Environment Variables
- `WEB_SCRAPER_TIMEOUT`: Default request timeout (default: 20 seconds)
- `WEB_SCRAPER_USER_AGENT`: Custom User-Agent string
- `WEB_SCRAPER_MAX_CHARS`: Default text truncation limit

## Usage Examples

### Basic Page Scraping
```python
# Simple page fetch
{
  "action": "fetch",
  "url": "https://example.com/article"
}

# With custom timeout and truncation
{
  "action": "fetch",
  "url": "https://example.com/long-article",
  "timeout": 30,
  "max_chars": 5000
}
```

### Structured Data Extraction
```python
# Extract tables and forms
{
  "action": "fetch",
  "url": "https://example.com/data-page",
  "extract_tables": true,
  "extract_forms": true,
  "extract_lists": true
}

# Include raw HTML for analysis
{
  "action": "fetch",
  "url": "https://example.com/complex-page",
  "include_html": true,
  "max_chars": 0  // Unlimited text
}
```

### Link Extraction
```python
# Extract all links
{
  "action": "links",
  "url": "https://example.com"
}

# Filtered link extraction
{
  "action": "links",
  "url": "https://example.com",
  "only_same_domain": true,
  "max_links": 50,
  "include_nofollow": false
}
```

### Advanced Scraping
```python
# Research-focused scraping
{
  "action": "fetch",
  "url": "https://research-site.com/paper",
  "extract_tables": true,
  "extract_lists": true,
  "timeout": 45,
  "max_chars": 12000,
  "user_agent": "Research Bot 1.0"
}
```

## API Reference

### Parameters

- **action** (required): Scraping operation
  - `fetch` - Download and parse page content
  - `links` - Extract anchor links from page

- **url** (required): Target URL to scrape
  - Must be valid HTTP/HTTPS URL
  - Supports redirects and common URL formats

#### Fetch Action Parameters
- **timeout**: Request timeout in seconds (default: 20)
- **include_html**: Include raw HTML in response (default: false)
- **max_chars**: Truncate text length (default: 8000, 0=unlimited)
- **extract_tables**: Parse HTML tables (default: false)
- **extract_forms**: Parse form elements (default: false) 
- **extract_lists**: Parse ul/ol lists (default: false)
- **user_agent**: Custom User-Agent string

#### Links Action Parameters
- **include_nofollow**: Include nofollow links (default: false)
- **only_same_domain**: Limit to same domain links (default: false)
- **max_links**: Maximum links to return (default: 0=unlimited)

### Response Format

#### Fetch Response
```json
{
  "url": "https://example.com/article",
  "title": "Article Title",
  "text_content": "Extracted text content from the page...",
  "word_count": 1250,
  "char_count": 7834,
  "status_code": 200,
  "content_type": "text/html",
  "tables": [
    {
      "headers": ["Name", "Value", "Description"],
      "rows": [
        ["Item 1", "100", "First item"],
        ["Item 2", "200", "Second item"]
      ]
    }
  ],
  "forms": [
    {
      "action": "/submit",
      "method": "POST",
      "fields": [
        {
          "name": "username",
          "type": "text",
          "required": true
        }
      ]
    }
  ],
  "lists": [
    {
      "type": "ul",
      "items": ["First item", "Second item", "Third item"]
    }
  ]
}
```

#### Links Response
```json
{
  "url": "https://example.com",
  "links": [
    {
      "text": "Link Text",
      "href": "https://example.com/page1",
      "title": "Link Title",
      "rel": "nofollow",
      "target": "_blank"
    }
  ],
  "total_links": 25,
  "same_domain_links": 18,
  "external_links": 7
}
```

## Content Processing

### Text Extraction
- **Clean Text**: Removes HTML tags and formatting
- **Whitespace Normalization**: Cleans excessive whitespace
- **Special Character Handling**: Properly decodes HTML entities
- **Structure Preservation**: Maintains paragraph breaks

### Table Processing
When `extract_tables: true`:
- **Header Detection**: Automatically identifies table headers
- **Data Typing**: Attempts to identify numeric and date columns
- **Nested Tables**: Handles tables within tables
- **Empty Cell Handling**: Manages missing or empty data

### Form Analysis
When `extract_forms: true`:
- **Field Types**: Identifies input, select, textarea elements
- **Validation Rules**: Extracts required, pattern, and type attributes
- **Default Values**: Captures default and placeholder text
- **Submit Information**: Action URLs and HTTP methods

## Performance Optimization

### Token Efficiency
- **Smart Truncation**: `max_chars` parameter for content limiting
- **Structured Extraction**: Focus on relevant content sections
- **Whitespace Cleanup**: Removes unnecessary formatting
- **Content Prioritization**: Main content detection over navigation

### Network Optimization
- **Timeout Management**: Configurable timeouts prevent hanging
- **Retry Logic**: Automatic retry for transient failures
- **User-Agent Rotation**: Avoid being blocked by websites
- **Compression Support**: Accepts gzipped responses

### Intelligent Caching
- **Automatic Caching**: Results cached in `.cache/web_scraper/` directory
- **TTL-Based Expiration**: Default 30-minute cache lifetime (configurable)
- **Cache Keys**: Based on URL, operation type, and extraction options
- **Performance Boost**: Avoid repeated requests for same content
- **Cache Management**: Automatic cleanup of expired entries

## Error Handling

### Network Errors
- **Connection Timeout**: Configurable timeout with clear error messages
- **HTTP Errors**: Detailed status code reporting
- **SSL Errors**: Graceful handling of certificate issues
- **Redirect Loops**: Protection against infinite redirects

### Content Errors
- **Invalid HTML**: Robust parsing of malformed pages
- **Empty Content**: Appropriate handling of empty responses
- **Encoding Issues**: Automatic character encoding detection
- **JavaScript-heavy Sites**: Clear indication of limitations

### Common Error Responses
```json
{
  "error": "Request timeout after 20 seconds",
  "url": "https://slow-site.com",
  "suggestion": "Try increasing the timeout parameter or check if the site is accessible"
}
```

## Best Practices

### Respectful Scraping
- **Rate Limiting**: Built-in delays between requests
- **robots.txt Awareness**: Respect website scraping policies
- **User-Agent Identification**: Clear identification as a bot
- **Error Handling**: Don't retry aggressively on errors

### Content Quality
- **Relevant Extraction**: Focus on main content areas
- **Structure Preservation**: Use structured data extraction
- **Size Management**: Use `max_chars` for large pages
- **Encoding Handling**: Let the plugin handle character encoding

### Performance Tips
- **Targeted Scraping**: Only extract needed structured data
- **Timeout Tuning**: Adjust timeouts based on site responsiveness
- **Content Limits**: Use reasonable `max_chars` values
- **Batch Processing**: Group related scraping operations

## Legal and Ethical Considerations

### Website Policies
- **Terms of Service**: Review target website's terms
- **robots.txt**: Respect robots.txt directives
- **Rate Limits**: Don't overwhelm target servers
- **Copyright**: Respect content copyrights and licensing

### Data Usage
- **Public Content**: Only scrape publicly accessible content
- **Personal Data**: Avoid scraping personal information
- **Attribution**: Maintain proper source attribution
- **Data Retention**: Consider data retention policies

## Troubleshooting

### Common Issues

1. **Timeout Errors**
   - Increase timeout parameter
   - Check target website responsiveness
   - Verify network connectivity
   - Try during different times of day

2. **Empty or Incomplete Content**
   - Check if site requires JavaScript
   - Verify URL is accessible in browser
   - Try different User-Agent string
   - Check if site blocks bots

3. **Parsing Errors**
   - Enable `include_html` to see raw content
   - Check if content is malformed HTML
   - Try without structured data extraction
   - Verify character encoding handling

4. **Access Denied**
   - Check robots.txt compliance
   - Try different User-Agent
   - Verify site doesn't require authentication
   - Check for IP-based blocking

### Debugging Tips
- Use `include_html: true` for content inspection
- Start with basic fetch before adding structured extraction
- Check status codes and error messages
- Test URLs manually in browser first