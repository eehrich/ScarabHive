"""
Tests for MCP tool caching system.
"""

import asyncio
import pytest
from agent_system.mcp.tool_cache import ToolCache, CacheStatistics


class TestCacheStatistics:
    """Test CacheStatistics dataclass"""

    def test_initial_stats(self):
        """Test initial statistics are zero"""
        stats = CacheStatistics()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.invalidations == 0
        assert stats.sets == 0
        assert stats.hit_rate == 0.0

    def test_hit_rate_calculation(self):
        """Test hit rate percentage calculation"""
        stats = CacheStatistics(hits=90, misses=10)
        assert stats.hit_rate == 90.0

        stats = CacheStatistics(hits=50, misses=50)
        assert stats.hit_rate == 50.0

        stats = CacheStatistics(hits=1, misses=99)
        assert stats.hit_rate == 1.0

    def test_to_dict(self):
        """Test conversion to dictionary"""
        stats = CacheStatistics(hits=10, misses=5, invalidations=2, sets=5)
        data = stats.to_dict()

        assert data["hits"] == 10
        assert data["misses"] == 5
        assert data["invalidations"] == 2
        assert data["sets"] == 5
        assert data["hit_rate"] == 66.67


class TestToolCache:
    """Test ToolCache class"""

    @pytest.mark.asyncio
    async def test_cache_disabled(self):
        """Test cache when disabled"""
        cache = ToolCache(enabled=False)

        # Get should return None
        result = await cache.get("key1", "hash1")
        assert result is None

        # Set should do nothing
        await cache.set("key1", "value1", "hash1")
        result = await cache.get("key1", "hash1")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        """Test cache hit scenario"""
        cache = ToolCache(enabled=True)
        config_hash = "test_hash_123"

        # Set value
        await cache.set("key1", "value1", config_hash)

        # Get should return cached value
        result = await cache.get("key1", config_hash)
        assert result == "value1"

        # Check statistics
        stats = await cache.get_statistics()
        assert stats["statistics"]["hits"] == 1
        assert stats["statistics"]["misses"] == 0
        assert stats["statistics"]["sets"] == 1

    @pytest.mark.asyncio
    async def test_cache_miss(self):
        """Test cache miss scenario"""
        cache = ToolCache(enabled=True)

        # Get non-existent key
        result = await cache.get("nonexistent", "hash1")
        assert result is None

        # Check statistics
        stats = await cache.get_statistics()
        assert stats["statistics"]["hits"] == 0
        assert stats["statistics"]["misses"] == 1

    @pytest.mark.asyncio
    async def test_config_hash_invalidation(self):
        """Test automatic cache invalidation on config change"""
        cache = ToolCache(enabled=True)

        # Set value with config hash 1
        await cache.set("key1", "value1", "hash1")
        result = await cache.get("key1", "hash1")
        assert result == "value1"

        # Try to get with different config hash (should invalidate)
        result = await cache.get("key1", "hash2")
        assert result is None

        # Cache should be empty now
        stats = await cache.get_statistics()
        assert stats["size"] == 0
        assert stats["statistics"]["invalidations"] == 1

    @pytest.mark.asyncio
    async def test_manual_invalidation_all(self):
        """Test manual invalidation of entire cache"""
        cache = ToolCache(enabled=True)
        config_hash = "hash1"

        # Set multiple values
        await cache.set("key1", "value1", config_hash)
        await cache.set("key2", "value2", config_hash)

        # Verify they're cached
        assert await cache.get("key1", config_hash) == "value1"
        assert await cache.get("key2", config_hash) == "value2"

        # Invalidate all
        await cache.invalidate()

        # Both should be gone
        assert await cache.get("key1", config_hash) is None
        assert await cache.get("key2", config_hash) is None

        # Check statistics
        stats = await cache.get_statistics()
        assert stats["size"] == 0
        assert stats["statistics"]["invalidations"] == 1

    @pytest.mark.asyncio
    async def test_manual_invalidation_specific_key(self):
        """Test manual invalidation of specific key"""
        cache = ToolCache(enabled=True)
        config_hash = "hash1"

        # Set multiple values
        await cache.set("key1", "value1", config_hash)
        await cache.set("key2", "value2", config_hash)

        # Invalidate only key1
        await cache.invalidate("key1")

        # key1 should be gone, key2 should remain
        assert await cache.get("key1", config_hash) is None
        assert await cache.get("key2", config_hash) == "value2"

        # Check statistics
        stats = await cache.get_statistics()
        assert stats["size"] == 1
        assert stats["statistics"]["invalidations"] == 1

    @pytest.mark.asyncio
    async def test_max_size_enforcement(self):
        """Test cache size limit with FIFO eviction"""
        cache = ToolCache(enabled=True, max_size=2)
        config_hash = "hash1"

        # Add 3 items (should evict oldest)
        await cache.set("key1", "value1", config_hash)
        await cache.set("key2", "value2", config_hash)
        await cache.set("key3", "value3", config_hash)

        # key1 should be evicted (oldest)
        assert await cache.get("key1", config_hash) is None
        assert await cache.get("key2", config_hash) == "value2"
        assert await cache.get("key3", config_hash) == "value3"

        # Check size
        stats = await cache.get_statistics()
        assert stats["size"] == 2
        assert stats["max_size"] == 2

    @pytest.mark.asyncio
    async def test_concurrent_access(self):
        """Test thread-safe concurrent access"""
        cache = ToolCache(enabled=True)
        config_hash = "hash1"

        # Concurrent sets
        await asyncio.gather(
            cache.set("key1", "value1", config_hash),
            cache.set("key2", "value2", config_hash),
            cache.set("key3", "value3", config_hash),
        )

        # Concurrent gets
        results = await asyncio.gather(
            cache.get("key1", config_hash),
            cache.get("key2", config_hash),
            cache.get("key3", config_hash),
        )

        assert results == ["value1", "value2", "value3"]

    @pytest.mark.asyncio
    async def test_config_hash_computation(self):
        """Test config hash computation is consistent"""
        cache = ToolCache()

        # Same config should produce same hash
        config1 = {"servers": ["a", "b"], "blocked": ["tool1"]}
        config2 = {"servers": ["a", "b"], "blocked": ["tool1"]}

        hash1 = cache.compute_config_hash(config1)
        hash2 = cache.compute_config_hash(config2)
        assert hash1 == hash2

        # Different config should produce different hash
        config3 = {"servers": ["a", "b"], "blocked": ["tool2"]}
        hash3 = cache.compute_config_hash(config3)
        assert hash1 != hash3

    @pytest.mark.asyncio
    async def test_config_hash_key_order_independence(self):
        """Test config hash is independent of key order"""
        cache = ToolCache()

        # Different key order should produce same hash
        config1 = {"servers": ["a"], "blocked": ["tool1"]}
        config2 = {"blocked": ["tool1"], "servers": ["a"]}

        hash1 = cache.compute_config_hash(config1)
        hash2 = cache.compute_config_hash(config2)
        assert hash1 == hash2

    @pytest.mark.asyncio
    async def test_cache_statistics_accumulation(self):
        """Test statistics accumulate correctly over time"""
        cache = ToolCache(enabled=True)
        config_hash = "hash1"

        # Set once
        await cache.set("key1", "value1", config_hash)

        # Multiple hits
        for _ in range(10):
            await cache.get("key1", config_hash)

        # Multiple misses
        for _ in range(5):
            await cache.get("nonexistent", config_hash)

        # Check accumulated statistics
        stats = await cache.get_statistics()
        assert stats["statistics"]["hits"] == 10
        assert stats["statistics"]["misses"] == 5
        assert stats["statistics"]["sets"] == 1
        assert stats["statistics"]["hit_rate"] == 66.67

    @pytest.mark.asyncio
    async def test_complex_value_caching(self):
        """Test caching complex nested data structures"""
        cache = ToolCache(enabled=True)
        config_hash = "hash1"

        # Complex nested structure
        complex_value = {
            "plugins": {
                "plugin1": [
                    {"name": "tool1", "description": "desc1", "schema": {"type": "object"}},
                    {"name": "tool2", "description": "desc2", "schema": {"type": "string"}},
                ]
            },
            "external_servers": {
                "server1": [
                    {"name": "tool3", "blocked": False},
                ]
            }
        }

        # Cache and retrieve
        await cache.set("all_tools", complex_value, config_hash)
        result = await cache.get("all_tools", config_hash)

        # Verify structure is preserved
        assert result == complex_value
        assert result["plugins"]["plugin1"][0]["name"] == "tool1"
        assert result["external_servers"]["server1"][0]["blocked"] is False

    @pytest.mark.asyncio
    async def test_invalidation_nonexistent_key(self):
        """Test invalidating a key that doesn't exist"""
        cache = ToolCache(enabled=True)

        # Should not raise error
        await cache.invalidate("nonexistent_key")

        # Statistics should not increment
        stats = await cache.get_statistics()
        assert stats["statistics"]["invalidations"] == 0
