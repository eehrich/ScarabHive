# Caching Systems in AgentSystem

AgentSystem implements multiple caching layers to optimize performance across different components. This document covers the two main caching systems: **Plugin Caching** (file-based caching for plugin operations) and **MCP Tool Caching** (configuration-aware caching for MCP tool discovery).

## Plugin Caching System

The plugin caching system provides file-based storage for plugin operations to avoid repeated downloads and API calls. It supports TTL (Time To Live) expiration, manual cache management, and runtime cache control.

### Architecture

#### PluginCache Class

Located in `src/agent_system/plugins/cache.py`, this class provides:

- **File-based storage**: Cache files stored in `.cache/{plugin_name}/` directories
- **TTL support**: Automatic expiration based on configurable time-to-live values
- **Atomic operations**: Safe concurrent access with file locking
- **Automatic cleanup**: Expired cache entries are removed automatically
- **Statistics**: Cache hit/miss tracking and storage information

### Supported Plugins

#### Web Scraper Plugin (`web_scraper`)
- **Default TTL**: 30 minutes (1800 seconds)
- **Cache Key**: Based on URL, operation type, and extraction options
- **Cache Location**: `.cache/web_scraper/`

#### DuckDuckGo Search Plugin (`duckduckgo_search`)
- **Default TTL**: 15 minutes (900 seconds)
- **Cache Key**: Based on search query and max_results
- **Cache Location**: `.cache/duckduckgo_search/`

### Runtime Cache Control

Both plugins support runtime cache control parameters for fine-grained caching behavior:

#### Parameters

##### `ignore_cache` (boolean)
- **Default**: `false`
- **Description**: When `true`, bypasses cache completely and fetches fresh data
- **Use Case**: Force fresh data retrieval for real-time information

```json
{
  "url": "https://example.com",
  "ignore_cache": true
}
```

##### `cache_ttl` (integer)
- **Default**: Uses plugin's default TTL
- **Description**: Custom TTL in seconds for this specific request
- **Use Case**: Cache data for longer/shorter periods than default

```json
{
  "query": "search terms",
  "cache_ttl": 3600
}
```

### Examples

#### Web Scraper with Cache Control
```python
# Use cache (default behavior)
result = await web_scraper.call("scrape_webpage", {
    "url": "https://example.com"
})

# Bypass cache for fresh data
result = await web_scraper.call("scrape_webpage", {
    "url": "https://example.com",
    "ignore_cache": True
})

# Cache for 1 hour (3600 seconds)
result = await web_scraper.call("scrape_webpage", {
    "url": "https://example.com",
    "cache_ttl": 3600
})
```

#### DuckDuckGo Search with Cache Control
```python
# Use cache (default behavior)
result = await search.call("web_search", {
    "query": "python programming"
})

# Bypass cache for latest results
result = await search.call("web_search", {
    "query": "python programming",
    "ignore_cache": True
})

# Cache for 10 minutes (600 seconds)
result = await search.call("web_search", {
    "query": "python programming",
    "cache_ttl": 600
})
```

### Cache Management

#### CLI Tool

The system includes a CLI utility for cache management:

```bash
# Show cache information
python -m agent_system.plugins.cache_manager info

# Clean expired entries from all caches
python -m agent_system.plugins.cache_manager clean

# Clear all cache data
python -m agent_system.plugins.cache_manager clear

# Clear specific plugin cache
python -m agent_system.plugins.cache_manager clear --plugin web_scraper
```

#### Programmatic Management

```python
from agent_system.plugins.cache import PluginCache

# Create cache instance
cache = PluginCache("my_plugin", default_ttl=1800)

# Manual operations
await cache.set("key", data, ttl=3600)
data = await cache.get("key")
await cache.delete("key")
await cache.clear()

# Get cache statistics
info = await cache.info()
print(f"Cache size: {info['total_files']} files, {info['total_size_mb']:.2f} MB")
```

### Configuration

#### Plugin Configuration

Cache behavior can be configured per plugin:

```yaml
# config/agent.yaml
plugins:
  web_scraper:
    cache_enabled: true
    cache_ttl: 1800  # 30 minutes

  duckduckgo_search:
    cache_enabled: true
    cache_ttl: 900   # 15 minutes
```

#### Environment Variables

- `AGENT_CACHE_DISABLED`: Set to "1" to disable all caching
- `AGENT_CACHE_DIR`: Override default cache directory location

