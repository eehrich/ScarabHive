# MCP Tool Caching System

## Overview

The AgentSystem MCP integration uses a configuration-aware cache to avoid reinitializing tools on every request. This significantly improves response times when working with external MCP servers.

## How It Works

### Configuration-Hash Based Invalidation

Unlike traditional time-based caching (TTL), the tool cache uses configuration hashing:

1. **Config Hash**: A SHA256 hash is computed from the MCP configuration (servers, blocked tools, etc.)
2. **Auto-Invalidation**: When the config changes, the hash changes, automatically invalidating stale cache entries
3. **No Stale Data**: Cache is always fresh when configuration is unchanged

### Two-Level Caching

The system uses two levels of caching:

1. **MCPIntegration.list_all_tools()** - Config-hash-based cache (new)
   - Caches the combined result of plugin + external tools
   - Invalidates automatically when MCP configuration changes
   - No TTL needed (always fresh)

2. **MCPClientManager.list_all_tools()** - Time-based cache (existing)
   - Internal cache for external server HTTP calls
   - Uses TTL (default 30s)
   - Reduces HTTP requests to external servers

## Configuration

### config/mcp.yaml

```yaml
mcp_system:
  external_servers:
    cache:
      enabled: true              # Enable tool list caching (default: true)
      tool_list_ttl: 30.0        # TTL for MCPClientManager cache (seconds)
      max_size: null             # Maximum cache entries (null = unlimited)
```

### Configuration Options

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

## Cache Statistics

### Monitoring

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

### API Endpoint

Cache statistics are exposed via the API (see Task 9166 for implementation).

### Logging

Cache operations are logged at DEBUG level:

```
Cache HIT: key=all_tools (hits=45)
Cache MISS: key=all_tools (misses=5)
Cache invalidated: cleared 1 entries
Config hash mismatch: cache=abc123, current=def456
```

## Performance Impact

### Before Caching
- Every request queries all external MCP servers
- External HTTP calls: ~500-2000ms per server
- 5 servers × 1000ms = 5 seconds per request

### After Caching
- First request: ~5 seconds (cache miss)
- Subsequent requests: <10ms (cache hit)
- **99% reduction** in response time

### Cache Hit Rate

Typical hit rates in production:
- **90-95%**: Normal operations
- **<50%**: Frequent config changes or high invalidation
- **>95%**: Stable configuration, high request volume

## Cache Invalidation

### Automatic Invalidation

Cache automatically invalidates when:
- MCP configuration changes (servers added/removed)
- Blocked tools list changes
- Plugin registry changes

### Manual Invalidation

```python
# Invalidate entire cache
await mcp_integration.invalidate_tools_cache()

# Invalidate specific key
await mcp_integration._tool_cache.invalidate("all_tools")
```

## Thread Safety

The cache is async-safe:
- Uses `asyncio.Lock` for concurrent access protection
- All operations are atomic
- No race conditions in multi-request scenarios

## Best Practices

1. **Keep cache enabled** unless debugging
2. **Monitor hit rate** - low rates indicate problems
3. **Adjust TTL** based on external server stability
4. **Set max_size** if memory is constrained
5. **Use statistics** for performance optimization

## Troubleshooting

### Low Hit Rate

**Symptoms**: Hit rate < 50%

**Possible causes**:
- Frequent configuration changes
- Cache disabled
- max_size too small (causing evictions)

**Solutions**:
- Check for unnecessary config changes
- Increase max_size
- Review invalidation frequency

### Stale Data

**Symptoms**: Tools not updating after config changes

**Possible causes**:
- Cache not properly invalidated
- Config hash not changing

**Solutions**:
- Manually invalidate cache
- Check config loading
- Verify config hash computation

### Memory Issues

**Symptoms**: High memory usage

**Possible causes**:
- max_size = null with many servers
- Large tool schemas

**Solutions**:
- Set max_size to limit entries
- Monitor cache size via statistics
- Consider pruning tool schemas

## Implementation Details

### Cache Key Strategy

Currently uses a single key: `"all_tools"`

### Config Hash Computation

```python
def compute_config_hash(config_data):
    config_json = json.dumps(config_data, sort_keys=True, default=str)
    return hashlib.sha256(config_json.encode()).hexdigest()[:16]
```

Includes:
- External server URLs
- Blocked tools lists
- Plugin server names

### Eviction Policy

When `max_size` is reached:
- **Strategy**: FIFO (First-In-First-Out)
- **Behavior**: Oldest entry is removed
- **Logging**: Evictions logged at DEBUG level