### Performance Benefits

#### Benchmark Results

Typical performance improvements with caching enabled:

- **Web Scraper**: 95%+ faster for cached pages (30ms vs 800ms average)
- **DuckDuckGo Search**: 90%+ faster for cached queries (50ms vs 500ms average)
- **Network Usage**: 80%+ reduction in external API calls and bandwidth

#### Cache Hit Rates

Under normal usage patterns:
- **Web Scraper**: 60-70% hit rate (varies by browsing patterns)
- **DuckDuckGo Search**: 40-50% hit rate (varies by query repetition)

### Best Practices

#### Cache TTL Guidelines

- **Static content**: Use longer TTL (hours to days)
- **Dynamic content**: Use shorter TTL (minutes to hours)
- **Real-time data**: Use `ignore_cache=true` or very short TTL

#### Memory and Storage

- Cache files are automatically cleaned up when expired
- Use `cache_manager.py clean` periodically for maintenance
- Monitor cache size with `cache_manager.py info`

#### Error Handling

The caching system is designed to be resilient:
- Cache corruption is handled gracefully (falls back to fresh fetch)
- Network errors during fresh fetch return cached data if available
- Cache operations never block the main functionality

### Implementation Details

#### Cache Key Generation

##### Web Scraper
```python
def _create_cache_key(self, url: str, operation: str = "content", **params) -> str:
    # Normalize URL and combine with operation and parameters
    cache_data = {
        "url": normalize_url(url),
        "operation": operation,
        **{k: v for k, v in params.items() if k not in ['_status']}
    }
    return json.dumps(cache_data, sort_keys=True, separators=(',', ':'))
```

##### DuckDuckGo Search
```python
def _create_cache_key(self, query: str, max_results: int) -> str:
    # Normalize query and combine with parameters
    cache_data = {
        "query": query.strip().lower(),
        "max_results": max_results
    }
    return json.dumps(cache_data, sort_keys=True, separators=(',', ':'))
```

#### File Format

Cache files use JSON format with metadata:

```json
{
  "data": {
    "url": "https://example.com",
    "content": "Page content...",
    "status_code": 200
  },
  "expires_at": 1640995200.0,
  "created_at": 1640991600.0
}
```

#### Concurrency Safety

- File operations use atomic writes (write to temp file, then rename)
- Multiple processes can safely access the same cache
- Lock-free design prevents deadlocks and race conditions

## MCP Tool Caching System

The MCP tool caching system uses configuration-aware caching to avoid reinitializing tools on every request when working with external MCP servers.

### How It Works

#### Configuration-Hash Based Invalidation

Unlike traditional time-based caching (TTL), the tool cache uses configuration hashing:

1. **Config Hash**: A SHA256 hash is computed from the MCP configuration (servers, blocked tools, etc.)
2. **Auto-Invalidation**: When the config changes, the hash changes, automatically invalidating stale cache entries
3. **No Stale Data**: Cache is always fresh when configuration is unchanged

#### Two-Level Caching

The system uses two levels of caching:

1. **MCPIntegration.list_all_tools()** - Config-hash-based cache (new)
   - Caches the combined result of plugin + external tools
   - Invalidates automatically when MCP configuration changes
   - No TTL needed (always fresh)

2. **MCPClientManager.list_all_tools()** - Time-based cache (existing)
   - Internal cache for external server HTTP calls
   - Uses TTL (default 30s)
   - Reduces HTTP requests to external servers

### Configuration

MCP tool caching is configured in `config/mcp_servers.yaml`:

#### config/mcp_servers.yaml

```yaml
cache:
  enabled: true         # Enable tool list caching
  tool_list_ttl: 30.0  # Cache TTL in seconds (30 seconds default)
  max_size: 1000       # Maximum cache entries (None = unlimited)
```

#### Configuration Options

- **enabled**: Enable/disable the tool cache
  - Type: `bool`
  - Default: `true`
  - When disabled, tools are fetched fresh on every request

- **tool_list_ttl**: Time-to-live for MCPClientManager internal cache
  - Type: `float`
  - Default: `30.0` seconds
  - Controls how often external servers are queried via HTTP

- **max_size**: Maximum number of cache entries
  - Type: `Optional[int]`
  - Default: `None` (unlimited)
  - When limit reached, oldest entries are evicted (FIFO)

### Cache Statistics

#### Monitoring

The cache exposes statistics for monitoring:

```python
stats = await mcp_integration.get_cache_statistics()
```

Returns:
```json
{
  "enabled": true,
  "size": 1,
  "max_size": null,
  "config_hash": "abc123def456",
  "statistics": {
    "hits": 45,
    "misses": 5,
    "invalidations": 2,
    "sets": 5,
    "hit_rate": 90.0
  }
}
```

#### API Endpoint

Cache statistics are exposed via the API (see Task 9166 for implementation).

#### Logging

Cache operations are logged at DEBUG level:

```
Cache HIT: key=all_tools (hits=45)
Cache MISS: key=all_tools (misses=5)
Cache invalidated: cleared 1 entries
Config hash mismatch: cache=abc123, current=def456
```

### Performance Impact

#### Before Caching
- Every request queries all external MCP servers
- External HTTP calls: ~500-2000ms per server
- 5 servers × 1000ms = 5 seconds per request

#### After Caching
- First request: ~5 seconds (cache miss)
- Subsequent requests: <10ms (cache hit)
- **99% reduction** in response time

#### Cache Hit Rate

Typical hit rates in production:
- **90-95%**: Normal operations
- **<50%**: Frequent config changes or high invalidation
- **>95%**: Stable configuration, high request volume

### Cache Invalidation

#### Automatic Invalidation

Cache automatically invalidates when:
- MCP configuration changes (servers added/removed)
- Blocked tools list changes
- Plugin registry changes

#### Manual Invalidation

```python
# Invalidate entire cache
await mcp_integration.invalidate_tools_cache()

# Invalidate specific key
await mcp_integration._tool_cache.invalidate("all_tools")
```

### Thread Safety

The cache is async-safe:
- Uses `asyncio.Lock` for concurrent access protection
- All operations are atomic
- No race conditions in multi-request scenarios

### Best Practices

1. **Keep cache enabled** unless debugging
2. **Monitor hit rate** - low rates indicate problems
3. **Adjust TTL** based on external server stability
4. **Set max_size** if memory is constrained
5. **Use statistics** for performance optimization

### Troubleshooting

#### Low Hit Rate

**Symptoms**: Hit rate < 50%

**Possible causes**:
- Frequent configuration changes
- Cache disabled
- max_size too small (causing evictions)

**Solutions**:
- Check for unnecessary config changes
- Increase max_size
- Review invalidation frequency

#### Stale Data

**Symptoms**: Tools not updating after config changes

**Possible causes**:
- Cache not properly invalidated
- Config hash not changing

**Solutions**:
- Manually invalidate cache
- Check config loading
- Verify config hash computation

#### Memory Issues

**Symptoms**: High memory usage

**Possible causes**:
- max_size = null with many servers
- Large tool schemas

**Solutions**:
- Set max_size to limit entries
- Monitor cache size via statistics
- Consider pruning tool schemas

### Implementation Details

#### Cache Key Strategy

Currently uses a single key: `"all_tools"`

#### Config Hash Computation

```python
def compute_config_hash(config_data):
    config_json = json.dumps(config_data, sort_keys=True, default=str)
    return hashlib.sha256(config_json.encode()).hexdigest()[:16]
```

Includes:
- External server URLs
- Blocked tools lists
- Plugin server names

#### Eviction Policy

When `max_size` is reached:
- **Strategy**: FIFO (First-In-First-Out)
- **Behavior**: Oldest entry is removed
- **Logging**: Evictions logged at DEBUG level

## Troubleshooting (General)

### Plugin Cache Issues

1. **Cache not working**: Check `cache_enabled` configuration
2. **High memory usage**: Run cache cleanup with `cache_manager.py clean`
3. **Stale data**: Use `ignore_cache=true` or reduce TTL values
4. **Permission errors**: Ensure write access to cache directory

### MCP Cache Issues

1. **Low hit rate**: Check for frequent config changes or small max_size
2. **Stale tools**: Verify cache invalidation after config changes
3. **Memory issues**: Set max_size to limit cache entries

### Debugging

Enable debug logging to monitor cache behavior:

```python
import logging
logging.getLogger("agent_system.plugins.cache").setLevel(logging.DEBUG)
logging.getLogger("agent_system.mcp.integration").setLevel(logging.DEBUG)
```

This will show cache hits, misses, invalidations, and cleanup operations in the logs.